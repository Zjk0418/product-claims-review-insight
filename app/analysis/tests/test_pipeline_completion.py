"""Transport/validation failure is not an empty successful analysis."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from analysis import pipeline
from analysis.categories import get_profile
from analysis.issue_schema import validate_issue


class PipelineCompletionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.export = self.root / "exports/tmall_123456_input.json"
        self.export.parent.mkdir()
        texts = ["衣服穿了两次袖口起球，摩擦部位出现很多毛球，影响穿着。",
                 "裤子的包装外袋压坏了，送到后快递又放错了楼栋，需要自己寻找。"]
        self.reviews = [{"review_id": f"r{i+1}", "star": 2, "date": "2026-10-28",
                         "sku_id": "sku-A", "sku": "黑色M", "text": text,
                         "content": text, "valid_for_analysis": True}
                        for i, text in enumerate(texts)]
        self.export.write_text(json.dumps({"product_id": "123456", "product": {"title": "棉质衣服"},
                                           "reviews": self.reviews}, ensure_ascii=False), encoding="utf-8")
        raw = {"object": "衣服", "aspect": "耐用性", "manifestation": "衣服起球", "direction": "pilling",
               "evidence": texts[0], "severity": "medium", "confidence": .9}
        self.issue, errors = validate_issue(raw, self.reviews[0], "offline", "issue-extract-v6:apparel", get_profile("apparel"))
        self.assertFalse(errors)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, index, status="empty", *, issues=None, errors=None):
        review = self.reviews[index]
        return {"review_id": review["review_id"], "star": review["star"], "date": review["date"],
                "sku_id": review["sku_id"], "content": review["text"], "status": status,
                "issues": issues or [], "errors": errors or []}

    def payload(self, rows):
        return {"model": "offline", "prompt_version": "issue-extract-v6:apparel", "usage": {},
                "input_review_count": 2, "processed_review_count": len(rows),
                "issue_count": sum(len(row["issues"]) for row in rows),
                "reviews_with_issues": sum(bool(row["issues"]) for row in rows),
                "validation_failed_count": sum(row["status"] == "validation_failed" for row in rows),
                "reviews": rows}

    def run_analysis(self, payload, *, skip_claims=True, from_extracted=None, raises=None,
                     claim_payload=None, gap_payload=None):
        extractor = Mock(return_value=payload, side_effect=raises)
        embed = Mock(return_value=([self.reviews[0]["text"]], np.array([[1.0, 0.0]]), {}))
        claims = Mock(return_value=claim_payload or {})
        analyzer = Mock()
        analyzer.analyze.return_value = gap_payload or {}
        with patch.object(pipeline, "ROOT", self.root), patch.object(
            pipeline.LLMConfig, "from_env", return_value=SimpleNamespace(model="offline")
        ), patch.object(pipeline, "run_extraction", extractor), patch.object(
            pipeline, "embed_issues", embed
        ), patch.object(pipeline, "run_claim_extraction", claims), patch.object(
            pipeline, "ChatClient"
        ), patch.object(pipeline, "GapAnalyzer", return_value=analyzer), patch.object(
            pipeline, "group_near_duplicate_claims", return_value=([], {})
        ), patch.object(
            pipeline, "_package_versions", return_value={}
        ):
            summary = pipeline.run_pipeline(product_id="123456", inputs=[str(self.export)],
                                            out_root=self.root / "analysis", exports_dir=self.export.parent,
                                            skip_claims=skip_claims, from_extracted=from_extracted)
        return summary, extractor, embed, claims

    def test_all_transport_failures_are_retryable_failure_and_skip_paid_followups(self):
        rows = [self.record(i, "validation_failed", errors=[
            "LLM 调用失败：Connection aborted RemoteDisconnected"
        ]) for i in range(2)]
        payload = self.payload(rows)
        before = copy.deepcopy(payload)
        summary, _, embed, claims = self.run_analysis(payload, skip_claims=False)
        self.assertEqual(summary["analysis_status"], "failed")
        self.assertEqual(summary["stages"], {"extract": "failed", "claims": "skipped", "gaps": "skipped"})
        self.assertIn("模型服务连接中断", summary["failure_reason"])
        self.assertTrue(summary["retryable"])
        self.assertEqual(summary["model"]["review_completion"]["completed_review_count"], 0)
        self.assertEqual(summary["model"]["review_completion"]["incomplete_review_count"], 2)
        embed.assert_not_called()
        claims.assert_not_called()
        saved = json.loads((self.root / "analysis/123456/extracted_issues.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, before, "failed outcomes and their errors stay available for retry")

    def test_complete_empty_outcomes_remain_legitimate_completed_analysis(self):
        payload = self.payload([self.record(0), self.record(1, "cached")])
        summary, _, embed, _ = self.run_analysis(payload)
        self.assertEqual(summary["analysis_status"], "completed")
        self.assertEqual(summary["issue_count"], 0)
        self.assertFalse(summary["failure_reason"])
        self.assertFalse(summary["retryable"])
        self.assertEqual(summary["model"]["review_completion"]["completed_review_count"], 2)
        self.assertTrue((self.root / "analysis/123456/statistics.json").is_file())
        embed.assert_not_called()

    def test_partial_extraction_keeps_validated_issues_and_clusters_but_is_not_complete(self):
        payload = self.payload([self.record(0, "ok", issues=[self.issue.to_dict()]),
                                self.record(1, "validation_failed", errors=["timeout"])])
        summary, _, embed, _ = self.run_analysis(payload)
        self.assertEqual(summary["analysis_status"], "partial")
        self.assertEqual((summary["issue_count"], summary["cluster_count"]), (1, 1))
        self.assertEqual(summary["model"]["review_completion"]["completed_review_count"], 1)
        self.assertTrue(summary["retryable"])
        embed.assert_called_once()
        flat = json.loads((self.root / "analysis/123456/issues_flat.json").read_text(encoding="utf-8"))
        self.assertEqual([row["review_id"] for row in flat["issues"]], ["r1"])

    def test_partial_validation_without_any_complete_review_retains_checked_facts(self):
        payload = self.payload([self.record(0, "partial_validation", issues=[self.issue.to_dict()], errors=["other issue invalid"]),
                                self.record(1, "validation_failed", errors=["timeout"])])
        summary, _, embed, _ = self.run_analysis(payload)
        self.assertEqual(summary["analysis_status"], "partial")
        self.assertEqual(summary["model"]["review_completion"]["completed_review_count"], 0)
        self.assertEqual(summary["issue_count"], 1)
        embed.assert_called_once()

    def test_missing_record_is_not_assumed_to_be_a_negative_observation(self):
        summary, _, _, _ = self.run_analysis(self.payload([self.record(0)]))
        self.assertEqual(summary["analysis_status"], "partial")
        self.assertEqual(summary["model"]["review_completion"]["missing_review_count"], 1)

    def test_loading_explicit_failed_extraction_does_not_turn_it_into_a_success(self):
        payload = self.payload([self.record(i, "validation_failed", errors=["timeout"]) for i in range(2)])
        saved = self.root / "failed-extraction.json"
        saved.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        summary, extractor, embed, claims = self.run_analysis(payload, skip_claims=False, from_extracted=saved)
        self.assertEqual(summary["analysis_status"], "failed")
        self.assertTrue(summary["retryable"])
        extractor.assert_not_called()
        embed.assert_not_called()
        claims.assert_not_called()

    def test_raised_transport_error_also_has_a_short_retryable_failure_reason(self):
        summary, _, embed, _ = self.run_analysis(self.payload([]), raises=ConnectionError("remote disconnected"))
        self.assertEqual(summary["analysis_status"], "failed")
        self.assertIn("模型服务连接中断", summary["failure_reason"])
        self.assertTrue(summary["retryable"])
        embed.assert_not_called()

    def complete_issues(self):
        return self.payload([self.record(0, "ok", issues=[self.issue.to_dict()]), self.record(1)])

    def claims_payload(self, *, errors=None, empty=False):
        claims = [] if empty else [{"claim_id": "c1", "aspect": "耐用性", "claim_type": "functional",
                                    "normalized_claim": "衣服耐穿不起球", "original_text": "耐穿不起球"}]
        return {"model": "offline", "claim_count": len(claims), "input_text_count": 1,
                "claims": claims, "errors": errors or []}

    def test_claim_transport_failure_does_not_publish_no_claims_as_completed(self):
        summary, _, _, _ = self.run_analysis(self.complete_issues(), skip_claims=False,
            claim_payload=self.claims_payload(errors=["Connection aborted"], empty=True))
        self.assertEqual(summary["analysis_status"], "partial")
        self.assertEqual(summary["stages"]["claims"], "failed")
        self.assertEqual(summary["issue_count"], 1)
        self.assertIn("商品宣传信息抽取未完成", summary["failure_reason"])
        self.assertTrue(summary["retryable"])

    def test_gap_transport_failure_does_not_publish_insufficient_shell_as_completed(self):
        gap = {"claim_id": "c1", "gap_type": "insufficient_evidence",
               "confidence_basis": "调用或解析失败", "finding": "LLM 复核失败，未生成落差判断"}
        summary, _, _, _ = self.run_analysis(self.complete_issues(), skip_claims=False,
            claim_payload=self.claims_payload(), gap_payload={"gap_count": 1, "gaps": [gap], "errors": ["c1: LLM 复核失败"]})
        self.assertEqual(summary["analysis_status"], "partial")
        self.assertEqual(summary["stages"]["gaps"], "failed")
        self.assertEqual(summary["issue_count"], 1)
        self.assertIn("宣传与评论对照未完成", summary["failure_reason"])

    def test_legitimate_empty_claims_are_completed_without_failure(self):
        summary, _, _, _ = self.run_analysis(self.complete_issues(), skip_claims=False,
                                             claim_payload=self.claims_payload(empty=True))
        self.assertEqual(summary["analysis_status"], "completed")
        self.assertEqual(summary["stages"]["claims"], "ok")
        self.assertEqual(summary["stages"]["gaps"], "skipped")
        self.assertFalse(summary["retryable"])

    def test_legitimate_absence_of_matching_gap_remains_completed(self):
        gap = {"claim_id": "c1", "gap_type": "no_direct_conflict_found",
               "confidence_basis": "没有匹配到相关方面或问题簇", "finding": "未发现直接相关的具体问题"}
        summary, _, _, _ = self.run_analysis(self.complete_issues(), skip_claims=False,
            claim_payload=self.claims_payload(), gap_payload={"gap_count": 1, "gaps": [gap], "errors": []})
        self.assertEqual(summary["analysis_status"], "completed")
        self.assertEqual(summary["stages"]["gaps"], "ok")
        self.assertFalse(summary["retryable"])


if __name__ == "__main__":
    unittest.main()
