"""手动补充宣传语的落盘逻辑自检。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))

import server  # noqa: E402


class ManualClaimsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tmall_x.json"
        self.path.write_text(
            json.dumps({"product": {"id": "x"},
                        "promotional_claims": [{"text": "宽松版型", "source": "image_ocr",
                                                "confidence": 0.9}]},
                       ensure_ascii=False),
            encoding="utf-8",
        )

    def test_appends_and_dedupes(self):
        added = server._append_manual_claims(
            self.path, ["阔腿显瘦，宽松遮臀", "宽松版型", "   "]
        )
        self.assertEqual(added, 1)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        texts = [item["text"] for item in data["promotional_claims"]]
        self.assertIn("阔腿显瘦，宽松遮臀", texts)
        self.assertEqual(data["promotional_claims"][-1]["source"], "manual")
        self.assertEqual(data["product"]["promotional_claims"], data["promotional_claims"])

    def test_creates_file_when_missing(self):
        path = Path(self.tmp.name) / "tmall_y.json"
        added = server._append_manual_claims(path, ["透气舒适"])
        self.assertEqual(added, 1)
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["promotional_claims"][0]["source"], "manual")

    def test_repeated_lines_add_once(self):
        first = server._append_manual_claims(self.path, ["抗皱免烫"])
        second = server._append_manual_claims(self.path, ["抗皱免烫"])
        self.assertEqual((first, second), (1, 0))


if __name__ == "__main__":
    unittest.main()
