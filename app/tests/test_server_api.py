"""本地服务的接口集成测试。

用临时目录和假的 collect/inspect 函数启动真实 HTTP 服务，覆盖：
暂停 → 恢复、重复恢复、浏览器断开、取消、刷新后仍可见、JSON 导入。
真实浏览器恢复不在这里验证。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server
from job_store import JobStore


def http(method: str, url: str, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else {}


def make_result(status="success", reviews=None, intervention=None, stop_reason="完成", next_page=1):
    reviews = reviews if reviews is not None else [
        {"review_id": f"r{i}", "star": 2, "content": f"第 {i} 条：用了一周没有效果"} for i in range(6)
    ]
    return {
        "platform": "tmall",
        "product_id": "123456",
        "product_url": "https://detail.tmall.com/item.htm?id=123456",
        "product": {"id": "123456", "title": "测试商品"},
        "promotional_claims": [],
        "scope": {"mode": "count", "count": 100},
        "reviews": reviews,
        "collection_info": {
            "status": status,
            "is_complete": status == "success",
            "stop_reason": stop_reason,
            "scanned_count": 20,
            "matched_count": len(reviews),
            "matched_without_star": 0,
            "pages_fetched": 2,
            "page_size": 50,
            "api_total": 30,
            "api_total_page": 2,
            "oldest_scanned_date": "2026-10-01",
            "newest_scanned_date": "2026-10-20",
            "started_at": "2026-10-24T10:00:00",
            "finished_at": "2026-10-24T10:05:00",
            "resume_count": 0,
            "next_page": next_page,
            "review_dom_present": True,
            "review_sufficiency": {
                "matched": len(reviews),
                "minimum_recommended": 5,
                "sufficient": len(reviews) >= 5,
            },
            "errors": [],
            "product_info": {},
        },
        "intervention": intervention,
    }


class FakeCollector:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    async def __call__(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
            result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
            # 模拟真实采集器：暂停/失败前落一次断点
            status = result["collection_info"]["status"]
            if status in {"awaiting_user", "rate_limited", "failed", "cancelled"}:
                callback = kwargs.get("on_checkpoint")
                if callback:
                    info = result["collection_info"]
                    # 真实采集器里 seen 是“全部扫描过的评价 ID”，不只是命中的低星
                    review_ids = [r["review_id"] for r in result["reviews"]]
                    scanned = int(info.get("scanned_count", 0))
                    seen = review_ids + [
                        f"s{i}" for i in range(max(0, scanned - len(review_ids)))
                    ]
                    callback(
                        {
                            "version": 1,
                            "product_id": result["product_id"],
                            "reviews": result["reviews"],
                            "seen": seen,
                            "next_page": info.get("next_page", 1),
                            "scanned": scanned,
                            "pages": info.get("pages_fetched", 0),
                            "tab_id": "fake-tab",
                        }
                    )
        return result


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.original = {
            "ROOT": server.ROOT,
            "STORE": server.STORE,
            "EXPORT_DIR": server.EXPORT_DIR,
            "ANALYSIS_DIR": server.ANALYSIS_DIR,
            "ANALYSIS_JOBS": server.ANALYSIS_JOBS,
            "ACTIVE_ANALYSIS": server.ACTIVE_ANALYSIS,
            "collect": server.collect,
            "cdp_health": server.cdp_health,
            "inspect": server.inspect_product_page,
            "find_tab": server.find_tab,
            "focus_tab": server.focus_tab,
            "open_foreground_tab": server.open_foreground_tab,
        }
        server.ROOT = root
        server.STORE = JobStore(root)
        server.EXPORT_DIR = root / "exports"
        server.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        server.ANALYSIS_DIR = root / "analysis"
        server.ANALYSIS_JOBS = {}
        server.ACTIVE_ANALYSIS = None
        # 默认假装调试浏览器在线，使恢复/检查类测试不依赖真实浏览器；
        # 需要验证「浏览器断开」路径的测试自行覆盖成 cdp=False。
        server.cdp_health = lambda: {"cdp": True, "browser": "test"}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        server.ROOT = self.original["ROOT"]
        server.STORE = self.original["STORE"]
        server.EXPORT_DIR = self.original["EXPORT_DIR"]
        server.ANALYSIS_DIR = self.original["ANALYSIS_DIR"]
        server.ANALYSIS_JOBS = self.original["ANALYSIS_JOBS"]
        server.ACTIVE_ANALYSIS = self.original["ACTIVE_ANALYSIS"]
        server.collect = self.original["collect"]
        server.cdp_health = self.original["cdp_health"]
        server.inspect_product_page = self.original["inspect"]
        server.find_tab = self.original["find_tab"]
        server.focus_tab = self.original["focus_tab"]
        server.open_foreground_tab = self.original["open_foreground_tab"]
        self.tmp.cleanup()

    # ---------- 辅助 ----------

    def create(self):
        code, body = http(
            "POST",
            f"{self.base}/api/jobs",
            {"url": "https://detail.tmall.com/item.htm?id=123456", "mode": "count", "value": 100},
        )
        self.assertEqual(code, 202, body)
        return body["id"]

    def wait(self, job_id, statuses, timeout=8):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            code, body = http("GET", f"{self.base}/api/jobs/{job_id}")
            last = body
            if body.get("status") in statuses:
                return body
            time.sleep(0.05)
        self.fail(f"等待状态 {statuses} 超时，最后状态：{last}")

    def awaiting_job(self, fake):
        server.collect = fake
        job_id = self.create()
        self.wait(job_id, {"awaiting_user"})
        return job_id

    # ---------- 场景 ----------

    def test_short_share_is_resolved_before_job_is_saved(self):
        text = "https://e.tb.cn/h.Example123?tk=exampletoken CZ321"
        canonical = "https://detail.tmall.com/item.htm?id=123456789012"
        with patch("server.resolve_product_input", return_value=("123456789012", canonical)) as resolve, patch("server._thread_start") as start:
            code, body = http("POST", f"{self.base}/api/jobs", {"url": text, "mode": "time", "value": 1, "unit": "year"})
        self.assertEqual(code, 202, body)
        # 解析时额外注入浏览器兜底，这里只关心“用原始分享文本调用且只调一次”
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[0], text)
        record = server.STORE.get(body["id"])
        self.assertEqual(record["product_id"], "123456789012")
        self.assertEqual(record["product_url"], canonical)
        self.assertEqual(body["product_url"], canonical)
        start.assert_called_once_with(body["id"])

    def test_unresolved_share_does_not_create_a_job(self):
        before = server.STORE.list_recent()
        with patch("server.resolve_product_input", side_effect=server.ProductLinkError("分享短链解析超时")), patch("server._thread_start") as start:
            code, body = http("POST", f"{self.base}/api/jobs", {"url": "https://e.tb.cn/h.test", "mode": "count", "value": 10})
        self.assertEqual(code, 400)
        self.assertIn("解析超时", body["error"])
        self.assertEqual(server.STORE.list_recent(), before)
        start.assert_not_called()

    def test_invalid_scope_does_not_resolve_share(self):
        with patch("server.resolve_product_input") as resolve:
            code, _ = http("POST", f"{self.base}/api/jobs", {"url": "https://e.tb.cn/h.test", "mode": "count", "value": 999})
        self.assertEqual(code, 400)
        resolve.assert_not_called()

    def test_busy_task_does_not_resolve_another_share(self):
        job = server.STORE.create(product_url="https://detail.tmall.com/item.htm?id=123456", product_id="123456",
                                  mode="count", value=10, unit="reviews", scope={})
        with patch("server.resolve_product_input") as resolve:
            code, _ = http("POST", f"{self.base}/api/jobs", {"url": "https://e.tb.cn/h.test", "mode": "count", "value": 10})
        self.assertEqual(code, 409)
        resolve.assert_not_called()
        self.assertEqual(server.STORE.get(job["id"])["status"], "queued")

    def test_normal_collection_completes(self):
        server.collect = FakeCollector(make_result())
        job_id = self.create()
        job = self.wait(job_id, {"completed"})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["summary"]["collection_info"]["matched_count"], 6)
        self.assertTrue(job["download_url"])
        export = Path(job["export_path"])
        self.assertTrue(export.exists())
        saved = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["reviews"]), 6)
        code, listing = http("GET", f"{self.base}/api/jobs/recent")
        self.assertEqual(code, 200)
        self.assertIsNone(listing["active"])

    def test_pause_and_resume_success(self):
        fake = FakeCollector(
            make_result(
                status="awaiting_user",
                intervention={
                    "kind": "platform_validate",
                    "title": "平台要求人工验证",
                    "reason": "需要验证",
                    "steps": ["打开浏览器", "完成验证"],
                    "can_resume": True,
                },
                stop_reason="FAIL_SYS_USER_VALIDATE",
                next_page=2,
            ),
            make_result(),
        )
        job_id = self.awaiting_job(fake)
        job = http("GET", f"{self.base}/api/jobs/{job_id}")[1]
        self.assertTrue(job["can_resume"])
        self.assertEqual(job["intervention"]["kind"], "platform_validate")
        self.assertEqual(job["progress"]["next_page"], 2)

        code, body = http("POST", f"{self.base}/api/jobs/{job_id}/resume", {})
        self.assertEqual(code, 202, body)
        done = self.wait(job_id, {"completed"})
        self.assertEqual(done["resume_count"], 1)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1]["state"]["next_page"], 2)
        self.assertTrue(fake.calls[1]["state"]["reviews"])

        code, body = http("POST", f"{self.base}/api/jobs/{job_id}/resume", {})
        self.assertEqual(code, 409, body)

    def test_resume_rejects_corrupted_checkpoint(self):
        """断点去重集合小于已扫描量时：拒绝恢复，且不丢原始数据。"""
        fake = FakeCollector(
            make_result(
                status="awaiting_user",
                intervention={
                    "kind": "platform_validate",
                    "title": "平台要求人工验证",
                    "reason": "需要验证",
                    "steps": ["打开浏览器"],
                    "can_resume": True,
                },
                next_page=3,
            )
        )
        job_id = self.awaiting_job(fake)
        checkpoint = server.STORE.load_checkpoint(job_id)
        checkpoint["seen"] = ["r0", "r1"]  # 模拟去重集合被截断
        server.STORE.save_checkpoint(job_id, checkpoint)

        code, body = http("POST", f"{self.base}/api/jobs/{job_id}/resume", {})
        self.assertEqual(code, 409, body)
        self.assertEqual(body.get("kind"), "checkpoint_invalid")
        self.assertIn("断点", body["error"])

        job = http("GET", f"{self.base}/api/jobs/{job_id}")[1]
        self.assertEqual(job["status"], "awaiting_user")
        self.assertEqual(job["progress"]["scanned"], 20)
        self.assertEqual(len(fake.calls), 1)  # 没有真的再次进入采集

    def test_resume_when_browser_disconnected(self):
        fake = FakeCollector(make_result(status="awaiting_user", intervention={"can_resume": True}))
        job_id = self.awaiting_job(fake)
        server.cdp_health = lambda: {"cdp": False, "browser": "", "error": "refused"}
        code, body = http("POST", f"{self.base}/api/jobs/{job_id}/resume", {})
        self.assertEqual(code, 409)
        self.assertIn("浏览器未连接", body["error"])
        job = http("GET", f"{self.base}/api/jobs/{job_id}")[1]
        self.assertEqual(job["status"], "awaiting_user")

    def test_cancel_waiting_job(self):
        fake = FakeCollector(make_result(status="awaiting_user", intervention={"can_resume": True}))
        job_id = self.awaiting_job(fake)
        code, body = http("POST", f"{self.base}/api/jobs/{job_id}/cancel", {})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "cancelled")
        self.assertIsNone(body["intervention"])
        # 取消后允许新建任务
        server.collect = FakeCollector(make_result())
        self.create()

    def test_waiting_job_is_not_auto_retried(self):
        fake = FakeCollector(make_result(status="awaiting_user", intervention={"can_resume": True}))
        job_id = self.awaiting_job(fake)
        time.sleep(1.2)
        self.assertEqual(len(fake.calls), 1)
        job = http("GET", f"{self.base}/api/jobs/{job_id}")[1]
        self.assertEqual(job["status"], "awaiting_user")
        self.assertTrue(job["awaiting_since"])
        # 刷新页面后仍能看到同一个任务
        listing = http("GET", f"{self.base}/api/jobs/recent")[1]
        self.assertIsNotNone(listing["active"])
        self.assertEqual(listing["active"]["id"], job_id)

    def test_recheck_returns_checks(self):
        fake = FakeCollector(make_result(status="awaiting_user", intervention={"can_resume": True}))
        job_id = self.awaiting_job(fake)
        captured = {}

        async def fake_inspect(product_url, tab_id="", background=True):
            captured.update({"url": product_url, "tab_id": tab_id})
            return {
                "ok": True,
                "kind": "ok",
                "detail": "",
                "checks": [{"name": "调试浏览器连接", "ok": True, "detail": "可用"}],
                "tab_id": "t1",
                "page": {"href": "https://detail.tmall.com/item.htm?id=123456"},
            }

        server.inspect_product_page = fake_inspect
        code, body = http("POST", f"{self.base}/api/jobs/{job_id}/recheck", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["checks"][0]["name"], "调试浏览器连接")
        self.assertIn("123456", captured["url"])
        self.assertEqual(http("GET", f"{self.base}/api/jobs/{job_id}")[1]["tab_id"], "t1")

    def test_focus_without_browser(self):
        fake = FakeCollector(make_result(status="awaiting_user", intervention={"can_resume": True}))
        job_id = self.awaiting_job(fake)
        server.find_tab = lambda _tab_id: None
        server.open_foreground_tab = lambda _url: None
        code, body = http("POST", f"{self.base}/api/jobs/{job_id}/focus", {})
        self.assertEqual(code, 409)
        self.assertIn("启动", body["error"])

    def test_new_job_blocked_while_waiting(self):
        fake = FakeCollector(make_result(status="awaiting_user", intervention={"can_resume": True}))
        job_id = self.awaiting_job(fake)
        code, body = http(
            "POST",
            f"{self.base}/api/jobs",
            {"url": "https://detail.tmall.com/item.htm?id=999", "mode": "count", "value": 100},
        )
        self.assertEqual(code, 409)
        self.assertEqual(body["active_job"]["id"], job_id)

    def test_import_valid_and_analysis_ready(self):
        payload = {
            "product_id": "654321",
            "product": {"title": "导入商品"},
            "reviews": [
                {"review_id": "a1", "star": 1, "content": "用了过敏"},
                {"review_id": "a2", "star": 2, "content": "没有效果"},
            ],
        }
        code, body = http(
            "POST",
            f"{self.base}/api/import",
            {"content": json.dumps(payload, ensure_ascii=False), "filename": "old.json"},
        )
        self.assertEqual(code, 201, body)
        self.assertTrue(body["imported"])
        self.assertEqual(body["status"], "completed")
        self.assertIn("导入历史数据", body["message"])
        self.assertTrue(Path(body["export_path"]).exists())
        self.assertEqual(body["summary"]["collection_info"]["status"], "imported")

    def test_import_invalid_json(self):
        code, body = http(
            "POST", f"{self.base}/api/import", {"content": "{broken", "filename": "x.json"}
        )
        self.assertEqual(code, 400)
        self.assertIn("JSON", body["error"])

    def test_unknown_job_404(self):
        code, body = http("GET", f"{self.base}/api/jobs/notexist1")
        self.assertEqual(code, 404)

    # ---------- 分析 HTTP 链路：不连接浏览器或外部模型 ----------

    def seed_analysis(self, product_id="123456", title="无效"):
        from analysis import claim_extractor, gap_analyzer, issue_extractor
        from analysis.clustering import CLUSTER_RULE_VERSION
        from analysis.pipeline import clusters_fingerprint, input_digest

        review = {"review_id": "r1", "star": 1, "content": "没有效果"}
        export = server.EXPORT_DIR / f"tmall_{product_id}_test.json"
        export.write_text(json.dumps({"product_id": product_id, "reviews": [review]}), encoding="utf-8")
        digest = input_digest([{**review, "text": "没有效果"}])
        clusters = {
            "params": {"rule_version": CLUSTER_RULE_VERSION},
            "clusters": [{"cluster_id": "C01", "member_issue_ids": ["i1"]}],
        }
        fingerprint = clusters_fingerprint(clusters)
        summary = {
            "product_id": product_id, "analysis_status": "completed", "created_at": title,
            "issue_count": 1, "cluster_count": 1, "claim_count": 1, "gap_count": 1,
            "reviews_with_issues": 1, "errors": [],
            "model": {"prompt_version": issue_extractor.PROMPT_VERSION + ":beauty"},
            "stages": {"claims": "ok", "gaps": "ok"},
            "algorithm_params": {"rule_version": CLUSTER_RULE_VERSION},
            "fingerprints": {"clusters": fingerprint}, "input": {"digest": digest},
        }
        payloads = {
            "summary": summary,
            "quality": {"valid_review_count": 1, "unique_review_count": 1},
            "reviews": {"input_digest": digest, "reviews": [review]},
            "clusters": clusters,
            "extracted": {"reviews": [{"review_id": "r1", "issues": [{"issue_id": "i1", "review_id": "r1"}]}]},
            "flat": {"issues": [{"issue_id": "i1", "review_id": "r1"}]},
            "statistics": {
                "top_problems": [{"cluster_id": "C01", "problem_name": title, "rank": 1}],
                "clusters": [{"cluster_id": "C01", "object": "皮肤", "representative_reviews": [review]}],
            },
            "claims": {"prompt_version": claim_extractor.PROMPT_VERSION, "claims": [
                {"claim_id": "PC1", "normalized_claim": "改善痘印", "claim_group": "G1"}
            ]},
            "gaps": {
                "prompt_version": gap_analyzer.PROMPT_VERSION, "clusters_fingerprint": fingerprint,
                "gaps": [{"gap_id": "GAP1", "claim_id": "PC1", "gap_type": "potential_conflict"}],
            },
        }
        paths = server._analysis_paths(product_id)
        paths["base"].mkdir(parents=True, exist_ok=True)
        for key, value in payloads.items():
            paths[key].write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return summary

    def wait_analysis(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, job = http("GET", f"{self.base}/api/analysis/{job_id}")
            self.assertEqual(code, 200)
            if job["status"] in {"success", "failed", "blocked"}:
                return job
            time.sleep(0.02)
        self.fail("分析任务未结束")

    def test_analysis_create_complete_result_and_download(self):
        self.seed_analysis()
        server._analysis_paths("123456")["summary"].unlink()  # 尚未生成过分析结果
        def fake_pipeline(**kwargs):
            kwargs["progress"]({"step": "gaps"})
            return self.seed_analysis(title="本轮结果")
        with patch("analysis.llm_client.LLMConfig.from_env", return_value=object()), patch(
            "analysis.pipeline.run_pipeline", side_effect=fake_pipeline
        ) as pipeline:
            code, created = http("POST", f"{self.base}/api/analysis", {"product_id": "123456"})
            self.assertEqual(code, 202, created)
            job = self.wait_analysis(created["id"])
            self.assertEqual(job["status"], "success", job)
            self.assertEqual(job["step"], "gaps")
            code, result = http("GET", self.base + job["result_url"])
            self.assertEqual(code, 200)
            self.assertTrue(result["freshness"]["is_current"])
            self.assertEqual(result["top_problems"][0]["problem_name"], "本轮结果")
            self.assertEqual(len(result["gaps"]), 1)
            code, bundle = http("GET", f"{self.base}/api/analysis/{created['id']}/download")
            self.assertEqual(code, 200)
            self.assertTrue(bundle["freshness"]["is_current"])
            self.assertEqual(bundle["summary"]["created_at"], "本轮结果")
            pipeline.assert_called_once()

    def test_analysis_existing_result_and_forced_rerun(self):
        self.seed_analysis(title="旧结果")
        with patch("analysis.llm_client.LLMConfig.from_env", return_value=object()), patch(
            "analysis.pipeline.run_pipeline", side_effect=lambda **kwargs: self.seed_analysis(title="重跑结果")
        ) as pipeline:
            code, existing = http("POST", f"{self.base}/api/analysis", {"product_id": "123456"})
            self.assertEqual((code, existing["status"]), (200, "exists"))
            pipeline.assert_not_called()
            code, created = http("POST", f"{self.base}/api/analysis", {"product_id": "123456", "force": True})
            self.assertEqual(code, 202, created)
            self.assertEqual(self.wait_analysis(created["id"])["status"], "success")
            result = http("GET", f"{self.base}/api/analysis/product/123456")[1]
            self.assertEqual(result["generated_at"], "重跑结果")
            pipeline.assert_called_once()

    def test_analysis_concurrent_request_is_rejected(self):
        self.seed_analysis()
        entered, release = threading.Event(), threading.Event()
        def fake_pipeline(**kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("测试未释放分析线程")
            return self.seed_analysis()
        with patch("analysis.llm_client.LLMConfig.from_env", return_value=object()), patch(
            "analysis.pipeline.run_pipeline", side_effect=fake_pipeline
        ) as pipeline:
            try:
                code, created = http("POST", f"{self.base}/api/analysis", {"product_id": "123456", "force": True})
                self.assertEqual(code, 202, created)
                self.assertTrue(entered.wait(2))
                code, denied = http("POST", f"{self.base}/api/analysis", {"product_id": "123456", "force": True})
                self.assertEqual(code, 409, denied)
                self.assertEqual(pipeline.call_count, 1)
            finally:
                release.set()
            self.assertEqual(self.wait_analysis(created["id"])["status"], "success")

    def test_analysis_old_prompt_is_historical_in_result_and_download(self):
        self.seed_analysis()
        path = server._analysis_paths("123456")["gaps"]
        gaps = json.loads(path.read_text(encoding="utf-8"))
        gaps["prompt_version"] = "gap-analyze-obsolete"
        path.write_text(json.dumps(gaps), encoding="utf-8")
        code, result = http("GET", f"{self.base}/api/analysis/product/123456")
        self.assertEqual(code, 200)
        self.assertEqual(result["status"], "stale_or_incomplete")
        self.assertFalse(result["freshness"]["is_current"])
        self.assertTrue(any("落差判断版本" in reason for reason in result["freshness"]["reasons"]))
        self.assertFalse(server._analysis_ready("123456"))
        code, bundle = http("GET", f"{self.base}/api/analysis/product/123456/download")
        self.assertEqual(code, 200)
        self.assertFalse(bundle["freshness"]["is_current"])
        self.assertEqual(bundle["gaps"]["prompt_version"], "gap-analyze-obsolete")

    def test_analysis_incomplete_output_cannot_finish_successfully(self):
        self.seed_analysis()
        def fake_pipeline(**kwargs):
            summary = self.seed_analysis()
            server._analysis_paths("123456")["gaps"].unlink()
            return summary
        with patch("analysis.llm_client.LLMConfig.from_env", return_value=object()), patch(
            "analysis.pipeline.run_pipeline", side_effect=fake_pipeline
        ):
            code, created = http("POST", f"{self.base}/api/analysis", {"product_id": "123456", "force": True})
            self.assertEqual(code, 202, created)
            job = self.wait_analysis(created["id"])
            self.assertEqual(job["status"], "failed", job)
            self.assertIn("完整性检查", job["error"])
            self.assertEqual(http("GET", f"{self.base}/api/analysis/{created['id']}/download")[0], 409)
        result = http("GET", f"{self.base}/api/analysis/product/123456")[1]
        self.assertFalse(result["freshness"]["is_current"])
        self.assertTrue(any("claim_gaps.json" in reason for reason in result["freshness"]["reasons"]))

    def test_analysis_old_cluster_rule_is_rejected_in_clusters_or_summary(self):
        for key, field in (("clusters", "params"), ("summary", "algorithm_params")):
            for rule_version in ("component-compat-v1", None):
                with self.subTest(artifact=key, rule_version=rule_version):
                    self.seed_analysis()
                    path = server._analysis_paths("123456")[key]
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if rule_version is None:
                        payload[field].pop("rule_version")
                    else:
                        payload[field]["rule_version"] = rule_version
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    code, result = http("GET", f"{self.base}/api/analysis/product/123456")
                    self.assertEqual(code, 200)
                    self.assertEqual(result["status"], "stale_or_incomplete")
                    self.assertFalse(result["freshness"]["is_current"])
                    self.assertTrue(any("规则版本" in reason for reason in result["freshness"]["reasons"]))
                    self.assertFalse(server._analysis_ready("123456"))

    def test_analysis_changed_clusters_and_new_reviews_are_stale(self):
        self.seed_analysis()
        paths = server._analysis_paths("123456")
        paths["clusters"].write_text(json.dumps({"clusters": [
            {"cluster_id": "C01", "member_issue_ids": ["i2"]}
        ]}), encoding="utf-8")
        extra_export = server.EXPORT_DIR / "tmall_123456_new.json"
        extra_export.write_text(json.dumps({"product_id": "123456", "reviews": [
            {"review_id": "r2", "star": 1, "content": "皮肤发红"}
        ]}), encoding="utf-8")
        result = http("GET", f"{self.base}/api/analysis/product/123456")[1]
        self.assertFalse(result["freshness"]["is_current"])
        self.assertTrue(any("问题簇" in reason for reason in result["freshness"]["reasons"]))
        self.assertTrue(any("历史评论已有更新" in reason for reason in result["freshness"]["reasons"]))

    def test_analysis_issue_ids_must_match_extraction_and_flat_output(self):
        self.seed_analysis()
        paths = server._analysis_paths("123456")
        paths["extracted"].write_text(json.dumps({"reviews": [{"review_id": "r1", "issues": [
            {"issue_id": "different", "review_id": "r1"}
        ]}]}), encoding="utf-8")
        result = http("GET", f"{self.base}/api/analysis/product/123456")[1]
        self.assertFalse(result["freshness"]["is_current"])
        self.assertTrue(any("ID 未完整一一对应" in reason for reason in result["freshness"]["reasons"]))


if __name__ == "__main__":
    unittest.main()
