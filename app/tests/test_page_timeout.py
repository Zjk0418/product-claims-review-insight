"""页面响应超时与浏览器断线的区分（真机回归：TimeoutError 曾被误报成断线）。

用假的 tmall 模块驱动 _run_pages，不访问真实平台。
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import collector
from intervention import build as build_intervention


class FlakyTmall:
    """按「第几次调用某一页」返回结果或抛异常，用来模拟超时/断线。

    script: {页码: [动作1, 动作2, ...]}；动作是 dict 表示返回该响应，
    是 BaseException 实例表示抛出；列表用完或页码不在脚本里就返回空页。
    """

    def __init__(self, script: dict) -> None:
        self.script = {page: list(actions) for page, actions in script.items()}
        self.calls: list[int] = []

    def is_success(self, payload: dict) -> bool:
        return "SUCCESS" in self.ret_text(payload)

    def is_validate_error(self, payload: dict) -> bool:
        return False

    def ret_text(self, payload: dict) -> str:
        ret = payload.get("ret") or []
        return " ".join(ret) if isinstance(ret, list) else str(ret)

    def get_validate_url(self, payload: dict) -> str:
        return ""

    async def request_page_with_retry(self, **kwargs):  # noqa: ANN003
        page_no = int(kwargs["page_no"])
        self.calls.append(page_no)
        actions = self.script.get(page_no)
        if not actions:
            return _page([f"r{page_no}-{len(self.calls)}"], has_next=page_no < 3)
        action = actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class TimeoutCDP:
    async def eval(self, *_args, **_kwargs):  # noqa: ANN002
        return ""


def _page(ids: list[str], has_next: bool = True) -> dict:
    rates = [
        {
            "id": rid,
            "extraInfoMap": {"userGrade": 2},
            "rateType": "-1",
            "feedbackDate": "2026年10月20日",
            "feedback": f"{rid} 内容",
        }
        for rid in ids
    ]
    return {
        "ret": ["SUCCESS::接口调用成功"],
        "data": {"rateList": rates, "total": 100, "totalPage": 10, "hasNext": has_next},
    }


def _state() -> dict:
    scope = collector.normalize_scope("count", 100, "reviews")
    return collector.new_state("123", "https://detail.tmall.com/item.htm?id=123", scope, 50, 300)


def _run(fake: FlakyTmall, state: dict):
    events: list[dict] = []
    checkpoints: list[dict] = []
    with mock.patch.object(collector, "tmall", fake):
        status, reason = asyncio.run(
            collector._run_pages(
                TimeoutCDP(),
                state,
                None,
                0,
                0,
                lambda stage, message: events.append({"stage": stage, "message": message}),
                checkpoints.append,
                None,
            )
        )
    return status, reason, events, checkpoints


class ClassifyErrorTest(unittest.TestCase):
    def test_timeout_is_not_disconnect(self):
        self.assertEqual(collector._classify_request_error(TimeoutError("Page.navigate")), "page_timeout")
        self.assertEqual(collector._classify_request_error(asyncio.TimeoutError()), "page_timeout")

    def test_real_disconnect_detected(self):
        for exc in (
            ConnectionRefusedError(),
            ConnectionResetError(),
            BrokenPipeError(),
            type("ConnectionClosedError", (Exception,), {})(),
        ):
            self.assertEqual(collector._classify_request_error(exc), "browser_disconnected", type(exc).__name__)

    def test_other_errors_are_failures(self):
        for exc in (ValueError("bad"), KeyError("data"), OSError("network down")):
            self.assertEqual(collector._classify_request_error(exc), "failed", type(exc).__name__)


class PageTimeoutRetryTest(unittest.TestCase):
    def test_timeout_retries_same_page_then_succeeds(self):
        fake = FlakyTmall({1: [TimeoutError("Page.navigate")]})
        state = _state()
        status, reason, events, checkpoints = _run(fake, state)
        self.assertEqual(status, "success")
        # 第 1 页第一次超时，重试同一页成功，然后继续第 2、3 页
        self.assertEqual(fake.calls, [1, 1, 2, 3])
        self.assertTrue(any(item["stage"] == "waiting" for item in events))
        self.assertEqual(state["next_page"], 4)
        self.assertTrue(checkpoints)

    def test_repeated_timeout_pauses_without_losing_checkpoint(self):
        fake = FlakyTmall(
            {1: [TimeoutError("x")] * (collector.MAX_PAGE_TIMEOUT_RETRIES + 1)}
        )
        state = _state()
        status, reason, _events, checkpoints = _run(fake, state)
        self.assertEqual(status, "page_timeout")
        self.assertIn("超时", reason)
        self.assertEqual(
            fake.calls, [1] * (collector.MAX_PAGE_TIMEOUT_RETRIES + 1)
        )  # 只重试限额内次数
        self.assertTrue(checkpoints)
        self.assertEqual(checkpoints[-1]["next_page"], 1)
        self.assertEqual(checkpoints[-1]["scanned"], 0)

    def test_disconnect_still_reported_as_disconnect(self):
        closed = type("ConnectionClosedError", (Exception,), {})()
        fake = FlakyTmall({1: [closed]})
        state = _state()
        status, reason, _events, checkpoints = _run(fake, state)
        self.assertEqual(status, "browser_disconnected")
        self.assertIn("连接中断", reason)
        self.assertEqual(fake.calls, [1])  # 断线不重试
        self.assertTrue(checkpoints)


class InterventionTest(unittest.TestCase):
    def test_page_timeout_guide_mentions_page_number(self):
        guide = build_intervention("page_timeout", "123", extra={"next_page": 4})
        self.assertEqual(guide["kind"], "page_timeout")
        self.assertIn("4", guide["reason"])
        self.assertTrue(guide["can_resume"])
        self.assertTrue(any("4" in step for step in guide["steps"]))


class ProbeAccessTest(unittest.TestCase):
    """第二级检查（数据可用性探测）里的异常分类。"""

    def _probe(self, exc: BaseException):
        fake = mock.Mock()
        fake.request_page_with_retry = _raising(exc)
        with mock.patch.object(collector, "tmall", fake):
            return asyncio.run(
                collector.probe_rate_access(
                    TimeoutCDP(), "123", 2, 50, "https://detail.tmall.com/item.htm?id=123"
                )
            )

    def test_probe_timeout_is_not_browser_error(self):
        access = self._probe(TimeoutError("Runtime.evaluate"))
        self.assertEqual(access["status"], collector.ACCESS_PAGE_TIMEOUT)
        self.assertEqual(access["page_no"], 2)

    def test_probe_disconnect_is_browser_error(self):
        access = self._probe(
            type("ConnectionClosedError", (Exception,), {})("no close frame")
        )
        self.assertEqual(access["status"], collector.ACCESS_BROWSER_ERROR)


def _raising(exc: BaseException):
    async def _inner(**_kwargs):  # noqa: ANN003
        raise exc

    return _inner


if __name__ == "__main__":
    unittest.main()
