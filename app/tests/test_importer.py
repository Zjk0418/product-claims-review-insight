"""导入历史采集 JSON 的校验测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from importer import validate_import, write_imported_export


def export_payload(**overrides):
    payload = {
        "platform": "tmall",
        "product_id": "123456",
        "product_url": "https://detail.tmall.com/item.htm?id=123456",
        "product": {"id": "123456", "title": "测试商品", "price_text": "99"},
        "promotional_claims": [{"text": "持久保湿", "source": "detail_text", "confidence": 0.9}],
        "reviews": [
            {"review_id": "r1", "star": 1, "content": "用了一周没有效果", "sku": "默认"},
            {"review_id": "r2", "star": 2, "content": "味道有点重"},
        ],
        "collection_info": {"status": "success", "scanned_count": 20},
    }
    payload.update(overrides)
    return payload


class ImportValidationTest(unittest.TestCase):
    def test_valid_payload(self):
        record, error = validate_import(json.dumps(export_payload(), ensure_ascii=False), "a.json")
        self.assertEqual(error, "")
        self.assertEqual(record["product_id"], "123456")
        self.assertEqual(record["collection_info"]["status"], "imported")
        self.assertFalse(record["collection_info"]["is_complete"])
        self.assertTrue(record["import_meta"]["source"] == "import")
        self.assertEqual(record["import_meta"]["accepted_review_count"], 2)

    def test_invalid_json(self):
        record, error = validate_import("{not json", "bad.json")
        self.assertIsNone(record)
        self.assertIn("JSON 解析失败", error)
        self.assertIn("行", error)

    def test_top_level_not_object(self):
        record, error = validate_import("[1,2,3]")
        self.assertIsNone(record)
        self.assertIn("JSON 对象", error)

    def test_missing_product_id(self):
        payload = export_payload()
        payload.pop("product_id")
        payload.pop("product")
        record, error = validate_import(json.dumps(payload))
        self.assertIsNone(record)
        self.assertIn("商品 ID", error)

    def test_empty_reviews(self):
        record, error = validate_import(json.dumps(export_payload(reviews=[])))
        self.assertIsNone(record)
        self.assertIn("没有可分析", error)

    def test_reviews_missing_id_rejected(self):
        record, error = validate_import(
            json.dumps(export_payload(reviews=[{"star": 1, "content": "x"}]))
        )
        self.assertIsNone(record)
        self.assertIn("没有可用评价", error)

    def test_duplicate_reviews_are_deduped(self):
        record, error = validate_import(
            json.dumps(
                export_payload(
                    reviews=[
                        {"review_id": "r1", "star": 1, "content": "a"},
                        {"review_id": "r1", "star": 1, "content": "a"},
                        {"review_id": "r2", "star": 2, "content": "b"},
                    ]
                )
            )
        )
        self.assertEqual(error, "")
        self.assertEqual(len(record["reviews"]), 2)
        self.assertEqual(record["import_meta"]["original_review_count"], 3)

    def test_product_id_from_product_field(self):
        payload = export_payload()
        payload.pop("product_id")
        record, error = validate_import(json.dumps(payload))
        self.assertEqual(error, "")
        self.assertEqual(record["product_id"], "123456")

    def test_written_file_is_pickable_by_analysis_loader(self):
        from analysis.claim_extractor import load_export

        record, _ = validate_import(json.dumps(export_payload(), ensure_ascii=False), "a.json")
        with tempfile.TemporaryDirectory() as tmp:
            path = write_imported_export(record, Path(tmp))
            self.assertTrue(path.exists())
            self.assertIn("imported", path.name)
            loaded, loaded_path = load_export("123456", Path(tmp))
            self.assertEqual(loaded["product_id"], "123456")
            self.assertEqual(len(loaded["reviews"]), 2)
            self.assertEqual(loaded_path, str(path))

    def test_oversize_rejected(self):
        record, error = validate_import("x" * (21 * 1024 * 1024))
        self.assertIsNone(record)
        self.assertIn("超过", error)


if __name__ == "__main__":
    unittest.main()
