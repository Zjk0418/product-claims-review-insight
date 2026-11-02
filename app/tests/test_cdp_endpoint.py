"""Every desktop collection path must use its owned endpoint, never port 9222."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import cdp_tabs
import collector
import server
from cdp_endpoint import configure_cdp_endpoint, get_cdp_host


class CdpEndpointTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"REVIEW_DESKTOP_BROWSER": "1"})
        self.environment.start()
        configure_cdp_endpoint(None)

    def tearDown(self):
        configure_cdp_endpoint(None)
        self.environment.stop()

    def test_desktop_has_no_default_browser_endpoint(self):
        with self.assertRaisesRegex(RuntimeError, "尚未启动"):
            get_cdp_host()
        configure_cdp_endpoint(45123)
        self.assertEqual(get_cdp_host(), "http://127.0.0.1:45123")
        configure_cdp_endpoint(None)
        with self.assertRaises(RuntimeError):
            get_cdp_host()

    def test_cli_default_is_preserved_and_configuration_can_override_it(self):
        with patch.dict(os.environ, {"REVIEW_DESKTOP_BROWSER": "0"}):
            self.assertEqual(get_cdp_host(), "http://127.0.0.1:9222")
            configure_cdp_endpoint(45124)
            self.assertEqual(get_cdp_host(), "http://127.0.0.1:45124")

    def test_only_valid_integer_ports_can_be_published(self):
        for value in (0, -1, 65536, True, False, 9222.0, "9222", "https://example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configure_cdp_endpoint(value)
        for value in (1, 65535):
            configure_cdp_endpoint(value)
            self.assertEqual(get_cdp_host(), f"http://127.0.0.1:{value}")

    def test_tab_http_routes_follow_current_endpoint(self):
        session = Mock()
        page = {"type": "page", "id": "owned-tab", "webSocketDebuggerUrl": "ws://owned/page"}

        def response_for(url, **_kwargs):
            response = Mock(status_code=200)
            response.json.return_value = ({"webSocketDebuggerUrl": "ws://owned/browser"}
                                          if url.endswith("/version") else [page])
            return response

        session.get.side_effect = response_for
        session.put.return_value.json.return_value = page
        with patch("cdp_tabs._session", return_value=session):
            for port in (45125, 45126):
                configure_cdp_endpoint(port)
                session.reset_mock()
                self.assertEqual(cdp_tabs._browser_ws_url(), "ws://owned/browser")
                self.assertEqual(cdp_tabs._find_page("owned-tab"), page)
                self.assertEqual(cdp_tabs.list_tabs(), [page])
                self.assertTrue(cdp_tabs.focus_tab("owned-tab"))
                self.assertEqual(cdp_tabs._legacy_new_tab(), page)
                cdp_tabs._close("owned-tab")
                for call in session.get.call_args_list + session.put.call_args_list:
                    self.assertTrue(call.args[0].startswith(f"http://127.0.0.1:{port}/json/"), call)
                self.assertEqual(session.get.call_count, 5)
                self.assertEqual(session.put.call_count, 1)

    def test_unconfigured_desktop_routes_do_not_query_personal_browser(self):
        session = Mock()
        with patch("cdp_tabs._session", return_value=session):
            self.assertEqual(cdp_tabs.list_tabs(), [])
            self.assertIsNone(cdp_tabs.find_tab("old-tab"))
            self.assertFalse(cdp_tabs.focus_tab("old-tab"))
            cdp_tabs._close("old-tab")
            for route in (cdp_tabs._browser_ws_url, cdp_tabs._legacy_new_tab):
                with self.assertRaises(RuntimeError):
                    route()
        session.get.assert_not_called()
        session.put.assert_not_called()

    def test_server_health_uses_current_endpoint_and_stays_off_9222_when_unready(self):
        session = Mock()
        session.get.return_value.json.return_value = {"Browser": "owned-browser"}
        with patch("server.requests.Session", return_value=session):
            self.assertFalse(server.cdp_health()["cdp"])
            session.get.assert_not_called()
            configure_cdp_endpoint(45127)
            self.assertTrue(server.cdp_health()["cdp"])
        session.get.assert_called_once_with("http://127.0.0.1:45127/json/version", timeout=3)

    def test_collector_page_check_uses_current_endpoint(self):
        session = Mock()
        session.get.return_value.raise_for_status.side_effect = RuntimeError("stop after health probe")
        configure_cdp_endpoint(45128)
        with patch("requests.Session", return_value=session):
            result = asyncio.run(collector.inspect_product_page("123456"))
        self.assertFalse(result["ok"])
        session.get.assert_called_once_with("http://127.0.0.1:45128/json/version", timeout=3)

    def test_collector_unready_check_does_not_probe_personal_browser(self):
        session = Mock()
        with patch("requests.Session", return_value=session):
            result = asyncio.run(collector.inspect_product_page("123456"))
        self.assertFalse(result["ok"])
        session.get.assert_not_called()

    def test_collector_upstream_fallback_is_synchronized(self):
        configure_cdp_endpoint(45129)
        with patch.object(collector.tmall, "CDP_HOST", "http://127.0.0.1:9222"), patch.object(
            collector.tmall, "get_page_ws_url", side_effect=lambda: collector.tmall.CDP_HOST + "/owned"
        ) as upstream:
            self.assertEqual(collector._ws_url_of(None), "http://127.0.0.1:45129/owned")
            configure_cdp_endpoint(None)
            with self.assertRaises(RuntimeError):
                collector._ws_url_of(None)
            upstream.assert_called_once()

    def test_worker_upstream_fallback_is_synchronized(self):
        configure_cdp_endpoint(45130)
        client = object()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        upstream = SimpleNamespace(CDP_HOST="http://127.0.0.1:9222", CDPClient=Mock(return_value=context))
        upstream.get_page_ws_url = Mock(side_effect=lambda: upstream.CDP_HOST + "/owned")

        async def open_worker():
            async with cdp_tabs.worker_cdp(upstream, background=False) as actual:
                self.assertIs(actual, client)

        asyncio.run(open_worker())
        self.assertEqual(upstream.CDP_HOST, "http://127.0.0.1:45130")
        upstream.CDPClient.assert_called_once_with("http://127.0.0.1:45130/owned")

    def test_worker_unready_fallback_never_calls_upstream_browser(self):
        upstream = SimpleNamespace(get_page_ws_url=Mock(), CDPClient=Mock())

        async def open_worker():
            async with cdp_tabs.worker_cdp(upstream, background=False):
                self.fail("A desktop without its owned browser cannot yield a CDP client")

        with self.assertRaises(RuntimeError):
            asyncio.run(open_worker())
        upstream.get_page_ws_url.assert_not_called()
        upstream.CDPClient.assert_not_called()


class WindowVisibilityTest(unittest.TestCase):
    """采集窗口默认隐藏；没有浏览器时必须安全降级，不能阻塞采集。"""

    def test_hide_window_returns_false_without_browser(self):
        with patch("cdp_tabs._browser_ws", return_value=""):
            self.assertFalse(cdp_tabs.hide_window())

    def test_show_window_returns_false_without_browser(self):
        with patch("cdp_tabs._browser_ws", return_value=""):
            self.assertFalse(cdp_tabs.show_window())

    def test_hide_window_sets_minimized_and_moves_offscreen(self):
        responses = [
            {"id": 1, "result": {"windowId": 7, "bounds": {"windowState": "normal"}}},
            {"id": 2, "result": {}},
        ]

        class FakeWs:
            def __init__(self):
                self.sent = []

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def recv(self):
                return json.dumps(responses.pop(0))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        fake = FakeWs()
        with patch("cdp_tabs._browser_ws", return_value="ws://example/devtools/browser/x"), patch(
            "cdp_tabs.list_tabs", return_value=[{"id": "T1", "type": "page"}]
        ), patch("cdp_tabs.websockets.connect", return_value=fake):
            self.assertTrue(cdp_tabs.hide_window())
        bounds = fake.sent[-1]["params"]["bounds"]
        self.assertEqual(bounds["windowState"], "minimized")
        self.assertEqual(bounds["left"], -32000)

    def test_show_window_restores_onscreen_position(self):
        responses = [
            {"id": 1, "result": {"windowId": 7}},
            {"id": 2, "result": {}},
        ]

        class FakeWs:
            def __init__(self):
                self.sent = []

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def recv(self):
                return json.dumps(responses.pop(0))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        fake = FakeWs()
        with patch("cdp_tabs._browser_ws", return_value="ws://example/devtools/browser/x"), patch(
            "cdp_tabs.list_tabs", return_value=[{"id": "T1", "type": "page"}]
        ), patch("cdp_tabs.websockets.connect", return_value=fake):
            self.assertTrue(cdp_tabs.show_window())
        bounds = fake.sent[-1]["params"]["bounds"]
        self.assertEqual(bounds["windowState"], "normal")
        self.assertGreaterEqual(bounds["left"], 0)


if __name__ == "__main__":
    unittest.main()
