import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from browser_session import CollectionBrowser, cdp_available
from cdp_endpoint import configure_cdp_endpoint, get_cdp_host


class BrowserSessionTest(unittest.TestCase):
    def setUp(self):
        self.mode = patch.dict(os.environ, {"REVIEW_DESKTOP_BROWSER": "1"})
        self.mode.start()
        configure_cdp_endpoint(None)

    def tearDown(self):
        configure_cdp_endpoint(None)
        self.mode.stop()

    def ready_process(self, args, **_kwargs):
        profile = Path(next(v.split("=", 1)[1] for v in args if v.startswith("--user-data-dir=")))
        (profile / "DevToolsActivePort").write_text("43127\n/devtools/browser/test-owned\n", encoding="utf-8")
        process = Mock()
        process.poll.return_value = None
        return process

    def test_an_existing_debug_browser_is_not_reused(self):
        browser = CollectionBrowser()
        with tempfile.TemporaryDirectory() as tmp, patch("browser_session.cdp_available", return_value=True) as probe, patch(
            "browser_session.find_browser", return_value=Path("C:/Browser/browser.exe")
        ), patch("browser_session._terminate_profile_browsers"), patch(
            "browser_session.subprocess.Popen", side_effect=self.ready_process
        ) as spawn:
            browser.ensure(Path(tmp))
            spawn.assert_called_once()
            self.assertEqual(probe.call_args.args, (43127, "/devtools/browser/test-owned"))
            self.assertEqual(get_cdp_host(), "http://127.0.0.1:43127")
            browser.close()

    def test_profile_and_extensions_are_isolated_and_owned_process_closes_once(self):
        browser = CollectionBrowser()
        with tempfile.TemporaryDirectory() as tmp, patch("browser_session.cdp_available", return_value=True), patch(
            "browser_session.find_browser", return_value=Path("C:/Browser/browser.exe")
        ), patch("browser_session._terminate_profile_browsers"), patch(
            "browser_session.subprocess.Popen", side_effect=self.ready_process
        ) as spawn:
            browser.ensure(Path(tmp), "about:blank")
            process = browser.process
            args = spawn.call_args.args[0]
            self.assertIn(f"--user-data-dir={(Path(tmp) / 'collection-browser').resolve()}", args)
            for flag in ("--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                         "--disable-extensions", "--disable-component-extensions-with-background-pages",
                         "--disable-default-apps", "--disable-sync"):
                self.assertIn(flag, args)
            browser.close()
            browser.close()
            process.terminate.assert_called_once()
            with self.assertRaises(RuntimeError):
                get_cdp_host()

    def test_same_session_reuses_its_own_ready_process(self):
        browser = CollectionBrowser()
        with tempfile.TemporaryDirectory() as tmp, patch("browser_session.cdp_available", return_value=True), patch(
            "browser_session.find_browser", return_value=Path("C:/Browser/browser.exe")
        ), patch("browser_session._terminate_profile_browsers"), patch(
            "browser_session.subprocess.Popen", side_effect=self.ready_process
        ) as spawn:
            browser.ensure(Path(tmp))
            browser.ensure(Path(tmp))
            spawn.assert_called_once()
            browser.close()

    def test_idle_close_does_not_terminate_any_browser(self):
        with patch("browser_session.subprocess.Popen") as spawn:
            CollectionBrowser().close()
        spawn.assert_not_called()

    def test_failed_start_is_closed_and_endpoint_cleared(self):
        browser = CollectionBrowser()
        with tempfile.TemporaryDirectory() as tmp, patch("browser_session.cdp_available", return_value=False), patch(
            "browser_session.find_browser", return_value=Path("C:/Browser/browser.exe")
        ), patch("browser_session._terminate_profile_browsers"), patch(
            "browser_session.subprocess.Popen", side_effect=self.ready_process
        ) as spawn, patch("browser_session.time.monotonic", side_effect=[0, 1, 46]), patch("browser_session.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "未能就绪"):
                browser.ensure(Path(tmp))
            self.assertIsNone(browser.process)
            self.assertIsNone(browser.port)
            self.assertEqual(spawn.call_count, 1)
            with self.assertRaises(RuntimeError):
                get_cdp_host()

    def test_edge_relaunch_is_waited_out_instead_of_breaking_early(self):
        browser = CollectionBrowser()
        exited = Mock()
        exited.poll.return_value = 1
        exited.returncode = 1

        def spawn(args, **_kwargs):
            return exited

        with tempfile.TemporaryDirectory() as tmp, patch(
            "browser_session.find_browser", return_value=Path("C:/Browser/browser.exe")
        ), patch("browser_session._terminate_profile_browsers"), patch(
            "browser_session.subprocess.Popen", side_effect=spawn
        ), patch("browser_session.cdp_available", return_value=True), patch(
            "browser_session.time.sleep", side_effect=lambda _seconds: (
                (Path(tmp) / "collection-browser" / "DevToolsActivePort").write_text(
                    "43127\n/devtools/browser/relaunched\n", encoding="utf-8"
                )
            )
        ):
            browser.ensure(Path(tmp))
            self.assertEqual(browser.port, 43127)
            self.assertEqual(browser.browser_path, "/devtools/browser/relaunched")
            browser.close()

    def test_stale_profile_browser_is_cleaned_before_launch(self):
        browser = CollectionBrowser()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "browser_session.find_browser", return_value=Path("C:/Browser/browser.exe")
        ), patch("browser_session.cdp_available", return_value=True), patch(
            "browser_session._terminate_profile_browsers"
        ) as cleanup, patch(
            "browser_session.subprocess.Popen", side_effect=self.ready_process
        ):
            browser.ensure(Path(tmp))
            cleanup.assert_called_once_with((Path(tmp) / "collection-browser").resolve())
            browser.close()

    def test_close_cleans_orphans_by_profile(self):
        browser = CollectionBrowser()
        profile = Path("C:/Temp/owned-profile")
        browser.profile = profile
        with patch("browser_session._terminate_profile_browsers") as cleanup:
            browser.close()
            cleanup.assert_called_once_with(profile)

    def test_endpoint_must_match_our_browser_identity(self):
        data = {"Browser": "Edg/Test", "webSocketDebuggerUrl": "ws://127.0.0.1:43127/devtools/browser/wrong"}
        with patch("browser_session.urllib.request.build_opener") as factory:
            factory.return_value.open.side_effect = lambda *_a, **_kw: io.BytesIO(json.dumps(data).encode())
            self.assertFalse(cdp_available(43127, "/devtools/browser/test-owned"))
            data["webSocketDebuggerUrl"] = "ws://127.0.0.1:43127/devtools/browser/test-owned"
            self.assertTrue(cdp_available(43127, "/devtools/browser/test-owned"))


if __name__ == "__main__":
    unittest.main()
