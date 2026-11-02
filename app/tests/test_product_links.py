"""Resolve official product shares without live network or unrelated numeric IDs."""
from contextlib import contextmanager
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import quote

import requests

from product_links import UNRESOLVED, ProductLinkError, resolve_product_input


PRODUCT_ID = "123456789012"
DETAIL = f"https://detail.tmall.com/item.htm?id={PRODUCT_ID}"
SHORT = "https://e.tb.cn/h.Example123?tk=exampletoken"


class FakeResponse:
    """Only streaming content is available, so accidental unbounded reads fail."""

    def __init__(self, body="", status=200, headers=None, url=SHORT):
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.url = url
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.close = Mock()
        self.iter_content = Mock(side_effect=self._iter_content)

    def _iter_content(self, chunk_size=32768, **_kwargs):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    @property
    def text(self):
        raise AssertionError("Link resolution must stream bounded content")

    @property
    def content(self):
        raise AssertionError("Link resolution must stream bounded content")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@contextmanager
def fake_network(*responses):
    session = MagicMock(spec=requests.Session)
    session.__enter__.return_value = session
    session.get.side_effect = list(responses)
    with patch("product_links.requests.Session", return_value=session) as factory:
        yield session, factory


def requested_url(call):
    return call.args[0] if call.args else call.kwargs["url"]


