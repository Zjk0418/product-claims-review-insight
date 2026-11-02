"""会话暂存、API保密、预览和手动导出的真实HTTP集成测试；不调用模型。"""
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import server
from api_settings import ApiSettingsStore
from job_store import JobStore
from session_workspace import SessionWorkspace
from analysis import claim_extractor, gap_analyzer, issue_extractor
from analysis.clustering import CLUSTER_RULE_VERSION
from analysis.pipeline import clusters_fingerprint, input_digest


class ArtifactApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = {key: getattr(server, key) for key in (
            "SESSION", "STORE", "EXPORT_DIR", "ANALYSIS_DIR", "LEGACY_STORE", "LEGACY_EXPORT_DIR", "SETTINGS"
        )}
        self.old_product_root = server.product_module.ROOT
        server.SESSION = SessionWorkspace(root / "sessions")
        server.STORE = JobStore(server.SESSION.root)
        server.EXPORT_DIR = server.SESSION.root / "runtime/exports"
        server.ANALYSIS_DIR = server.SESSION.root / "runtime/analysis"
        server.EXPORT_DIR.mkdir(parents=True)
        server.LEGACY_STORE = None
        server.LEGACY_EXPORT_DIR = root / "empty_legacy"
        server.SETTINGS = ApiSettingsStore(root / "config")
        self.pid = "112233"
        self.seed()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)
        server.SESSION.cleanup()
        for key, value in self.saved.items():
            setattr(server, key, value)
        server.product_module.ROOT = self.old_product_root
        self.tmp.cleanup()

    def seed(self):
        review = {"review_id": "r1", "star": 1, "content": "涂完过敏，使用体验不舒服", "text": "涂完过敏，使用体验不舒服",
                  "valid_for_analysis": True}
        issue = {"issue_id": "i1", "review_id": "r1", "object": "皮肤", "aspect": "使用舒适性",
                 "direction": "irritation", "manifestation": "过敏", "evidence": "涂完过敏"}
        cluster = {"cluster_id": "C1", "member_issue_ids": ["i1"], "review_ids": ["r1"], "review_count": 1,
                   "object": "皮肤", "aspect": "使用舒适性", "problem_name": "过敏", "review_evidence": {"r1": "涂完过敏"}}
        clusters = {"params": {"rule_version": CLUSTER_RULE_VERSION}, "clusters": [cluster]}
        fp = clusters_fingerprint(clusters)
        summary = {"product_id": self.pid, "analysis_status": "completed", "errors": [], "valid_review_count": 1,
                   "issue_count": 1, "cluster_count": 1, "claim_count": 1, "gap_count": 1,
                   "model": {"prompt_version": issue_extractor.PROMPT_VERSION + ":beauty"},
                   "algorithm_params": clusters["params"], "fingerprints": {"clusters": fp},
                   "input": {"digest": input_digest([review])}, "stages": {"claims": "ok", "gaps": "ok"}}
        export = {"product_id": self.pid, "product": {"id": self.pid, "title": "测试精华"}, "reviews": [review]}
        (server.EXPORT_DIR / f"tmall_{self.pid}_test.json").write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
        payloads = {"summary": summary, "quality": {"valid_review_count": 1},
                    "reviews": {"input_digest": input_digest([review]), "reviews": [review]}, "clusters": clusters,
                    "extracted": {"reviews": [{"review_id": "r1", "issues": [issue]}]}, "flat": {"issues": [issue]},
                    "statistics": {"clusters": [cluster]},
                    "claims": {"prompt_version": claim_extractor.PROMPT_VERSION, "claims": [{"claim_id": "p1",
                        "aspect": "使用舒适性", "normalized_claim": "配方温和", "original_text": "温和", "claim_type": "comfort_experience"}]},
                    "gaps": {"prompt_version": gap_analyzer.PROMPT_VERSION, "clusters_fingerprint": fp,
                        "gaps": [{"claim_id": "p1", "gap_type": "potential_conflict", "claim_text": "配方温和",
                            "claim_aspect": "使用舒适性", "evidence_review_ids": ["r1"], "confidence_level": "low",
                            "related_clusters": ["C1"], "review_evidence": [{"review_id": "r1", "evidence": "涂完过敏"}]}]}}
        paths = server._analysis_paths(self.pid)
        paths["base"].mkdir(parents=True)
        for key, value in payloads.items():
            paths[key].write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def request(self, path, payload=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=12)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_catalog_preview_and_export_do_not_write_reports(self):
        folder = server.ANALYSIS_DIR / self.pid
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        code, _, raw = self.request(f"/api/analysis/product/{self.pid}/artifacts")
        self.assertEqual(code, 200)
        catalog = json.loads(raw)
        self.assertEqual(len(catalog["artifacts"]), 5)
        code, headers, body = self.request(catalog["report_url"])
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("测试精华", body.decode())
        for item in catalog["artifacts"]:
            if item["id"] in {"product_json", "reviews_json", "reviews_xlsx"}:
                preview_code, preview_headers, preview = self.request(item["preview_url"])
                self.assertEqual(preview_code, 200, item)
                self.assertIn("style-src 'unsafe-inline'", preview_headers["Content-Security-Policy"])
                self.assertIn(b"<style>", preview)
                self.assertNotIn(b'rel="stylesheet"', preview, "isolated file previews must carry their own theme")
            code, _, data = self.request(item["download_url"])
            self.assertEqual(code, 200, item)
            if item["id"] == "report_pdf":
                self.assertTrue(data.startswith(b"%PDF"))
            if item["id"] == "reviews_xlsx":
                self.assertIn("xl/workbook.xml", zipfile.ZipFile(io.BytesIO(data)).namelist())
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_selective_zip_contains_only_selected_files(self):
        code, _, raw = self.request(f"/api/analysis/product/{self.pid}/export", {"artifact_ids": ["reviews_json", "report_html"]})
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertEqual(len(z.namelist()), 2)
            self.assertEqual({Path(p).suffix for p in z.namelist()}, {".json", ".html"})
        code, _, _ = self.request(f"/api/analysis/product/{self.pid}/export", {"artifact_ids": []})
        self.assertEqual(code, 400)

    def test_settings_are_session_only_and_never_echo_key(self):
        key = "test-key-never-export-12345678"
        code, _, raw = self.request("/api/settings", {"api_key": key, "remember": False})
        self.assertEqual(code, 200)
        self.assertNotIn(key.encode(), raw)
        self.assertTrue(json.loads(raw)["configured"])
        self.assertFalse(server.SETTINGS.path.exists())
        _, _, raw = self.request("/api/settings")
        self.assertNotIn(key.encode(), raw)

    def test_cross_origin_settings_and_bad_host_are_rejected(self):
        code, _, _ = self.request("/api/settings", {"api_key": "evil-key"}, {"Origin": "https://untrusted.example"})
        self.assertEqual(code, 403)
        self.assertFalse(server.SETTINGS.public()["configured"])
        code, _, _ = self.request("/api/settings", headers={"Host": "untrusted.example"})
        self.assertEqual(code, 403)

    def test_clear_deletes_owned_session_but_keeps_api_configuration(self):
        server.SETTINGS.update({"api_key": "session-test-12345678", "remember": False})
        old_root = server.SESSION.root
        code, _, raw = self.request("/api/session/clear", {})
        self.assertEqual(code, 200, raw)
        self.assertFalse(old_root.exists())
        self.assertTrue(server.SESSION.root.exists())
        self.assertTrue(server.SETTINGS.public()["configured"])

    def test_unknown_artifact_is_not_a_file_path(self):
        code, _, _ = self.request(f"/api/analysis/product/{self.pid}/export/unknown")
        self.assertEqual(code, 404)

    def test_pdf_viewer_and_worker_are_local_and_do_not_export(self):
        _, _, raw = self.request(f"/api/analysis/product/{self.pid}/artifacts")
        item = next(row for row in json.loads(raw)["artifacts"] if row["id"] == "report_pdf")
        code, headers, body = self.request(item["preview_url"])
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"/static/pdf-preview.mjs", body)
        self.assertIn(f"/api/analysis/product/{self.pid}/preview/report_pdf".encode(), body)
        self.assertNotIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"<style>", body)
        self.assertNotIn(b"__PREVIEW_STYLES__", body)
        self.assertNotIn(b'rel="stylesheet"', body, "strict preview CSP must not leave the theme blocked")
        for path in ("/static/pdf-preview.mjs", "/static/pdfjs/pdf.min.mjs", "/static/pdfjs/pdf.worker.min.mjs"):
            code, headers, body = self.request(path)
            self.assertEqual(code, 200)
            self.assertIn("text/javascript", headers["Content-Type"])
            self.assertTrue(body)
        code, _, _ = self.request("/static/../../.env")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
