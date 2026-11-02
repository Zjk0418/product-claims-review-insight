"""本轮修复的回归测试：断点归属、采集中途断线、检查不关暂停页、重启后孤儿任务。

这些测试全部使用假 CDP / 假采集器，不访问真实平台。
真实浏览器验收结果见《功能说明文档.md》第 15 节。
"""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import collector
import server
from job_store import JobStore


# ---------- 假 CDP ----------

class FakeCDP:
    """最简 CDP：eval 直接返回给定的页面状态。"""

    def __init__(self, page_state=None, fail_on_call=False):
        self.page_state = page_state or {}
        self.fail_on_call = fail_on_call

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def call(self, *_args, **_kwargs):
        if self.fail_on_call:
            raise ConnectionError("websocket closed")
        return {}

    async def navigate(self, *_args, **_kwargs):
        if self.fail_on_call:
            raise ConnectionError("websocket closed")
        return {}

    async def eval(self, *_args, **_kwargs):
        return self.page_state


class _Resp:
    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.trust_env = False

    def get(self, *_args, **_kwargs):
        return _Resp()


def fake_tmall(cdp_factory=None):
    return SimpleNamespace(
        normalize_detail_url=lambda url: ("123", url),
        CDPClient=cdp_factory or (lambda *_a, **_k: FakeCDP()),
        get_page_ws_url=lambda: "ws://127.0.0.1:9222/devtools/page/t1",
    )


def tab(tab_id="t1"):
    return {"id": tab_id, "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{tab_id}"}


class CollectStateTest(unittest.TestCase):
    def test_mismatched_checkpoint_raises_instead_of_restarting(self):
        """断点商品 ID 与任务不一致时必须报错，不能静默从第一页重采。"""
        state = collector.snapshot(
            collector.new_state(
                "123",
                "https://detail.tmall.com/item.htm?id=123",
                collector.normalize_scope("count", 100, "reviews"),
                50,
                300,
            )
        )
        state["product_id"] = "999"
        with mock.patch.object(collector, "tmall", fake_tmall()):
            with self.assertRaises(ValueError) as ctx:
                asyncio.run(
                    collector.collect(
                        product_url="https://detail.tmall.com/item.htm?id=123",
                        mode="count",
                        value=100,
                        state=state,
                    )
                )
        self.assertIn("不一致", str(ctx.exception))

    def test_midrun_connection_lost_pauses_with_checkpoint(self):
        """采集中途浏览器断开 → awaiting_user（可继续），不是 failed。"""

        def raiser(*_a, **_k):
            raise ConnectionError("websocket closed")

        saved: list[dict] = []
        with mock.patch.object(collector, "tmall", fake_tmall(raiser)), mock.patch.object(
            collector, "_open_tab", new=mock.AsyncMock(return_value=(tab(), "t1", True))
        ):
            result = asyncio.run(
                collector.collect(
                    product_url="https://detail.tmall.com/item.htm?id=123",
                    mode="count",
                    value=100,
                    on_checkpoint=saved.append,
                )
            )
        info = result["collection_info"]
        self.assertEqual(info["status"], "awaiting_user")
        self.assertIsNotNone(result["intervention"])
        self.assertEqual(result["intervention"]["kind"], "browser_disconnected")
        self.assertTrue(result["intervention"]["can_resume"])
        self.assertTrue(saved, "暂停前必须落一次断点")


class RunPagesDisconnectTest(unittest.TestCase):
    """翻页中途断线：必须是可继续的 browser_disconnected，而不是 failed。"""

    def test_page_request_disconnect(self):
        class BrokenTmall:
            def __init__(self):
                self.calls = []

            def is_success(self, payload):
                return "SUCCESS" in self.ret_text(payload)

            def is_validate_error(self, payload):
                return False

            def ret_text(self, payload):
                return " ".join(payload.get("ret") or [])

            def get_validate_url(self, payload):
                return ""

            async def request_page_with_retry(self, **kwargs):
                page_no = int(kwargs["page_no"])
                self.calls.append(page_no)
                if page_no == 1:
                    return {
                        "ret": ["SUCCESS::ok"],
                        "data": {
                            "rateList": [
                                {
                                    "id": "r1",
                                    "extraInfoMap": {"userGrade": 2},
                                    "rateType": "-1",
                                    "feedbackDate": "2026年10月20日",
                                    "feedback": "不好用",
                                }
                            ],
                            "total": 100,
                            "totalPage": 2,
                            "hasNext": True,
                        },
                    }
                raise ConnectionError("websocket closed")

        state = collector.new_state(
            "123",
            "https://detail.tmall.com/item.htm?id=123",
            collector.normalize_scope("count", 100, "reviews"),
            50,
            300,
        )
        saved = []
        fake = BrokenTmall()
        with mock.patch.object(collector, "tmall", fake):
            status, reason = asyncio.run(
                collector._run_pages(
                    FakeCDP(), state, None, 0, 0, lambda *_a: None, saved.append, None
                )
            )
        self.assertEqual(status, "browser_disconnected", reason)
        self.assertIn("连接中断", reason)
        self.assertEqual(state["next_page"], 2)
        self.assertEqual(saved[-1]["next_page"], 2)
        self.assertEqual(len(state["reviews"]), 1)


