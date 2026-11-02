"""Asset location must not depend on the working directory or module depth."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resources import resource_path, resource_root


class ResourcesTest(unittest.TestCase):
    def test_source_root_is_project(self):
        with patch.object(sys, "_MEIPASS", None, create=True):
            self.assertEqual(resource_root(), Path(__file__).resolve().parents[2])
            self.assertTrue(resource_path("models", "bge-small-zh-v1.5", "model.onnx").is_file())

    def test_frozen_root_uses_bundle_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "_MEIPASS", tmp, create=True):
            self.assertEqual(resource_root(), Path(tmp).resolve())
            self.assertEqual(resource_path("app", "static", "index.html"), Path(tmp).resolve() / "app/static/index.html")


if __name__ == "__main__":
    unittest.main()
