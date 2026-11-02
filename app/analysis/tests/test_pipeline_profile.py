"""抽取、缓存与复用路径必须沿用品类，保持问题 ID 与下游引用一致。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from analysis import pipeline
from analysis.categories import get_profile
from analysis.issue_schema import validate_issue


class PipelineProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.export = self.root / "runtime/exports/tmall_123456_input.json"
        self.export.parent.mkdir(parents=True)
        self.text = "用了以后脸上起痘，皮肤还一直泛红"
        self.review = {"review_id": "r1", "star": 1, "text": self.text, "content": self.text,
                       "valid_for_analysis": True, "sku_id": "", "date": "2026-10-01"}
        self.export.write_text(json.dumps({"product_id": "123456", "product": {"title": "祛痘精华"},
            "reviews": [self.review]}, ensure_ascii=False), encoding="utf-8")
        raw = {"object": "面部", "aspect": "功效与效果", "manifestation": "使用后起痘并泛红",
               "direction": "irritation", "evidence": self.text, "severity": "medium", "confidence": 0.9}
        self.issue, errors = validate_issue(raw, self.review, "fake", "issue-extract-v6:beauty", get_profile("beauty"))
        self.assertEqual(errors, [])
        self.extraction = {"model": "fake", "prompt_version": "issue-extract-v6:beauty", "usage": {},
            "input_review_count": 1, "issue_count": 1, "reviews_with_issues": 1, "validation_failed_count": 0,
            "reviews": [{"review_id": "r1", "issues": [self.issue.to_dict()]}]}
        self.saved = self.root / "saved.json"
        self.saved.write_text(json.dumps(self.extraction, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self, **kwargs):
        with patch.object(pipeline, "ROOT", self.root), patch.object(
            pipeline.LLMConfig, "from_env", return_value=SimpleNamespace(model="fake")
        ), patch.object(pipeline, "run_extraction", return_value=self.extraction), patch.object(
            pipeline, "embed_issues", return_value=([self.text], np.array([[1.0, 0.0]]), {})
        ), patch.object(pipeline, "_package_versions", return_value={}):
            summary = pipeline.run_pipeline(product_id="123456", inputs=[str(self.export)],
                out_root=self.root / "analysis", skip_claims=True, **kwargs)
        self.assertEqual(summary["analysis_status"], "completed", summary["errors"])
        out = self.root / "analysis/123456"
        if kwargs.get("from_gold"):
            out = out / "experiments/gold"
        flat = json.loads((out / "issues_flat.json").read_text(encoding="utf-8"))["issues"]
        clusters = json.loads((out / "issue_clusters.json").read_text(encoding="utf-8"))["clusters"]
        self.assertEqual({i["issue_id"] for i in flat}, {self.issue.issue_id})
        self.assertEqual([i["object"] for i in flat], ["皮肤"])
        self.assertEqual({iid for c in clusters for iid in c["member_issue_ids"]}, {self.issue.issue_id})
        return flat, out

    def test_new_and_cached_extraction_preserve_source_ids(self):
        for status in ("ok", "cached"):
            with self.subTest(status=status):
                self.extraction["reviews"][0]["status"] = status
                flat, out = self.run_pipeline()
                source = json.loads((out / "extracted_issues.json").read_text(encoding="utf-8"))
                self.assertEqual(source["reviews"][0]["issues"][0]["issue_id"], flat[0]["issue_id"])

    def test_from_extracted_preserves_category_and_model(self):
        flat, _ = self.run_pipeline(from_extracted=self.saved)
        self.assertEqual((flat[0]["model"], flat[0]["prompt_version"]), ("fake", "issue-extract-v6:beauty"))

    def test_gold_experiment_uses_detected_category(self):
        flat, _ = self.run_pipeline(from_gold=self.saved)
        self.assertEqual(flat[0]["model"], "manual_gold")

    def test_helper_infers_category_from_saved_prompt(self):
        issues, errors = pipeline.issues_from_extraction(self.extraction, [self.review])
        self.assertEqual(errors, [])
        self.assertEqual((issues[0].object, issues[0].issue_id), ("皮肤", self.issue.issue_id))


if __name__ == "__main__":
    unittest.main()