class ResumeCountTest(unittest.TestCase):
    def test_resume_count_written_back(self):
        """从断点恢复后，导出里的 resume_count 必须递增。"""
        base = collector.new_state(
            "123",
            "https://detail.tmall.com/item.htm?id=123",
            collector.normalize_scope("count", 100, "reviews"),
            50,
            300,
        )
        base["reviews"] = [{"review_id": "r1", "star": 2, "content": "不好"}]
        base["seen"] = ["r1"]
        base["scanned"] = 1
        base["resume_count"] = 1
        checkpoint = collector.snapshot(base)

        with mock.patch.object(collector, "tmall", fake_tmall()), mock.patch.object(
            collector, "_open_tab", new=mock.AsyncMock(side_effect=ConnectionError("down"))
        ):
            result = asyncio.run(
                collector.collect(
                    product_url="https://detail.tmall.com/item.htm?id=123",
                    mode="count",
                    value=100,
                    state=checkpoint,
                    on_checkpoint=lambda _c: None,
                )
            )
        self.assertEqual(result["collection_info"]["resume_count"], 2)
        self.assertEqual(result["collection_info"]["matched_count"], 1)


class InspectTabTest(unittest.TestCase):
    def test_recheck_keeps_paused_tab(self):
        """检查通过时不能关闭暂停时留下的标签页（用户可能刚在上面完成验证）。"""
        page_state = {
            "href": "https://detail.tmall.com/item.htm?id=123",
            "title": "商品详情",
            "ready_state": "complete",
            "text": "评价",
            "has_comment_dom": True,
        }
        close_mock = mock.AsyncMock()
        with mock.patch("requests.Session", _Session), mock.patch.object(
            collector, "tmall", fake_tmall(lambda *_a, **_k: FakeCDP(page_state))
        ), mock.patch.object(
            collector, "_open_tab", new=mock.AsyncMock(return_value=(tab(), "t1", True))
        ), mock.patch.object(
            collector, "_close_tab", new=close_mock
        ):
            result = asyncio.run(
                collector.inspect_product_page(
                    "https://detail.tmall.com/item.htm?id=123", tab_id="t1"
                )
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tab_id"], "t1")
        close_mock.assert_not_awaited()

    def test_recheck_reports_wrong_product(self):
        page_state = {
            "href": "https://detail.tmall.com/item.htm?id=999",
            "title": "别的商品",
            "ready_state": "complete",
            "text": "",
            "has_comment_dom": True,
        }
        with mock.patch("requests.Session", _Session), mock.patch.object(
            collector, "tmall", fake_tmall(lambda *_a, **_k: FakeCDP(page_state))
        ), mock.patch.object(
            collector, "_open_tab", new=mock.AsyncMock(return_value=(tab(), "t1", True))
        ), mock.patch.object(
            collector, "_close_tab", new=mock.AsyncMock()
        ):
            result = asyncio.run(
                collector.inspect_product_page(
                    "https://detail.tmall.com/item.htm?id=123", tab_id="t1"
                )
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "wrong_product")
        id_check = [c for c in result["checks"] if c["name"] == "商品 ID 一致"][0]
        self.assertFalse(id_check["ok"])


def http(method, url, payload=None):
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


class JobStoreApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_store = server.STORE
        self.original_export = server.EXPORT_DIR
        self.original_health = server.cdp_health
        self.original_collect = server.collect
        self.original_find_tab = server.find_tab
        self.original_open_tab = server.open_foreground_tab
        self.original_focus_tab = server.focus_tab
        server.STORE = JobStore(self.root)
        server.EXPORT_DIR = self.root / "exports"
        server.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        server.STORE = self.original_store
        server.EXPORT_DIR = self.original_export
        server.cdp_health = self.original_health
        server.collect = self.original_collect
        server.find_tab = self.original_find_tab
        server.open_foreground_tab = self.original_open_tab
        server.focus_tab = self.original_focus_tab
        self.tmp.cleanup()

    def _job(self, product_id="123456"):
        return server.STORE.create(
            product_url=f"https://detail.tmall.com/item.htm?id={product_id}",
            product_id=product_id,
            mode="count",
            value=100,
            unit="reviews",
            scope={"mode": "count", "count": 100},
        )

    def test_resume_rejects_checkpoint_of_other_product(self):
        record = self._job("123456")
        server.STORE.update(record["id"], status="awaiting_user", stage="等待人工介入")
        server.STORE.save_checkpoint(
            record["id"],
            {"version": 1, "product_id": "999999", "reviews": [], "seen": [], "next_page": 3},
        )
        server.cdp_health = lambda: {"cdp": True, "browser": "test"}
        code, body = http("POST", f"{self.base}/api/jobs/{record['id']}/resume", {})
        self.assertEqual(code, 409, body)
        self.assertIn("不一致", body["error"])
        record_after = server.STORE.get(record["id"])
        self.assertEqual(record_after["status"], "awaiting_user")

    def test_focus_opens_saved_validate_url(self):
        """平台验证暂停时，「打开采集浏览器」应直接打开断点里的验证页。"""
        record = self._job()
        server.STORE.update(
            record["id"],
            status="awaiting_user",
            stage="等待人工介入",
            intervention={"kind": "platform_validate", "can_resume": True},
            tab_id="",
        )
        server.STORE.save_checkpoint(
            record["id"],
            {
                "version": 1,
                "product_id": "123456",
                "validate_url": "https://h5api.m.tmall.com/punish?x5secdata=abc",
                "reviews": [],
                "seen": [],
                "next_page": 1,
            },
        )
        server.find_tab = lambda _tab_id: None
        captured = {}

        def fake_open(url):
            captured["url"] = url
            return {"id": "new-tab-1"}

        server.open_foreground_tab = fake_open
        code, body = http("POST", f"{self.base}/api/jobs/{record['id']}/focus", {})
        self.assertEqual(code, 200, body)
        self.assertIn("punish", captured["url"])
        self.assertEqual(server.STORE.get(record["id"])["tab_id"], "new-tab-1")

    def test_orphan_job_becomes_resumable(self):
        """服务重启后，遗留的 collecting 任务应转为可继续，而不是永远卡住。"""
        record = self._job()
        server.STORE.update(record["id"], status="collecting", stage="读取评价分页")
        server.STORE.save_checkpoint(
            record["id"],
            {"version": 1, "product_id": "123456", "reviews": [], "seen": [], "next_page": 4},
        )
        recovered = server.recover_orphaned_jobs()
        self.assertIn(record["id"], recovered)
        job = server.STORE.get(record["id"])
        self.assertEqual(job["status"], "awaiting_user")
        self.assertEqual(job["intervention"]["kind"], "interrupted")
        self.assertTrue(job["intervention"]["can_resume"])

    def test_orphan_without_checkpoint_marked_failed(self):
        record = self._job()
        server.STORE.update(record["id"], status="collecting")
        server.recover_orphaned_jobs()
        job = server.STORE.get(record["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("断点", job["last_error"])

    def test_active_orphan_does_not_block_after_recovery(self):
        record = self._job()
        server.STORE.update(record["id"], status="collecting")
        server.STORE.save_checkpoint(
            record["id"],
            {"version": 1, "product_id": "123456", "reviews": [], "seen": [], "next_page": 2},
        )
        server.recover_orphaned_jobs()
        # 恢复后仍是 awaiting_user，新任务应被挡住并指回这个任务
        code, body = http(
            "POST",
            f"{self.base}/api/jobs",
            {"url": "https://detail.tmall.com/item.htm?id=123456", "mode": "count", "value": 50},
        )
        self.assertEqual(code, 409)
        self.assertEqual(body["active_job"]["id"], record["id"])


if __name__ == "__main__":
    unittest.main()
