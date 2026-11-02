"""登录后自动继续 + 持久化浏览器配置目录的单元测试。"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from browser_session import persistent_profile


class PersistentProfileTest(unittest.TestCase):
    def test_env_override_wins(self):
        with patch.dict(os.environ, {"REVIEW_COLLECTION_PROFILE": r"D:\profiles\col"}, clear=False):
            self.assertEqual(persistent_profile(), Path(r"D:\profiles\col"))

    def test_default_lives_under_localappdata(self):
        env = {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("REVIEW_COLLECTION_PROFILE", None)
            self.assertEqual(
                persistent_profile(),
                Path(env["LOCALAPPDATA"]) / "ReviewInsight" / "collection-profile",
            )


class AutoResumeTest(unittest.TestCase):
    def test_only_login_related_pauses_start_the_watcher(self):
        with patch("server.threading.Thread") as thread:
            server.schedule_auto_resume("job-a", "browser_disconnected")
        thread.assert_not_called()
        with patch("server.threading.Thread") as thread:
            server.schedule_auto_resume("job-b", "login_required")
        thread.assert_called_once()

    def test_watcher_runs_as_daemon_with_its_own_name(self):
        with patch("server.threading.Thread") as thread:
            server.schedule_auto_resume("job-c", "platform_validate")
        kwargs = thread.call_args.kwargs
        self.assertTrue(kwargs["daemon"])
        self.assertEqual(kwargs["name"], "auto-resume")

    def test_cancel_invalidates_pending_watcher(self):
        server._auto_resume_tokens["job-d"] = 3
        server.cancel_auto_resume("job-d")
        self.assertEqual(server._auto_resume_tokens["job-d"], 4)

    def test_each_schedule_bumps_the_token(self):
        server._auto_resume_tokens.pop("job-e", None)
        with patch("server.threading.Thread"):
            server.schedule_auto_resume("job-e", "login_required")
            first = server._auto_resume_tokens["job-e"]
            server.schedule_auto_resume("job-e", "login_required")
            second = server._auto_resume_tokens["job-e"]
        self.assertEqual(second, first + 1)

    def test_login_state_is_safe_without_browser(self):
        from cdp_tabs import login_state

        with patch("cdp_tabs._browser_ws", return_value=""):
            state = login_state()
        self.assertFalse(state["available"])
        self.assertFalse(state["logged_in"])
        self.assertFalse(state["on_login_page"])


if __name__ == "__main__":
    unittest.main()
