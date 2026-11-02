"""Report assembly restores source metadata without bypassing analysis cleaning."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class ServerReportModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.exports = self.root / "exports"
        self.exports.mkdir()
        self.legacy_exports = self.root / "legacy-exports"
        self.legacy_exports.mkdir()
        for name, value in {
            "ROOT": self.root,
            "EXPORT_DIR": self.exports,
            "LEGACY_EXPORT_DIR": self.legacy_exports,
            "ANALYSIS_DIR": self.root / "analysis",
        }.items():
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.paths = server._analysis_paths("123456")
        self.paths["base"].mkdir(parents=True)
        self.cleaned = {"review_id": "r1", "text": "清洗后的正文", "content": "原分析正文",
                        "valid_for_analysis": False, "sku_id": "", "flags": ["low_information"]}
        for key, value in {
            "summary": {"product_id": "123456"},
            "statistics": {"clusters": []},
            "claims": {"claims": []},
            "gaps": {"gaps": []},
            "reviews": {"input_digest": "kept-digest", "reviews": [self.cleaned]},
        }.items():
            self.write(self.paths[key], value)
        freshness = patch.object(server, "load_analysis_result", return_value={"freshness": {"status": "current"}})
        freshness.start()
        self.addCleanup(freshness.stop)
        builder = patch("reporting.build_report_model", return_value={"assembled": True})
        self.build = builder.start()
        self.addCleanup(builder.stop)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def export(self, suffix, reviews, directory=None, **extra):
        path = (directory or self.exports) / f"tmall_123456_{suffix}.json"
        self.write(path, {"product_id": "123456", "reviews": reviews, **extra})
        return path

    def call(self):
        self.assertEqual(server.report_model("123456"), {"assembled": True})
        return self.build.call_args.args, self.build.call_args.kwargs

    def test_hint_selects_one_export_and_restores_only_safe_metadata(self):
        source = {"review_id": "r1", "sku_id": "sku-1", "product_id": "123456",
                  "append_content": "追评：仍有问题", "append_date": "2026-10-28",
                  "parent_review_id": "parent-1", "original_review_id": "original-1",
                  "reviewer_id": "reviewer-1", "buyer_id": 123, "user_id": "user-1",
                  "text": "未清洗正文", "content": "不应覆盖分析正文", "valid_for_analysis": True,
                  "flags": [], "nick": "买家昵称", "unrelated_private_field": "not-copied"}
        selected = self.export("20261001", [source], product={"title": "选中的商品"},
                               collection_info={"scanned_count": 300})
        self.export("20261002", [{"review_id": "r1", "sku_id": "other-batch"}],
                    product={"title": "不应选择"}, collection_info={"scanned_count": 900})
        self.write(self.paths["claims"], {"input_export": str(Path("older-location") / selected.name), "claims": []})
        flat = {"issues": [{"issue_id": "I1", "review_id": "r1", "description": "问题依据"}]}
        self.write(self.paths["flat"], flat)

        args, kwargs = self.call()
        reviews = args[4]
        row = reviews["reviews"][0]
        self.assertEqual(reviews["input_digest"], "kept-digest")
        self.assertEqual(row["text"], self.cleaned["text"])
        self.assertEqual(row["content"], self.cleaned["content"])
        self.assertFalse(row["valid_for_analysis"])
        self.assertEqual(row["flags"], ["low_information"])
        for key in ("sku_id", "product_id", "append_content", "append_date", "parent_review_id",
                    "original_review_id", "reviewer_id", "buyer_id", "user_id"):
            self.assertEqual(row[key], source[key], key)
        self.assertNotIn("nick", row)
        self.assertNotIn("unrelated_private_field", row)
        self.assertEqual(args[5]["title"], "选中的商品")
        self.assertEqual(kwargs["collection"], {"scanned_count": 300})
        self.assertEqual(kwargs["issues"], flat)

    def test_extraction_statuses_are_forwarded_and_missing_is_optional(self):
        payload = {"reviews": [{"review_id": "r1", "status": "partial_validation", "errors": ["引用错误"]}]}
        self.write(self.paths["extracted"], payload)
        _, kwargs = self.call()
        self.assertEqual(kwargs["extraction"], payload)
        self.paths["extracted"].unlink()
        _, kwargs = self.call()
        self.assertIsNone(kwargs["extraction"])

    def test_unknown_hint_keeps_latest_export_fallback_and_list_flat(self):
        self.export("20261001", [{"review_id": "r1", "user_id": "older-batch-user"}])
        self.export("20261002", [{"review_id": "r1", "sku_id": "latest-sku", "nick": "同一昵称"}])
        self.write(self.paths["claims"], {"input_export": "missing.json", "claims": []})
        flat = [{"issue_id": "I2", "review_id": "r1"}]
        self.write(self.paths["flat"], flat)

        args, kwargs = self.call()
        row = args[4]["reviews"][0]
        self.assertEqual(row["sku_id"], "latest-sku")
        for identity in ("reviewer_id", "buyer_id", "user_id"):
            self.assertNotIn(identity, row)
        self.assertEqual(kwargs["issues"], flat)

    def test_missing_flat_is_optional_and_raw_rows_do_not_expand_analysis_input(self):
        self.export("20261001", [
            {"review_id": "other", "user_id": "unrelated-user"},
            {"review_id": "r1", "nick": "只有昵称", "reviewer_id": {"unsafe": "nested"},
             "buyer_id": True, "user_id": [], "append_content": ["not-text"]},
        ])
        args, kwargs = self.call()
        self.assertEqual(args[4]["reviews"], [self.cleaned])
        self.assertIsNone(kwargs["issues"])

    def test_list_reviews_keep_existing_metadata_and_source_is_not_mutated(self):
        cleaned = [{**self.cleaned, "sku_id": "analysis-sku", "reviewer_id": "analysis-user"}, None]
        raw = {"reviews": [{"review_id": "r1", "sku_id": "raw-sku", "reviewer_id": "raw-user",
                            "append_date": "2026-10-28"}]}
        merged = server._merge_report_review_metadata(cleaned, raw)
        self.assertIsInstance(merged, list)
        self.assertEqual(merged[0]["sku_id"], "analysis-sku")
        self.assertEqual(merged[0]["reviewer_id"], "analysis-user")
        self.assertEqual(merged[0]["append_date"], "2026-10-28")
        self.assertIsNone(merged[1])
        self.assertNotIn("append_date", cleaned[0])
        self.assertEqual(raw["reviews"][0]["sku_id"], "raw-sku")

    def test_no_export_does_not_create_review_metadata(self):
        args, kwargs = self.call()
        self.assertEqual(args[4]["reviews"], [self.cleaned])
        self.assertEqual(args[5]["id"], "123456")
        self.assertEqual(kwargs["collection"], {})
        self.assertIsNone(kwargs["issues"])

    def test_legacy_analysis_reads_flat_and_uses_legacy_export(self):
        legacy = self.root / "runtime" / "analysis" / "123456"
        legacy.mkdir(parents=True)
        for path in self.paths["base"].iterdir():
            path.replace(legacy / path.name)
        self.export("20261001", [{"review_id": "r1", "sku_id": "legacy-sku"}], directory=self.legacy_exports)
        flat = {"issues": [{"issue_id": "legacy-issue", "review_id": "r1"}]}
        self.write(legacy / "issues_flat.json", flat)
        args, kwargs = self.call()
        self.assertEqual(args[4]["reviews"][0]["sku_id"], "legacy-sku")
        self.assertEqual(kwargs["issues"], flat)


if __name__ == "__main__":
    unittest.main()
