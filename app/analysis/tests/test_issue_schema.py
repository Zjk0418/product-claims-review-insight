import unittest

from analysis.categories import get_profile
from analysis.issue_schema import (
    check_evidence,
    direction_polarity,
    normalize_direction,
    normalize_severity,
    validate_issue,
)


class IssueSchemaTest(unittest.TestCase):
    def test_evidence_exact_and_normalized(self):
        text = "鞋的头部较小，后脚跟较硬，穿的脚疼"
        self.assertEqual(check_evidence("鞋的头部较小", text), "exact")
        self.assertEqual(check_evidence("鞋的头部较小，", text), "exact")
        self.assertEqual(check_evidence("鞋的头部 较小", text), "normalized")
        self.assertEqual(check_evidence("完全不存在的描述", text), "failed")

    def test_direction_aliases(self):
        self.assertEqual(normalize_direction("偏小")[0], "too_small")
        self.assertEqual(normalize_direction("太窄")[0], "too_narrow")
        self.assertEqual(normalize_direction("开胶")[0], "unglued")
        self.assertEqual(normalize_direction("")[0], "other")

    def test_opposite_directions_have_different_polarity(self):
        self.assertEqual(direction_polarity("too_small"), ("scale", "small"))
        self.assertEqual(direction_polarity("too_narrow"), ("scale", "small"))
        self.assertEqual(direction_polarity("too_large"), ("scale", "large"))
        self.assertNotEqual(direction_polarity("too_hard"), direction_polarity("too_soft"))

    def test_validate_issue_rejects_fake_evidence(self):
        review = {"review_id": "r1", "text": "鞋底太硬，走路脚疼", "star": 1, "sku_id": "s1"}
        payload = {
            "object": "鞋底",
            "aspect": "穿着舒适性",
            "manifestation": "鞋底过硬",
            "direction": "太硬",
            "severity": "high",
            "evidence": "鞋底很硬",  # 与原文不一致
            "confidence": 0.9,
        }
        issue, errors = validate_issue(payload, review, profile=get_profile("shoe"))
        self.assertIsNone(issue)
        self.assertTrue(any("evidence" in error for error in errors))

    def test_validate_issue_ok(self):
        review = {"review_id": "r1", "text": "鞋底太硬，走路脚疼", "star": 1, "sku_id": "s1"}
        payload = {
            "object": "鞋底",
            "aspect": "穿着舒适性",
            "manifestation": "鞋底过硬",
            "direction": "太硬",
            "severity": "high",
            "evidence": "鞋底太硬",
            "confidence": 0.9,
            "basis": "explicit",
        }
        issue, errors = validate_issue(payload, review, profile=get_profile("shoe"))
        self.assertEqual(errors, [])
        self.assertIsNotNone(issue)
        self.assertEqual(issue.direction, "too_hard")
        self.assertEqual(issue.aspect, "穿着舒适性")
        self.assertEqual(issue.evidence_check, "exact")
        self.assertEqual(normalize_severity(issue.severity), "high")


if __name__ == "__main__":
    unittest.main()
