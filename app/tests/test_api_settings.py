"""API settings remain session-only unless explicitly remembered with DPAPI."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api_settings import ApiSettingsError, ApiSettingsStore
from analysis.llm_client import LLMConfig


class ApiSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "settings"
        self.store = ApiSettingsStore(self.directory)
        self.key = "fake-test-key-0123456789abcdef"

    def test_session_default_never_writes_config_or_returns_full_key(self):
        public = self.store.update({"api_key": self.key, "model": "test-model"})
        self.assertTrue(public["configured"])
        self.assertFalse(public["remember"])
        self.assertNotIn(self.key, json.dumps(public))
        self.assertNotIn("api_key", public)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.store.llm_kwargs()["api_key"], self.key)

    def test_blank_key_preserves_existing_and_clear_is_explicit(self):
        self.store.update({"api_key": self.key})
        self.store.update({"api_key": "", "model": "another"})
        self.assertEqual(self.store.llm_kwargs()["api_key"], self.key)
        self.assertFalse(self.store.update({"clear_key": True, "api_key": ""})["configured"])
        self.assertIsNone(self.store.llm_kwargs())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_remembered_key_is_encrypted_and_roundtrips_current_user(self):
        self.store.update({"api_key": self.key, "remember": True})
        raw = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn(self.key, raw)
        self.assertNotIn('"api_key"', raw)
        self.assertEqual(json.loads(raw)["key_storage"], "windows-dpapi-v1")
        restored = ApiSettingsStore(self.directory)
        self.assertEqual(restored.llm_kwargs()["api_key"], self.key)
        restored.update({"remember": False})
        self.assertFalse(restored.path.exists())
        self.assertTrue(restored.public()["configured"])

    def test_non_windows_remember_rejected_without_losing_session(self):
        self.store.update({"api_key": self.key})
        with patch("api_settings._dpapi", side_effect=ApiSettingsError("secure store unavailable")):
            with self.assertRaises(ApiSettingsError):
                self.store.update({"remember": True})
        self.assertTrue(self.store.public()["configured"])
        self.assertFalse(self.store.path.exists())

    def test_invalid_configuration_does_not_modify_existing_key(self):
        self.store.update({"api_key": self.key})
        for payload in ({"base_url": "https://user:secret@example.com/v1"},
                        {"base_url": "http://example.com/v1"},
                        {"base_url": "https://example.com/v1?api_key=secret"},
                        {"timeout": 1}, {"max_tokens": "bad"}, {"api_key": "masked****1234"}):
            with self.subTest(payload=payload), self.assertRaises(ApiSettingsError):
                self.store.update(payload)
        self.assertEqual(self.store.llm_kwargs()["api_key"], self.key)

    def test_remote_http_allowed_only_with_explicit_opt_in(self):
        with self.assertRaises(ApiSettingsError):
            self.store.update({"api_key": self.key, "base_url": "http://example.com/v1"})
        public = self.store.update({"api_key": self.key, "base_url": "http://example.com/v1",
                                    "allow_insecure_http": True})
        self.assertTrue(public["allow_insecure_http"])
        self.assertEqual(self.store.llm_kwargs()["base_url"], "http://example.com/v1")
        # 关掉开关后，保存再校验也会被拦截
        with self.assertRaises(ApiSettingsError):
            self.store.update({"allow_insecure_http": False})

    def test_connection_check_is_explicit_transient_and_redacts_upstream_error(self):
        response = SimpleNamespace(status_code=401, text=self.key)
        with patch("api_settings.requests.post", return_value=response) as post:
            result = self.store.test_connection({"api_key": self.key})
        self.assertFalse(result["ok"])
        self.assertNotIn(self.key, json.dumps(result))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer " + self.key)
        self.assertFalse(self.store.public()["configured"])
        self.assertFalse(self.directory.exists())

    def test_compatible_response_is_accepted_without_echoing_response_content(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": self.key}}]})
        with patch("api_settings.requests.post", return_value=response):
            result = self.store.test_connection({"api_key": self.key})
        self.assertTrue(result["ok"])
        self.assertNotIn(self.key, json.dumps(result))

    def test_app_mode_ignores_project_dotenv_and_inherited_shell_key(self):
        (Path(self.tmp.name) / ".env").write_text("LLM_API_KEY=another-project-key\n", encoding="utf-8")
        with patch("api_settings.SETTINGS", self.store), patch.dict(os.environ, {
            "REVIEW_APP_MODE": "1", "LLM_API_KEY": "inherited-key"
        }):
            self.assertIsNone(LLMConfig.from_env(Path(self.tmp.name)))
            self.store.update({"api_key": self.key})
            config = LLMConfig.from_env(Path(self.tmp.name))
            self.assertEqual(config.api_key, self.key)
            self.assertNotIn(self.key, repr(config))
            self.assertNotIn(self.key, json.dumps(config.describe()))

    def test_legacy_cli_environment_remains_compatible(self):
        with patch("api_settings.SETTINGS", self.store), patch.dict(os.environ, {
            "REVIEW_APP_MODE": "0", "LLM_API_KEY": self.key, "LLM_MODEL": "legacy-model"
        }):
            config = LLMConfig.from_env()
        self.assertEqual((config.api_key, config.model), (self.key, "legacy-model"))


if __name__ == "__main__":
    unittest.main()
