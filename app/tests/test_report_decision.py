"""Decision display scores do not invent probabilities or dilute strong signals."""
import copy
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report_decision
import reporting
from test_report_probability import report_case


def ad_case(count=2):
    reviews = [{"review_id": f"r{i}", "content": "就是没有图片中宽松肥大", "star": 2,
                "sku": "蓝色;M", "date": "2026-10-25", "valid_for_analysis": True} for i in range(count)]
    units = [{"review_id": r["review_id"], "text": r["content"], "quality": "partial",
              "relation": "contradiction", "quote_status": "verbatim", "provided_quote": True} for r in reviews]
    return dict(aspects=[{"aspect": "版型与合身度", "claim_count": 1}],
        recurrence={"as_of": "2026-10-29", "windows": {"30": {"problems": []}}},
        reviews=reviews, advertised_units={"版型与合身度": units}, counts={"scanned_review_count": 300})


def p_problem(probability, *, advertised=False, aspect="耐用性", name="衣服起球", rank=0):
    return {"problem_id": str(rank), "aspect": aspect, "name": name, "unadvertised": not advertised,
            "probability_estimate": {"status": "estimated", "estimate": probability}, "review_ids": ["extra1"],
            "evidence": [], "attention_level": "repeated", "repeat_count": 2}


class ReportDecisionTests(unittest.TestCase):
    def test_two_separate_short_identical_feedback_are_not_one_unit(self):
        result = report_decision.build(**ad_case())
        row = result["advertised_aspects"][0]
        self.assertEqual(row["contradiction_count"], 2)
        self.assertEqual(row["index"], 71.3)
        self.assertEqual(row["band"], "high")

    def test_one_explicit_verified_contradiction_is_visible_with_uncertainty_separate(self):
        result = report_decision.build(**ad_case(1))["advertised_aspects"][0]
        self.assertEqual(result["index"], 55.4)
        self.assertEqual(result["band"], "medium")

    def test_repeated_claims_same_review_do_not_raise_strength(self):
        data = ad_case(1)
        data["advertised_units"]["版型与合身度"] *= 3
        result = report_decision.build(**data)["advertised_aspects"][0]
        self.assertEqual(result["contradiction_count"], 1)
        self.assertEqual(result["index"], 55.4)

    def test_same_stable_account_dedup_and_no_identity_export(self):
        data = ad_case()
        for r in data["reviews"]:
            r["buyer_id"] = "private-source-123"
        result = report_decision.build(**data)
        self.assertEqual(result["advertised_aspects"][0]["contradiction_count"], 1)
        self.assertNotIn("private-source-123", json.dumps(result))

    def test_long_near_copies_dedup(self):
        data = ad_case()
        for r, unit in zip(data["reviews"], data["advertised_units"]["版型与合身度"]):
            r["content"] = "就是没有图片中宽松肥大" * 5
            unit["text"] = r["content"]
        self.assertEqual(report_decision.build(**data)["advertised_aspects"][0]["contradiction_count"], 1)

    def test_topic_or_related_or_unchecked_quote_cannot_form_ad_index(self):
        for field, value in (("relation", "related_negative"), ("relation", "topic_only"),
                             ("quote_status", "paraphrase"), ("text", "没在原文里的反驳")):
            with self.subTest(field=field, value=value):
                data = ad_case(1)
                data["advertised_units"]["版型与合身度"][0][field] = value
                self.assertIsNone(report_decision.build(**data)["advertised"]["index"])

    def test_vague_positive_quote_is_not_negative_evidence(self):
        data = ad_case(1)
        data["reviews"][0]["content"] = "没有掉色，穿起来很舒服"
        data["advertised_units"]["版型与合身度"][0]["text"] = data["reviews"][0]["content"]
        data["advertised_units"]["版型与合身度"][0]["relation"] = "support"
        self.assertIsNone(report_decision.build(**data)["advertised"]["index"])

    def test_index_is_monotone_in_independent_corroboration_and_bounded(self):
        indices = [report_decision.build(**ad_case(n))["advertised"]["index"] for n in (1, 2, 4, 20)]
        self.assertEqual(indices, sorted(indices))
        self.assertTrue(all(0 < n <= 100 for n in indices))

    def test_additional_scanned_positive_reviews_do_not_dilute(self):
        data = ad_case()
        before = report_decision.build(**data)["advertised"]
        data["counts"]["scanned_review_count"] = 20000
        self.assertEqual(report_decision.build(**data)["advertised"], before)

    def test_strong_ad_is_not_diluted_by_low_extra(self):
        data = ad_case(4)
        data["recurrence"]["windows"]["30"]["problems"] = [p_problem(.05)]
        window = report_decision.build(**data)["windows"]["30"]
        self.assertEqual(window["overall"]["index"], window["advertised"]["index"])
        self.assertEqual(window["overall"]["dominant"], "advertised")

    def test_strong_extra_is_not_diluted_by_no_ad(self):
        data = ad_case(0)
        data["recurrence"]["windows"]["30"]["problems"] = [p_problem(.9)]
        window = report_decision.build(**data)["windows"]["30"]
        self.assertEqual(window["overall"]["index"], 90)
        self.assertEqual(window["overall"]["missing_components"], ["advertised"])
        self.assertEqual(window["overall"]["status"], "partial")
        self.assertFalse(window["overall"]["is_probability"])

    def test_unknown_is_not_green_zero(self):
        window = report_decision.build(**ad_case(0))["windows"]["30"]
        self.assertIsNone(window["overall"]["index"])
        self.assertEqual(window["overall"]["band"], "none")

    def test_extra_problem_selection_uses_evidence_order_not_max_probability(self):
        data = ad_case(0)
        data["recurrence"]["windows"]["30"]["problems"] = [p_problem(.375, rank=1), p_problem(.9, rank=2)]
        window = report_decision.build(**data)["windows"]["30"]
        self.assertEqual(window["unadvertised"]["index"], 37.5)
        self.assertEqual(window["unadvertised"]["aspects"][0]["assessment"]["problem_id"], "1")

    def test_window_excludes_old_unknown_and_future_ad_evidence(self):
        data = ad_case(3)
        for review, day in zip(data["reviews"], ("2020-01-01", "", "2026-12-01")):
            review["date"] = day
        result = report_decision.build(**data)
        self.assertIsNotNone(result["advertised"]["index"])
        self.assertIsNone(result["windows"]["30"]["advertised"]["index"])
        self.assertEqual(result["windows"]["30"]["advertised"]["aspects"][0]["excluded_date_count"], 3)

    def test_ad_probability_matches_specific_issue_not_whole_aspect_or_review(self):
        data = ad_case(1)
        unit = data["advertised_units"]["版型与合身度"][0]
        unit["text"] = "衣服偏薄，款式很宽松"
        data["reviews"][0]["content"] = unit["text"]
        problem = p_problem(.9, advertised=True, aspect="版型与合身度")
        problem["evidence"] = [{"review_id": "r0", "specific": True, "quote": "衣服偏硬"}]
        data["recurrence"]["windows"]["30"]["problems"] = [problem]
        self.assertIsNone(report_decision.build(**data)["windows"]["30"]["advertised"]["probability_assessment"]["probability"])
        problem["evidence"][0]["quote"] = "衣服偏薄"
        self.assertEqual(report_decision.build(**data)["windows"]["30"]["advertised"]["probability_assessment"], problem)

    def test_no_targeted_quote_does_not_turn_all_review_issues_into_ad_probability(self):
        data = ad_case(1)
        unit = data["advertised_units"]["版型与合身度"][0]
        unit.update(text="面料偏薄", provided_quote=False, quote_status="original_review")
        data["reviews"][0]["content"] = "面料偏薄"
        problem = p_problem(.9, advertised=True, aspect="版型与合身度")
        problem["evidence"] = [{"review_id": "r0", "specific": True, "quote": "面料偏薄"}]
        data["recurrence"]["windows"]["30"]["problems"] = [problem]
        window = report_decision.build(**data)["windows"]["30"]
        self.assertIsNotNone(window["advertised"]["index"])
        self.assertIsNone(window["advertised"]["probability_assessment"]["probability"])

    def test_same_aspect_unmatched_specific_problem_is_kept_as_extra(self):
        data = ad_case(1)
        data["reviews"][0]["content"] = "就是没有图片中宽松肥大，面料偏薄，穿起来很僵硬。"
        problem = p_problem(.9, advertised=True, aspect="版型与合身度", name="面料偏薄")
        problem["evidence"] = [{"review_id": "r0", "specific": True, "quote": "面料偏薄"}]
        problem["probability_estimate"].update(successes=4, total=4, interval=[.555, .999])
        data["recurrence"]["windows"]["30"]["problems"] = [problem]
        before = copy.deepcopy(data)
        window = report_decision.build(**data)["windows"]["30"]
        self.assertEqual(data, before)
        extra = window["unadvertised"]
        self.assertEqual(extra["index"], 90)
        self.assertEqual(extra["problems"][0]["scope_reason"], "not_established_ad_contradiction")
        self.assertFalse(extra["problems"][0]["unadvertised"])
        self.assertEqual(extra["problems"][0]["probability_estimate"], problem["probability_estimate"])
        self.assertEqual(window["overall"]["index"], 90)
        self.assertNotIn("scope_reason", problem)

    def test_extra_scope_preserves_no_claim_provenance(self):
        data = ad_case(0)
        data["recurrence"]["windows"]["30"]["problems"] = [p_problem(.9)]
        extra = report_decision.build(**data)["windows"]["30"]["unadvertised"]
        self.assertEqual(extra["problems"][0]["scope_reason"], "no_claim_in_aspect")
        self.assertTrue(extra["problems"][0]["unadvertised"])
        self.assertEqual(extra["probability_label"], "额外问题复现概率")

    def test_real_report_integrates_counts_extra_and_keeps_inputs_and_legacy(self):
        data = report_case()
        before = copy.deepcopy(data)
        model = reporting.build_report_model(**data)
        self.assertEqual(data, before)
        result = model["report_decision"]
        self.assertEqual(result["counts"]["scanned_review_count"], 30)
        self.assertEqual(result["counts"]["claim_aspect_count"], 0)
        self.assertEqual(result["windows"]["30"]["unadvertised"]["index"], 90)
        self.assertIsNone(result["windows"]["30"]["advertised"]["index"])
        self.assertIn("risk_summary", model["overview"])
        exported = json.loads(reporting.artifact_bytes(model, "reviews_json"))
        self.assertEqual(exported["report_decision"], result)

    def test_real_ad_association_and_legacy_model_remain_separate(self):
        data = report_case()
        data["claims"] = [{"claim_id": "c1", "aspect": "耐用性", "normalized_claim": "面料耐磨不起球", "original_text": "耐磨不起球"}]
        data["gaps"] = [{"claim_id": "c1", "gap_type": "potential_conflict", "confidence_level": "high",
            "evidence_review_ids": [r["review_id"] for r in data["reviews"]],
            "review_evidence": [{"review_id": r["review_id"], "evidence": r["content"]} for r in data["reviews"]]}]
        model = reporting.build_report_model(**data)
        decision = model["report_decision"]["windows"]["30"]
        self.assertEqual(decision["advertised"]["probability_assessment"]["probability"], .9)
        self.assertIsNone(decision["unadvertised"]["index"])
        self.assertEqual(decision["unadvertised"]["problems"], [])
        self.assertGreater(decision["advertised"]["index"], model["aspects"][0]["risk_index"])
        self.assertEqual(model["aspects"][0]["risk_model_version"], "risk-v2")

    def test_real_claimed_aspect_different_defect_does_not_disappear(self):
        data = report_case()
        data["claims"] = [{"claim_id": "c1", "aspect": "耐用性", "normalized_claim": "面料色牢度好不掉色", "original_text": "色牢度好不掉色"}]
        model = reporting.build_report_model(**data)
        source = model["issue_evidence"]["windows"]["30"]["problems"][0]
        self.assertFalse(source["unadvertised"])
        self.assertNotIn("scope_reason", source)
        self.assertEqual(model["product_issues"], [])
        decision = model["report_decision"]["windows"]["30"]
        self.assertIsNone(decision["advertised"]["index"])
        self.assertEqual(decision["unadvertised"]["index"], 90)
        self.assertEqual(decision["unadvertised"]["problems"][0]["scope_reason"], "not_established_ad_contradiction")
        self.assertEqual(decision["unadvertised"]["probability_assessment"]["probability_estimate"], source["probability_estimate"])

    def test_pdf_and_xlsx_primary_ad_index_matches_new_decision(self):
        from openpyxl import load_workbook
        from pypdf import PdfReader
        data = report_case()
        data["claims"] = [{"claim_id": "c1", "aspect": "耐用性", "normalized_claim": "面料耐磨不起球", "original_text": "耐磨不起球"}]
        data["gaps"] = [{"claim_id": "c1", "gap_type": "potential_conflict", "confidence_level": "high",
            "evidence_review_ids": [r["review_id"] for r in data["reviews"]],
            "review_evidence": [{"review_id": r["review_id"], "evidence": r["content"]} for r in data["reviews"]]}]
        model = reporting.build_report_model(**data)
        index = model["report_decision"]["advertised_aspects"][0]["index"]
        sheet = load_workbook(io.BytesIO(reporting.generate_xlsx_bytes(model)))["风险概览"]
        self.assertEqual(sheet["B4"].value, index)
        self.assertEqual(sheet["C4"].value, "高风险指数")
        pdf = PdfReader(io.BytesIO(reporting.generate_pdf_bytes(model)))
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        self.assertIn(f"{index:.1f}/100", text)
        self.assertIn("综合关注指数", text)
        self.assertIn("宣传信息维度", text)


if __name__ == "__main__":
    unittest.main()