class ProductLinksTest(unittest.TestCase):
    def test_pure_id_and_official_complete_links_need_no_network(self):
        values = [PRODUCT_ID, f"  {PRODUCT_ID}  ", DETAIL,
                  f"https://item.taobao.com/item.htm?id={PRODUCT_ID}&skuId=999999999999",
                  f"https://h5.m.taobao.com/awp/core/detail.htm?itemId={PRODUCT_ID}",
                  f"https://detail.tmall.com:443/item.htm?id={PRODUCT_ID}"]
        with fake_network() as (_, factory):
            for value in values:
                with self.subTest(value=value):
                    self.assertEqual(resolve_product_input(value), (PRODUCT_ID, DETAIL))
            factory.assert_not_called()

    def test_complete_share_text_extracts_product_url_not_suffix(self):
        text = f"【淘宝】商品分享，复制这段信息打开淘宝 {DETAIL}&skuId=999999999999 CZ321"
        with fake_network() as (_, factory):
            self.assertEqual(resolve_product_input(text), (PRODUCT_ID, DETAIL))
            factory.assert_not_called()

    def test_original_short_share_strips_cz321_before_request(self):
        response = FakeResponse(f"<script>var url = '{DETAIL}';</script>")
        with fake_network(response) as (session, _):
            self.assertEqual(resolve_product_input(SHORT + " CZ321"), (PRODUCT_ID, DETAIL))
        self.assertEqual(requested_url(session.get.call_args), SHORT)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertTrue(session.get.call_args.kwargs["stream"])
        timeout = session.get.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, tuple)
        self.assertEqual(len(timeout), 2)
        self.assertTrue(all(0 < part <= 20 for part in timeout))
        args, kwargs = response.iter_content.call_args
        self.assertEqual(args[0] if args else kwargs["chunk_size"], 32768)

    def test_full_short_share_with_title_and_closing_punctuation(self):
        text = f"【淘宝】限时优惠 ￥ABCD￥ 「测试商品」 点击链接直接打开：{SHORT}，CZ321"
        response = FakeResponse(f"location.replace('{DETAIL}');")
        with fake_network(response) as (session, _):
            self.assertEqual(resolve_product_input(text), (PRODUCT_ID, DETAIL))
        self.assertEqual(requested_url(session.get.call_args), SHORT)

    def test_http_redirect_chain_stops_at_product_url_without_fetching_product(self):
        other_short = "https://m.tb.cn/h.next"
        first = FakeResponse(status=302, headers={"Location": other_short})
        second = FakeResponse(status=302, headers={"Location": DETAIL})
        with fake_network(first, second) as (session, _):
            self.assertEqual(resolve_product_input(SHORT), (PRODUCT_ID, DETAIL))
        self.assertEqual([requested_url(call) for call in session.get.call_args_list], [SHORT, other_short])

    def test_relative_redirect_uses_current_official_origin(self):
        first = FakeResponse(status=302, headers={"Location": "/h.next"})
        second = FakeResponse(f"location.href='{DETAIL}';", url="https://e.tb.cn/h.next")
        with fake_network(first, second) as (session, _):
            self.assertEqual(resolve_product_input(SHORT), (PRODUCT_ID, DETAIL))
        self.assertEqual(requested_url(session.get.call_args_list[1]), "https://e.tb.cn/h.next")

    def test_javascript_redirect_forms_and_meta_refresh(self):
        snippets = [
            f"<script>var url = 'https://pages-g.m.taobao.com/wow/item/detail?id={PRODUCT_ID}';</script>",
            f"<script>location.replace('{DETAIL}')</script>",
            f'<script>location.assign("{DETAIL}")</script>',
            f"<script>window.location.href = '{DETAIL}'</script>",
            f'<meta http-equiv="refresh" content="0;url={DETAIL}">',
            f'<meta content="0;URL={DETAIL}" http-equiv="refresh">',
        ]
        for snippet in snippets:
            with self.subTest(snippet=snippet), fake_network(FakeResponse(snippet)):
                self.assertEqual(resolve_product_input(SHORT), (PRODUCT_ID, DETAIL))

    def test_html_entities_and_javascript_escapes_are_decoded(self):
        snippets = [
            f"var url = 'https://item.taobao.com/item.htm?foo=x&amp;id={PRODUCT_ID}';",
            r'var url="https:\/\/detail.tmall.com\/item.htm?id=' + PRODUCT_ID + '";',
            r'location.href="https:\u002f\u002fitem.taobao.com/item.htm?foo=x\u0026id=' + PRODUCT_ID + '";',
        ]
        for snippet in snippets:
            with self.subTest(snippet=snippet), fake_network(FakeResponse(snippet)):
                self.assertEqual(resolve_product_input(SHORT), (PRODUCT_ID, DETAIL))

    def test_nested_url_can_be_decoded_more_than_once(self):
        nested = quote(quote(DETAIL, safe=""), safe="")
        value = "https://s.click.taobao.com/t?url=" + nested
        with fake_network() as (_, factory):
            self.assertEqual(resolve_product_input(value), (PRODUCT_ID, DETAIL))
            factory.assert_not_called()

    def test_non_official_input_never_opens_network_even_with_id(self):
        values = [
            f"https://example.com/item.htm?id={PRODUCT_ID}",
            f"http://127.0.0.1/item.htm?id={PRODUCT_ID}",
            f"http://localhost/item.htm?id={PRODUCT_ID}",
            f"https://taobao.com.evil.example/item.htm?id={PRODUCT_ID}",
            f"https://e.tb.cn.evil.example/item.htm?id={PRODUCT_ID}",
            f"https://user:password@item.taobao.com/item.htm?id={PRODUCT_ID}",
            f"https://item.taobao.com:444/item.htm?id={PRODUCT_ID}",
            "file:///C:/Windows/System32/drivers/etc/hosts",
        ]
        with fake_network() as (_, factory):
            for value in values:
                with self.subTest(value=value), self.assertRaises(ProductLinkError):
                    resolve_product_input(value)
            factory.assert_not_called()

    def test_redirect_is_checked_before_id_extraction_or_next_request(self):
        targets = [
            f"http://127.0.0.1/item.htm?id={PRODUCT_ID}",
            f"https://taobao.com.evil.example/item.htm?id={PRODUCT_ID}",
            f"https://user:password@item.taobao.com/item.htm?id={PRODUCT_ID}",
            "https://e.tb.cn:444/h.next",
        ]
        for target in targets:
            with self.subTest(target=target), fake_network(FakeResponse(status=302, headers={"Location": target})) as (session, _):
                with self.assertRaises(ProductLinkError):
                    resolve_product_input(SHORT)
                self.assertEqual(session.get.call_count, 1)

    def test_page_target_is_checked_before_using_its_product_id(self):
        response = FakeResponse(f"location.replace('http://127.0.0.1/item.htm?id={PRODUCT_ID}')")
        with fake_network(response) as (session, _), self.assertRaises(ProductLinkError):
            resolve_product_input(SHORT)
        self.assertEqual(session.get.call_count, 1)

    def test_network_errors_have_product_link_error_contract(self):
        for error in [requests.Timeout("timeout"), requests.ConnectionError("unavailable"), requests.HTTPError("failed")]:
            with self.subTest(error=type(error).__name__), fake_network(error), self.assertRaises(ProductLinkError):
                resolve_product_input(SHORT)

    def test_stream_failure_maps_error_and_closes_response_and_session(self):
        for error in [requests.Timeout("stream timed out"), requests.ConnectionError("stream disconnected")]:
            response = FakeResponse()
            response.iter_content.side_effect = error
            with self.subTest(error=type(error).__name__), fake_network(response) as (session, _):
                with self.assertRaises(ProductLinkError):
                    resolve_product_input(SHORT)
                response.close.assert_called_once()
                session.close.assert_called_once()

    def test_total_time_budget_is_shared_across_redirects(self):
        clock = [0]
        response = FakeResponse(status=302, headers={"Location": "https://e.tb.cn/h.next"})
        with fake_network() as (session, _), patch("product_links.time.monotonic", side_effect=lambda: clock[0]):
            def slow_response(*_args, **_kwargs):
                clock[0] = 21
                return response

            session.get.side_effect = slow_response
            with self.assertRaises(ProductLinkError):
                resolve_product_input(SHORT)
            self.assertEqual(session.get.call_count, 1, "deadline must stop the next network request")
            response.close.assert_called_once()

    def test_expired_http_response_fails_cleanly(self):
        with fake_network(FakeResponse("expired", status=404)), self.assertRaises(ProductLinkError):
            resolve_product_input(SHORT)

    def test_arbitrary_item_id_text_is_not_a_redirect(self):
        snippets = [f"<p>卖家编号 id={PRODUCT_ID}</p>",
                    f"<script>var itemId={PRODUCT_ID};</script>",
                    f'{{"skuId":"{PRODUCT_ID}","text":"id={PRODUCT_ID}"}}']
        for snippet in snippets:
            with self.subTest(snippet=snippet), fake_network(FakeResponse(snippet)), self.assertRaises(ProductLinkError):
                resolve_product_input(SHORT)

    def test_redirect_chain_has_bounded_requests(self):
        responses = [FakeResponse(status=302, headers={"Location": f"https://e.tb.cn/h.next{index}"})
                     for index in range(9)]
        with fake_network(*responses) as (session, _), self.assertRaises(ProductLinkError):
            resolve_product_input(SHORT)
        self.assertLessEqual(session.get.call_count, 6, "at most five redirects after the original request")

    def test_redirect_loop_does_not_keep_requesting(self):
        with fake_network(*[FakeResponse(status=302, headers={"Location": SHORT}) for _ in range(9)]) as (session, _):
            with self.assertRaises(ProductLinkError):
                resolve_product_input(SHORT)
        self.assertLessEqual(session.get.call_count, 6)

    def test_response_larger_than_one_mib_is_rejected(self):
        body = b"x" * (1024 * 1024 + 1)
        with fake_network(FakeResponse(body)), self.assertRaises(ProductLinkError):
            resolve_product_input(SHORT)

    def test_empty_or_non_link_share_text_does_not_open_network(self):
        with fake_network() as (_, factory):
            for text in ["", "   ", "CZ321", "复制口令打开淘宝", "￥ABCD￥"]:
                with self.subTest(text=text), self.assertRaises(ProductLinkError):
                    resolve_product_input(text)
            factory.assert_not_called()

    def test_item_ids_plural_is_recognized_without_network(self):
        """淘系新版中转页把商品 ID 放在 itemIds 参数里。"""
        value = f"https://pages-fast.m.taobao.com/wow/z/app/ltao-fe/tbms-limited-grad/externalDetail?itemIds={PRODUCT_ID}&src=msdetail"
        with fake_network() as (_, factory):
            self.assertEqual(resolve_product_input(value), (PRODUCT_ID, DETAIL))
            factory.assert_not_called()

    def test_browser_resolver_used_when_share_page_has_no_static_target(self):
        """分享页只剩 JS 壳（无 302、无跳转赋值）时，用浏览器兜底。"""
        shell = "<html><script src='//g.alicdn.com/app.js'></script><body></body></html>"
        resolver = Mock(return_value=PRODUCT_ID)
        with fake_network(FakeResponse(shell)):
            result = resolve_product_input(SHORT, browser_resolver=resolver)
        self.assertEqual(result, (PRODUCT_ID, DETAIL))
        resolver.assert_called_once_with(SHORT)

    def test_browser_resolver_not_called_when_http_resolves(self):
        resolver = Mock(return_value="999999999999")
        with fake_network(FakeResponse(f"location.replace('{DETAIL}')")):
            self.assertEqual(resolve_product_input(SHORT, browser_resolver=resolver), (PRODUCT_ID, DETAIL))
        resolver.assert_not_called()

    def test_browser_resolver_failure_keeps_original_error(self):
        shell = "<html><script src='//g.alicdn.com/app.js'></script></html>"
        for resolver in (Mock(return_value=""), Mock(return_value=None), Mock(side_effect=RuntimeError("cdp down"))):
            with self.subTest(resolver=resolver), fake_network(FakeResponse(shell)):
                with self.assertRaises(ProductLinkError) as ctx:
                    resolve_product_input(SHORT, browser_resolver=resolver)
                self.assertEqual(str(ctx.exception), UNRESOLVED)

    def test_browser_resolver_not_used_for_non_official_input(self):
        resolver = Mock(return_value=PRODUCT_ID)
        with fake_network() as (_, factory):
            with self.assertRaises(ProductLinkError):
                resolve_product_input(f"https://example.com/item.htm?id={PRODUCT_ID}", browser_resolver=resolver)
            factory.assert_not_called()
        resolver.assert_not_called()

    def test_resolve_share_in_browser_reads_final_url_and_closes_tab(self):
        from product_links import resolve_share_in_browser
        intermediate = "https://pages-fast.m.taobao.com/wow/z/app/ltao-fe/xsms-ssr-yyds/external?x-ssr=true"
        final = f"https://pages-fast.m.taobao.com/wow/z/app/ltao-fe/tbms-limited-grad/externalDetail?itemIds={PRODUCT_ID}&src=msdetail"
        healthy = MagicMock()
        healthy.raise_for_status.return_value = None
        session = MagicMock(spec=requests.Session)
        session.get.return_value = healthy
        with patch("product_links.requests.Session", return_value=session), \
             patch("cdp_tabs.open_background_tab", return_value={"id": "TAB1"}) as open_tab, \
             patch("cdp_tabs.find_tab", side_effect=[{"url": intermediate}, {"url": final}]), \
             patch("cdp_tabs.close_background_tab") as close_tab, \
             patch("product_links.time.sleep", return_value=None):
            self.assertEqual(resolve_share_in_browser(SHORT), PRODUCT_ID)
        open_tab.assert_called_once_with(SHORT)
        close_tab.assert_called_once_with("TAB1")

    def test_resolve_share_in_browser_returns_empty_without_browser(self):
        from product_links import resolve_share_in_browser
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("refused")
        with patch("product_links.requests.Session", return_value=session), \
             patch("cdp_tabs.open_background_tab") as open_tab:
            self.assertEqual(resolve_share_in_browser(SHORT), "")
        open_tab.assert_not_called()

    def test_resolve_share_in_browser_stops_early_on_login_page(self):
        from product_links import resolve_share_in_browser
        healthy = MagicMock()
        healthy.raise_for_status.return_value = None
        session = MagicMock(spec=requests.Session)
        session.get.return_value = healthy
        login = "https://login.taobao.com/member/login.jhtml?redirectURL=x"
        with patch("product_links.requests.Session", return_value=session), \
             patch("cdp_tabs.open_background_tab", return_value={"id": "TAB1"}), \
             patch("cdp_tabs.find_tab", return_value={"url": login}) as find, \
             patch("cdp_tabs.close_background_tab") as close_tab, \
             patch("product_links.time.sleep", return_value=None):
            self.assertEqual(resolve_share_in_browser(SHORT), "")
        self.assertEqual(find.call_count, 1, "登录页应立即返回，不轮询到超时")
        close_tab.assert_called_once_with("TAB1")


if __name__ == "__main__":
    unittest.main()
