"""采集翻页与断点续采的单元测试。

用假的 tmall 模块和假 CDP 驱动 _run_pages，不访问真实平台。
真实浏览器恢复流程属于集成测试，见文档「未验证」说明。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime
from unittest import mock

import collector


def rate(review_id: str, star: int = 2, day: int = 20) -> dict:
    return {
        "id": review_id,
        "extraInfoMap": {"userGrade": star},
        "rateType": "0" if star == 3 else "-1",
        "feedbackDate": f"2026年10月{day}日",
        "feedback": f"第 {review_id} 条评价，质量一般",
        "skuValueStr": "默认",
        "skuId": "1",
    }


def success_page(rates: list[dict], total: int = 10, total_page: int = 2, has_next: bool = True) -> dict:
    return {
        "ret": ["SUCCESS::接口调用成功"],
        "data": {
            "rateList": rates,
            "total": total,
            "totalPage": total_page,
            "hasNext": has_next,
        },
    }


def validate_page() -> dict:
    return {
        "ret": ["FAIL_SYS_USER_VALIDATE::哎哟喂,被挤爆啦"],
        "data": {"url": "https://detail.tmall.com/_____tmd_____/punish?x5secdata=abc"},
    }


class FakeTmall:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[int] = []

    def is_success(self, payload: dict) -> bool:
        return "SUCCESS" in self.ret_text(payload)

    def is_validate_error(self, payload: dict) -> bool:
        text = json.dumps(payload, ensure_ascii=False)
        return "FAIL_SYS_USER_VALIDATE" in text or "RGV587" in text

    def ret_text(self, payload: dict) -> str:
        ret = payload.get("ret") or []
        return " ".join(ret) if isinstance(ret, list) else str(ret)

    def get_validate_url(self, payload: dict) -> str:
        return str((payload.get("data") or {}).get("url") or "")

    async def request_page_with_retry(self, **kwargs):  # noqa: ANN003
        page_no = int(kwargs["page_no"])
        self.calls.append(page_no)
        if page_no <= len(self.pages):
            return self.pages[page_no - 1]
        return success_page([], has_next=False)


class FakeCDP:
    async def eval(self, *_args, **_kwargs):  # noqa: ANN002
        return ""


def make_state(item_id: str = "123") -> dict:
    scope = collector.normalize_scope("count", 3, "reviews")
    return collector.new_state(item_id, f"https://detail.tmall.com/item.htm?id={item_id}", scope, 50, 300)


def run_pages(fake: FakeTmall, state: dict, *, cancel=None):
    events: list[dict] = []
    checkpoints: list[dict] = []

    with mock.patch.object(collector, "tmall", fake):
        status, reason = asyncio.run(
            collector._run_pages(
                FakeCDP(),
                state,
                None,
                0,
                0,
                lambda stage, message: events.append({"stage": stage, "message": message}),
                checkpoints.append,
                cancel,
            )
        )
    return status, reason, events, checkpoints


class RunPagesTest(unittest.TestCase):
    def test_normal_success(self):
        fake = FakeTmall([success_page([rate("r1"), rate("r2"), rate("r3")])])
        state = make_state()
        status, reason, events, checkpoints = run_pages(fake, state)
        self.assertEqual(status, "success")
        self.assertEqual([r["review_id"] for r in state["reviews"]], ["r1", "r2", "r3"])
        self.assertEqual(state["scanned"], 3)
        self.assertTrue(checkpoints)
        self.assertTrue(any(item["stage"] == "page_done" for item in events))

    def test_validate_pauses_and_saves_checkpoint(self):
        fake = FakeTmall([success_page([rate("r1"), rate("r2")]), validate_page()])
        state = make_state()
        # 先把 count 调大，让第一页不足以结束
        state["scope"] = collector.normalize_scope("count", 100, "reviews")
        status, reason, _events, checkpoints = run_pages(fake, state)
        self.assertEqual(status, "awaiting_user")
        self.assertIn("VALIDATE", reason)
        self.assertEqual(state["next_page"], 2)
        self.assertEqual(len(state["reviews"]), 2)
        self.assertTrue(checkpoints)
        self.assertEqual(checkpoints[-1]["next_page"], 2)

    def test_resume_continues_without_duplicates(self):
        # 第一轮：第 1 页成功，第 2 页触发验证
        first = FakeTmall([success_page([rate("r1"), rate("r2")]), validate_page()])
        state = make_state()
        state["scope"] = collector.normalize_scope("count", 100, "reviews")
        status, _, _, checkpoints = run_pages(first, state)
        self.assertEqual(status, "awaiting_user")

        restored = collector.restore_state(checkpoints[-1], "123")
        # 第二轮：平台恢复；第 2 页故意重复返回 r1/r2，用来验证去重
        second = FakeTmall(
            [
                success_page([rate("r1"), rate("r2")]),
                success_page([rate("r1"), rate("r2"), rate("r3")], has_next=False),
            ]
        )
        restored["scope"] = collector.normalize_scope("count", 100, "reviews")
        status2, reason2, _, _ = run_pages(second, restored)
        self.assertEqual(status2, "success")
        ids = [r["review_id"] for r in restored["reviews"]]
        self.assertEqual(ids, ["r1", "r2", "r3"])
        self.assertEqual(second.calls, [2])
        self.assertEqual(restored["scanned"], 3)
        self.assertIn("更多评价", reason2)

    def test_cancel_stops_between_pages(self):
        fake = FakeTmall([success_page([rate("r1")]), success_page([rate("r2")])])
        state = make_state()
        state["scope"] = collector.normalize_scope("count", 100, "reviews")
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 3

        status, reason, _, _ = run_pages(fake, state, cancel=cancel)
        self.assertEqual(status, "cancelled")
        self.assertIn("取消", reason)
        self.assertEqual(len(state["reviews"]), 1)

    def test_resume_rejects_other_product(self):
        state = make_state("123")
        checkpoint = collector.snapshot(state)
        checkpoint["product_id"] = "999"
        with self.assertRaises(ValueError):
            collector.restore_state(checkpoint, "123")

    def test_scan_limit_returns_partial(self):
        fake = FakeTmall([success_page([rate("r1"), rate("r2")])])
        state = make_state()
        state["scope"] = collector.normalize_scope("count", 100, "reviews")
        state["max_scan"] = 2
        status, reason, _, _ = run_pages(fake, state)
        self.assertEqual(status, "partial")
        self.assertIn("上限", reason)


class ResultTest(unittest.TestCase):
    def result(self, status="success", reviews=None, stop_reason="完成"):
        st = make_state()
        st["reviews"] = reviews if reviews is not None else [{"review_id": f"r{i}"} for i in range(6)]
        st["scanned"] = 10
        return collector._result(
            product_url="https://detail.tmall.com/item.htm?id=123",
            item_id="123",
            product_info={"product": {"id": "123", "title": "t"}, "collection_info": {}},
            st=st,
            status=status,
            stop_reason=stop_reason,
            started_at=datetime(2026, 10, 24, 12, 0, 0),
            page_size=50,
            matched_without_star=0,
            pages=1,
            intervention=None,
            review_dom_present=True,
        )

    def test_success_with_enough_reviews(self):
        payload = self.result(reviews=[{"review_id": f"r{i}"} for i in range(6)])
        self.assertEqual(payload["collection_info"]["status"], "success")
        self.assertTrue(payload["collection_info"]["review_sufficiency"]["sufficient"])

    def test_partial_when_reviews_insufficient(self):
        payload = self.result(reviews=[{"review_id": "r1"}])
        self.assertEqual(payload["collection_info"]["status"], "partial")
        self.assertFalse(payload["collection_info"]["review_sufficiency"]["sufficient"])
        self.assertIn("样本不足", payload["collection_info"]["stop_reason"])

    def test_awaiting_keeps_status(self):
        payload = self.result(status="awaiting_user", reviews=[{"review_id": "r1"}], stop_reason="验证")
        self.assertEqual(payload["collection_info"]["status"], "awaiting_user")
        self.assertFalse(payload["collection_info"]["is_complete"])


if __name__ == "__main__":
    unittest.main()
