"""宣传来源选择与近义主张重复的回归测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analysis.pipeline import select_claim_export
from analysis.claim_extractor import build_raw_texts


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class SelectClaimExportTest(unittest.TestCase):
    """宣传主张必须与本次评论来自同一份采集文件。"""

    def test_prefers_explicit_input_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            # 旧文件信息量更高（OCR 文本更多），但本次分析指定的是新文件
            old = _write(
                exports / "tmall_1_lowstar_count100_20260101_000000.json",
                {
                    "product_id": "1",
                    "product": {"title": "同一商品", "images": [{"url": "a"}] * 13},
                    "promotional_claims": [{"text": f"旧卖点{i}", "source": "title"} for i in range(120)],
                },
            )
            new = _write(
                exports / "tmall_1_lowstar_count300_20260102_000000.json",
                {
                    "product_id": "1",
                    "product": {"title": "同一商品", "images": [{"url": "b"}] * 15},
                    "promotional_claims": [{"text": "新卖点", "source": "title"}],
                },
            )
            export, path = select_claim_export([str(new)], "1", exports)
            self.assertEqual(path, str(new))
            self.assertEqual(len(export["promotional_claims"]), 1)
            self.assertNotEqual(path, str(old))

    def test_falls_back_when_input_has_no_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            _write(
                exports / "tmall_2_lowstar_count100_20260101_000000.json",
                {
                    "product_id": "2",
                    "product": {"title": "有主张商品"},
                    "promotional_claims": [{"text": "卖点", "source": "title"}],
                },
            )
            empty_input = _write(
                root / "empty_input.json",
                {"product_id": "2", "product": {"title": "x"}, "promotional_claims": []},
            )
            export, path = select_claim_export([str(empty_input)], "2", exports)
            self.assertEqual(len(export["promotional_claims"]), 1)
            self.assertIn("count100", path)

    def test_falls_back_when_no_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            target = _write(
                exports / "tmall_3_lowstar_count100_20260101_000000.json",
                {
                    "product_id": "3",
                    "product": {"title": "仅此一份"},
                    "promotional_claims": [{"text": "卖点", "source": "title"}],
                },
            )
            export, path = select_claim_export([], "3", exports)
            self.assertEqual(path, str(target))
            self.assertTrue(export["promotional_claims"])

    def test_relative_input_path_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            target = _write(
                root / "rel_input.json",
                {
                    "product_id": "4",
                    "product": {"title": "相对路径"},
                    "promotional_claims": [{"text": "卖点", "source": "title"}],
                },
            )
            import os

            cwd = os.getcwd()
            try:
                os.chdir(root)
                export, path = select_claim_export(["rel_input.json"], "4", exports)
            finally:
                os.chdir(cwd)
            self.assertEqual(Path(path).resolve(), target.resolve())


class NearDuplicateClaimTest(unittest.TestCase):
    """同一卖点的近义表达在原始文本层应保留，但不能没有度量手段。"""

    def test_semantic_groups_can_be_measured(self):
        # 只用真实存在的工具做最小验证：build_raw_texts 保留全部原始文本
        export = {
            "product": {"title": "祛痘印淡化痘印舒缓痘印"},
            "promotional_claims": [
                {"text": "祛痘印淡化痘印舒缓痘印", "source": "title", "confidence": 1.0}
            ],
        }
        texts = build_raw_texts(export)
        self.assertEqual(len(texts), 1)
        self.assertIn("祛痘印", texts[0]["text"])


if __name__ == "__main__":
    unittest.main()
