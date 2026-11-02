"""Real HTTP cache replay preserves one dataset and never calls collection/LLM."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).parents[1]), str(Path(__file__).parent)]
import server
from product_cache import ProductCache
from session_workspace import SessionWorkspace
from job_store import JobStore
from test_issue_evidence import case
from analysis.pipeline import input_digest, clusters_fingerprint
from analysis import issue_extractor, claim_extractor, gap_analyzer
from analysis.clustering import CLUSTER_RULE_VERSION


class HistoryCacheServerTest(unittest.TestCase):
    pid = "12345678"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = {k: getattr(server, k) for k in ("SESSION", "STORE", "EXPORT_DIR", "ANALYSIS_DIR",
            "LEGACY_STORE", "LEGACY_EXPORT_DIR", "PRODUCT_CACHE", "CACHE_ENABLED", "CACHE_SOURCES")}
        self.old_product_root = server.product_module.ROOT
        server.SESSION = SessionWorkspace(self.root / "sessions")
        server.STORE = JobStore(server.SESSION.root)
        server.EXPORT_DIR = server.SESSION.root / "runtime/exports"
        server.ANALYSIS_DIR = server.SESSION.root / "runtime/analysis"
        server.EXPORT_DIR.mkdir(parents=True)
        server.ANALYSIS_DIR.mkdir(parents=True)
        server.LEGACY_STORE = None
        server.LEGACY_EXPORT_DIR = self.root / "no-legacy"
        server.CACHE_SOURCES = {}
        server.product_module.ROOT = server.SESSION.root
        self.seed_root = self.root / "saved-round"
        self.seed()
        self.cache = ProductCache(self.root / "cache")
        server.initialize_history_cache(cache=self.cache, roots=[self.seed_root])
        self.http = server.create_app_server(port=0)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join(3)
        server.SESSION.cleanup()
        for k, v in self.saved.items():
            setattr(server, k, v)
        server.product_module.ROOT = self.old_product_root
        self.tmp.cleanup()

    def seed(self):
        data = case()
        reviews = data["reviews"]
        for r in reviews:
            r["product_id"] = self.pid
        self.reviews = reviews
        analysis = self.seed_root / "runtime/analysis" / self.pid
        exports = self.seed_root / "runtime/exports"
        analysis.mkdir(parents=True); exports.mkdir(parents=True)
        self.source = exports / f"tmall_{self.pid}_saved.json"
        self.write(self.source, {"product_id": self.pid, "product": {"id": self.pid, "title": "真实记录复用测试"},
            "reviews": reviews, "collection_info": {"scanned_count": 30, "matched_count": 4,
                "pages_fetched": 1, "finished_at": "2026-10-29T15:00:00+08:00", "is_complete": True}})
        self.digest = input_digest(reviews)
        clusters = {"clusters": data["clusters"], "params": {"rule_version": CLUSTER_RULE_VERSION}}
        fp = clusters_fingerprint(clusters)
        summary = {"product_id": self.pid, "analysis_status": "completed", "created_at": "2026-10-29T15:00:00+08:00",
            "input": {"files": [str(self.source)], "digest": self.digest}, "issue_count": 4, "cluster_count": 1,
            "claim_count": 0, "gap_count": 0, "valid_review_count": 4, "errors": [],
            "model": {"prompt_version": issue_extractor.PROMPT_VERSION + ":apparel"},
            "algorithm_params": clusters["params"], "fingerprints": {"clusters": fp}, "stages": {"claims": "ok", "gaps": "ok"}}
        payloads = {"analysis_summary": summary, "data_quality": {"valid_review_count": 4, "unique_review_count": 4},
            "cleaned_reviews": {"product_id": self.pid, "reviews": reviews, "input_digest": self.digest},
            "statistics": {"clusters": data["clusters"]}, "issue_clusters": clusters,
            "issues_flat": {"issues": data["issues"]},
            "extracted_issues": {"reviews": [{"review_id": r["review_id"], "content": r["content"], "status": "cached", "errors": [],
                "issues": [i for i in data["issues"] if i["review_id"] == r["review_id"]]} for r in reviews]},
            "extracted_claims": {"claims": [], "input_export": str(self.source), "errors": [], "prompt_version": claim_extractor.PROMPT_VERSION},
            "claim_gaps": {"gaps": [], "errors": [], "prompt_version": gap_analyzer.PROMPT_VERSION, "clusters_fingerprint": fp}}
        for name, value in payloads.items():
            self.write(analysis / (name + ".json"), value)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def request(self, path, payload=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw

    def replay(self, **extra):
        return self.request("/api/jobs", {"url": self.pid, "mode": "count", "value": 100, **extra})

    def test_repeat_search_opens_full_saved_analysis_without_collection_or_key(self):
        with patch.object(server, "_thread_start") as collect, patch("analysis.llm_client.LLMConfig.from_env", return_value=None) as key:
            code, job = self.replay()
            self.assertEqual(code, 200)
            self.assertTrue(job["cache_hit"] and job["analysis_available"])
            self.assertEqual((job["progress"]["scanned"], job["progress"]["matched"]), (30, 4))
            code, existing = self.request("/api/analysis", {"product_id": self.pid, "force": False})
            self.assertEqual((code, existing["status"]), (200, "exists"))
            collect.assert_not_called(); key.assert_not_called()
        model = server.report_model(self.pid)
        self.assertEqual(model["issue_evidence"]["as_of"], "2026-10-29")
        self.assertAlmostEqual(model["report_decision"]["windows"]["30"]["overall"]["index"], 90)
        self.assertEqual({r["review_id"] for r in model["reviews"]}, {r["review_id"] for r in self.reviews})
        self.assertFalse(list(server.ANALYSIS_DIR.glob("*/report*")))

    def test_history_survives_clear_and_preserves_source_dates(self):
        _, before = self.request("/api/jobs/recent")
        self.assertIn("cache" + self.pid, [j["id"] for j in before["jobs"]])
        self.replay()
        self.request("/api/session/clear", {})
        self.assertTrue(self.cache.get(self.pid)["has_analysis"])
        code, job = self.request("/api/jobs/cache" + self.pid)
        self.assertEqual(code, 200)
        self.assertTrue(job["analysis_available"])
        self.assertEqual(job["summary"]["collection_info"]["finished_at"], "2026-10-29T15:00:00+08:00")

    def test_force_refresh_bypasses_cache(self):
        self.replay()
        with patch.object(server, "_thread_start") as collect:
            code, job = self.replay(force_refresh=True)
            self.assertEqual(code, 202)
            collect.assert_called_once_with(job["id"])
            self.assertFalse(job["cache_hit"])
        self.assertNotIn(self.pid, server.CACHE_SOURCES)

    def test_failed_new_round_cannot_replace_or_mix_successful_history(self):
        target = server.ANALYSIS_DIR / self.pid
        shutil.copytree(self.seed_root / "runtime/analysis" / self.pid, target)
        failed = json.loads((target / "extracted_issues.json").read_text(encoding="utf-8"))
        for row in failed["reviews"]:
            row.update(status="validation_failed", issues=[], errors=["RemoteDisconnected"])
        self.write(target / "extracted_issues.json", failed)
        _, result = self.request("/api/analysis/product/" + self.pid)
        self.assertEqual(result["original_status"], "failed")
        code, result = self.request(f"/api/analysis/product/{self.pid}/artifacts")
        self.assertEqual(code, 409)
        self.assertEqual(result["kind"], "analysis_incomplete")
        _, job = self.replay()
        self.assertTrue(job["analysis_available"])
        self.assertEqual(server.report_model(self.pid)["issue_evidence"]["windows"]["30"]["overall"]["probability_estimate"]["total"], 4)
        self.assertEqual(json.loads((target / "extracted_issues.json").read_text(encoding="utf-8"))["reviews"][0]["status"], "validation_failed")

    def test_known_short_link_reuses_cache_without_resolving_again(self):
        import history_cache
        url = "https://e.tb.cn/h.Existing?tk=sharecode 分享商品"
        history_cache.remember_share(self.cache, url, self.pid)
        with patch.object(server, "resolve_product_input", side_effect=AssertionError("network resolution should not run")):
            code, job = self.request("/api/jobs", {"url": url, "mode": "count", "value": 300})
        self.assertEqual(code, 200)
        self.assertTrue(job["analysis_available"])


if __name__ == "__main__":
    unittest.main()
