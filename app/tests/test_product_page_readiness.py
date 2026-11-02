"""Offline product readiness and resume regressions; no platform or browser calls."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import collector
from intervention import classify_page, item_id_in_href, product_content_ready


PID = "123456"
URL = f"https://detail.tmall.com/item.htm?id={PID}"
TAB = {"id": "paused", "webSocketDebuggerUrl": "ws://127.0.0.1:45123/devtools/page/paused"}


def page(**changes):
    state = {
        "href": URL, "title": "测试商品的具体标题-天猫", "text": "测试商品的具体标题 ¥89 商品详情 评价",
        "ready_state": "interactive", "has_comment_dom": True,
        "has_product_title": True, "has_product_price": True,
        "has_product_image": True, "has_purchase_action": True,
        "has_login_overlay": False, "has_verification_overlay": False,
    }
    state.update(changes)
    return state


def shell(**changes):
    return page(title="商品详情-天猫", text="", has_product_title=False,
                has_product_price=False, has_product_image=False, has_purchase_action=False, **changes)


class ProductPageClassificationTest(unittest.TestCase):
    def test_interactive_product_content_is_available_without_waiting_for_subresources(self):
        result = classify_page(page(), PID)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["diagnostics"]["product_content_ready"])
        self.assertTrue(any("仍在加载" in text for text in result["warnings"]))

    def test_title_and_real_price_are_sufficient_without_comment_placeholder(self):
        snapshot = page(has_product_image=False, has_purchase_action=False, has_comment_dom=False)
        self.assertTrue(product_content_ready(snapshot))
        self.assertTrue(classify_page(snapshot, PID)["ok"])

    def test_title_image_and_purchase_controls_support_delayed_price(self):
        snapshot = page(has_product_price=False)
        self.assertTrue(product_content_ready(snapshot))
        self.assertTrue(classify_page(snapshot, PID)["ok"])

    def test_shells_generic_titles_and_comment_placeholders_are_not_ready(self):
        for snapshot in (shell(), shell(has_comment_dom=True),
                         page(has_product_title=False),
                         page(has_product_price=False, has_purchase_action=False),
                         page(has_product_price=False, has_product_image=False)):
            with self.subTest(snapshot=snapshot):
                result = classify_page(snapshot, PID)
                self.assertFalse(result["ok"])
                self.assertEqual(result["kind"], "page_not_loaded")

    def test_complete_with_explicit_empty_content_signals_is_still_a_shell(self):
        result = classify_page(shell(ready_state="complete"), PID)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "page_not_loaded")

    def test_loading_and_unknown_state_do_not_pass_even_with_content(self):
        for ready_state in ("loading", "uninitialized", "unknown"):
            with self.subTest(ready_state=ready_state):
                result = classify_page(page(ready_state=ready_state), PID)
                self.assertFalse(result["ok"])
                self.assertEqual(result["kind"], "page_not_loaded")

    def test_verification_and_login_have_priority_over_loaded_product_content(self):
        cases = [
            (page(has_verification_overlay=True), "platform_validate"),
            (page(title="安全验证"), "platform_validate"),
            (page(href=URL + "&_____tmd_____=challenge"), "platform_validate"),
            (page(has_login_overlay=True), "login_required"),
            (page(href="https://login.taobao.com/member/login.jhtml?id=" + PID), "login_required"),
        ]
        for snapshot, kind in cases:
            with self.subTest(kind=kind, href=snapshot["href"]):
                result = classify_page(snapshot, PID)
                self.assertFalse(result["ok"])
                self.assertEqual(result["kind"], kind)

    def test_nonblocking_login_prompt_does_not_hide_ready_product(self):
        result = classify_page(page(text="亲，请登录 测试商品的具体标题 ¥89", has_login_overlay=False), PID)
        self.assertTrue(result["ok"])

    def test_wrong_item_and_nonofficial_or_error_urls_do_not_pass(self):
        result = classify_page(page(href="https://detail.tmall.com/item.htm?id=999999"), PID)
        self.assertEqual(result["kind"], "wrong_product")
        for href in ("https://example.com/item.htm?id=" + PID,
                     "https://detail.tmall.com.evil.example/item.htm?id=" + PID,
                     "chrome-error://chromewebdata/?id=" + PID):
            with self.subTest(href=href):
                result = classify_page(page(href=href), PID)
                self.assertFalse(result["ok"])
                self.assertEqual(result["kind"], "page_unexpected")

    def test_unavailable_product_is_not_hidden_by_ready_signals(self):
        result = classify_page(page(text="很抱歉，您查看的宝贝不存在"), PID)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "product_unavailable")

    def test_id_lookup_does_not_read_an_advertising_parameter(self):
        href = "https://detail.tmall.com/item.htm?adid=" + PID + "&id=999999"
        self.assertEqual(item_id_in_href(href), "999999")
        self.assertEqual(classify_page(page(href=href), PID)["kind"], "wrong_product")
        self.assertIsNone(item_id_in_href("https://detail.tmall.com/item.htm?adid=" + PID))

    def test_conflicting_repeated_or_aliased_ids_are_rejected(self):
        for query in (f"id={PID}&id=999999", f"id={PID}&itemId=999999", f"item_id={PID}&id=999999"):
            with self.subTest(query=query):
                href = "https://detail.tmall.com/item.htm?" + query
                self.assertIsNone(item_id_in_href(href))
                self.assertFalse(classify_page(page(href=href), PID)["ok"])


class ProductReadinessPollingTest(unittest.IsolatedAsyncioTestCase):
    async def test_ready_interactive_returns_without_sleeping(self):
        ready = page()
        with patch.object(collector, "read_page_state", AsyncMock(return_value=ready)) as read, patch.object(
            collector.asyncio, "sleep", AsyncMock()
        ) as sleep:
            result = await collector.wait_page_ready(object())
        self.assertIs(result, ready)
        read.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_interactive_and_complete_shells_wait_for_core_content(self):
        for shell_state in ("interactive", "complete"):
            with self.subTest(shell_state=shell_state), patch.object(
                collector, "read_page_state", AsyncMock(side_effect=[shell(ready_state=shell_state), page()])
            ) as read, patch.object(collector.asyncio, "sleep", AsyncMock()) as sleep:
                result = await collector.wait_page_ready(object())
                self.assertTrue(result["has_product_title"])
                self.assertEqual(read.await_count, 2)
                sleep.assert_awaited_once_with(0.5)

    async def test_empty_shell_timeout_does_not_claim_page_is_ready(self):
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0.2, 1.2]))
        with patch.object(collector, "time", clock), patch.object(
            collector, "read_page_state", AsyncMock(return_value=shell())
        ) as read, patch.object(collector.asyncio, "sleep", AsyncMock()) as sleep:
            result = await collector.wait_page_ready(object(), timeout=1)
        self.assertEqual(read.await_count, 2)
        sleep.assert_awaited_once()
        self.assertFalse(classify_page(result, PID)["ok"])

    async def test_gate_returns_immediately_then_is_classified_as_blocked(self):
        for snapshot, kind in ((page(has_login_overlay=True), "login_required"),
                               (shell(has_verification_overlay=True), "platform_validate")):
            with self.subTest(kind=kind), patch.object(
                collector, "read_page_state", AsyncMock(return_value=snapshot)
            ), patch.object(collector.asyncio, "sleep", AsyncMock()) as sleep:
                result = await collector.wait_page_ready(object())
                sleep.assert_not_awaited()
                self.assertEqual(classify_page(result, PID)["kind"], kind)


class _FakeCdp:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.eval = AsyncMock(side_effect=lambda *_a, **_kw: dict(self.snapshot))
        self.call = AsyncMock(return_value={})
        self.navigate = AsyncMock(side_effect=self._navigate)

    async def _navigate(self, *_args, **_kwargs):
        self.snapshot = page()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _fake_tmall(cdp):
    return SimpleNamespace(normalize_detail_url=Mock(return_value=(PID, URL)), CDPClient=Mock(return_value=cdp))


class ProductNavigationTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_product_with_tracking_or_sku_is_not_refreshed(self):
        cdp = _FakeCdp(page(href=URL + "&skuId=456&spm=test"))
        for _ in range(2):
            self.assertFalse(await collector.navigate_product_if_needed(cdp, PID, URL))
        cdp.navigate.assert_not_awaited()

    async def test_other_or_ambiguous_targets_navigate_to_the_expected_product(self):
        for href in ("about:blank", "https://detail.tmall.com/item.htm?id=999999",
                     "https://example.com/item.htm?id=" + PID,
                     "https://detail.tmall.com/item.htm?adid=" + PID + "&id=999999",
                     URL + "&id=999999"):
            with self.subTest(href=href):
                cdp = _FakeCdp(page(href=href))
                self.assertTrue(await collector.navigate_product_if_needed(cdp, PID, URL))
                cdp.navigate.assert_awaited_once_with(URL, wait_seconds=3.0)

    async def test_rechecking_the_loaded_product_does_not_navigate_or_probe_the_api(self):
        cdp = _FakeCdp(page())
        with patch.object(collector, "tmall", _fake_tmall(cdp)), patch.object(
            collector, "get_cdp_host", return_value="http://127.0.0.1:45123"
        ), patch("requests.Session"), patch.object(
            collector, "_open_tab", AsyncMock(return_value=(TAB, "paused", False))
        ), patch.object(collector, "_peek_href", AsyncMock(return_value=URL)), patch.object(
            collector, "_close_tab", AsyncMock()
        ) as close, patch.object(collector, "probe_rate_access", AsyncMock()) as probe:
            for _ in range(2):
                result = await collector.inspect_product_page(URL, tab_id="paused")
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["tab_id"], "paused")
        cdp.navigate.assert_not_awaited()
        close.assert_not_awaited()
        probe.assert_not_awaited()

    async def test_recheck_preserves_a_login_overlay_on_the_same_product(self):
        cdp = _FakeCdp(page(has_login_overlay=True))
        with patch.object(collector, "tmall", _fake_tmall(cdp)), patch.object(
            collector, "get_cdp_host", return_value="http://127.0.0.1:45123"
        ), patch("requests.Session"), patch.object(
            collector, "_open_tab", AsyncMock(return_value=(TAB, "paused", False))
        ), patch.object(collector, "_peek_href", AsyncMock(return_value=URL)), patch.object(
            collector, "_close_tab", AsyncMock()
        ) as close:
            result = await collector.inspect_product_page(URL, tab_id="paused")
        self.assertEqual(result["kind"], "login_required")
        cdp.navigate.assert_not_awaited()
        close.assert_not_awaited()

    async def test_collect_resume_reuses_page_and_probes_checkpoint_page_once(self):
        cdp = _FakeCdp(page())
        state = collector.new_state(PID, URL, collector.normalize_scope("count", 100, "reviews"), 50, 300)
        state.update(tab_id="paused", tab_owned=False, next_page=3)
        checkpoint = collector.snapshot(state)
        payload = {"ret": ["SUCCESS"], "data": {"rateList": []}}
        access = {"status": collector.ACCESS_READY, "page_no": 3, "payload": payload,
                  "count": 0, "product_match": True, "reason_code": "", "detail": "offline ready"}
        background_worker = Mock()
        background_worker.is_alive.return_value = False
        with patch.object(collector, "tmall", _fake_tmall(cdp)), patch.object(
            collector, "_open_tab", AsyncMock(return_value=(TAB, "paused", False))
        ), patch.object(collector, "threading", SimpleNamespace(Thread=Mock(return_value=background_worker))), patch.object(
            collector, "probe_rate_access", AsyncMock(return_value=access)
        ) as probe, patch.object(
            collector, "_run_pages", AsyncMock(return_value=("cancelled", "offline verification completed"))
        ) as run_pages, patch.object(collector, "_close_tab", AsyncMock()):
            result = await collector.collect(URL, mode="count", value=100, unit="reviews", state=checkpoint)
        cdp.navigate.assert_not_awaited()
        self.assertEqual(result["collection_info"]["status"], "cancelled")
        probe.assert_awaited_once_with(cdp, PID, 3, 50, URL)
        run_pages.assert_awaited_once()
        self.assertEqual(run_pages.await_args.kwargs["prime"]["page_no"], 3)
        self.assertIs(run_pages.await_args.kwargs["prime"]["payload"], payload)


if __name__ == "__main__":
    unittest.main()
