import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_workspace import SessionWorkspace


class SessionWorkspaceTest(unittest.TestCase):
    def test_cleanup_removes_only_owned_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            keep = base / "keep.txt"
            keep.write_text("user file")
            session = SessionWorkspace(base)
            (session.root / "output.json").write_text("temporary")
            session.cleanup()
            self.assertFalse(session.root.exists())
            self.assertEqual(keep.read_text(), "user file")

    def test_wrong_marker_refuses_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionWorkspace(Path(tmp))
            session.marker.write_text(json.dumps({"token": "not-ours"}))
            with self.assertRaises(ValueError):
                session.cleanup()
            self.assertTrue(session.root.exists())
            session.marker.write_text(json.dumps({"token": session.token}))
            session.cleanup()

    def test_locked_file_keeps_ownership_marker_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionWorkspace(Path(tmp))
            (session.root / "files").mkdir()
            with patch("session_workspace.shutil.rmtree", side_effect=PermissionError), patch("session_workspace.time.sleep"):
                session.cleanup()
            self.assertTrue(session.marker.exists())
            self.assertFalse(session.closed)
            session.cleanup()
            self.assertFalse(session.root.exists())

    def test_startup_removes_abandoned_but_not_active_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = base / "session-abandoned"
            old.mkdir()
            (old / ".review-session.json").write_text(json.dumps({"token": "a" * 32, "pid": 999999999}))
            with patch.object(SessionWorkspace, "_pid_alive", return_value=False):
                session = SessionWorkspace(base)
            self.assertFalse(old.exists())
            second = SessionWorkspace(base)
            self.assertTrue(session.root.exists())
            second.cleanup()
            session.cleanup()


if __name__ == "__main__":
    unittest.main()
