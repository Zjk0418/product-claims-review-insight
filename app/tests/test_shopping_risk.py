"""购物提醒不能因商家没有宣传承诺而消失。"""
import copy
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reporting
import shopping_risk


def case():
    # 对应用户截图的采样口径；这些评论是确定性测试数据，不是新增实采。
    reviews = [{"review_id": str(i), "star": 2, "text": f"洗了{i + 1}次后线头散开了，做工比较差。",
                "valid_for_analysis": i < 22} for i in range(33)]
    return dict(summary={"product_id": "shopping-test", "analysis_status": "completed", "valid_review_count": 22},
                reviews=reviews, claims=[], gaps=[],
                statistics={"clusters": [
                    {"cluster_id": "a", "aspect": "做工质量", "problem_name": "洗后开线", "review_ids": ["0", "1", "2", "3", "4"]},
                    {"cluster_id": "b", "aspect": "做工质量", "problem_name": "线头多", "review_ids": ["3", "4", "5"]},
                    {"cluster_id": "c", "aspect": "气味", "problem_name": "气味刺鼻", "review_ids": ["5", "6"]},
                ]},
                collection={"scanned_count": 300, "matched_count": 33, "status": "partial", "stop_reason": "达到扫描上限300条"})


class ShoppingRiskTests(unittest.TestCase):
    def test_no_claim_still_warns_and_counts_are_not_probabilities(self):
        model = reporting.build_report_model(**case())
        self.assertEqual(model["sampling"]["low_star_rate"], .11)
        self.assertEqual(model["sampling"]["effective_negative_review_count"], 22)
        by_aspect = {a["aspect"]: a for a in model["aspects"]}
        sewing = by_aspect["做工质量"]
        self.assertIsNone(sewing["risk_index"])
        self.assertEqual(sewing["claim_count"], 0)
        self.assertEqual(sewing["shopping"]["negative_count"], 6)
        self.assertEqual(sewing["shopping"]["scanned_rate"], .02)
        self.assertEqual(sewing["shopping"]["effective_share"], round(6 / 22, 6))
        self.assertEqual(sewing["shopping"]["attention_level"], "concentrated")
        shopping = model["overview"]["shopping"]
        self.assertEqual(shopping["negative_review_count"], 7)  # 跨方面去重
        self.assertEqual(shopping["repeated_aspect_count"], 2)
        self.assertEqual(shopping["unclassified_effective_count"], 15)
        self.assertIn("建议重点核查", shopping["attention_label"])
        self.assertFalse(shopping["probability_calibrated"])

    def test_absent_vs_no_conflict_claim_cannot_change_shopping_signal(self):
        data = case()
        before = reporting.build_report_model(**data)
        data["claims"] = [{"claim_id": "c1", "aspect": "做工质量", "normalized_claim": "做工精细", "original_text": "做工精细"}]
        data["gaps"] = [{"claim_id": "c1", "gap_type": "no_direct_conflict_found", "evidence_review_ids": []}]
        after = reporting.build_report_model(**data)
        self.assertEqual(before["overview"]["shopping"], after["overview"]["shopping"])

    def test_invalid_ids_and_topic_only_are_excluded(self):
        data = case()
        data["statistics"]["clusters"][0]["review_ids"].extend(["0", "32", "missing"])
        data["claims"] = [{"claim_id": "c1", "aspect": "容量", "normalized_claim": "500ml"}]
        data["gaps"] = [{"claim_id": "c1", "gap_type": "partial_gap", "evidence_review_ids": ["7"],
                         "review_evidence": [{"review_id": "7", "evidence": "瓶子上写有容量"}],
                         "evidence_detail": [{"review_id": "7", "relation": "topic_only"}]}]
        model = reporting.build_report_model(**data)
        self.assertEqual(model["overview"]["shopping"]["negative_review_count"], 7)
        self.assertEqual(next(a for a in model["aspects"] if a["aspect"] == "容量")["shopping"]["negative_count"], 0)

    def test_safety_comes_from_this_problem_not_another_sentence(self):
        data = case()
        data["reviews"][0]["text"] = "客服态度差，使用产品以后过敏。"
        data["statistics"]["clusters"] = [
            {"aspect": "客服与售后", "problem_name": "客服态度差", "review_ids": ["0"]},
            {"aspect": "使用舒适性", "problem_name": "使用后过敏", "review_ids": ["0"]}]
        model = reporting.build_report_model(**data)
        by_aspect = {a["aspect"]: a for a in model["aspects"]}
        self.assertEqual(by_aspect["客服与售后"]["shopping"]["attention_level"], "single")
        self.assertEqual(by_aspect["使用舒适性"]["shopping"]["attention_level"], "safety")
        self.assertEqual(model["overview"]["shopping"]["safety_aspects"], ["使用舒适性"])

    def test_missing_denominator_and_no_issues_never_mean_safe(self):
        model = reporting.build_report_model(summary={"valid_review_count": 5})
        self.assertEqual(model["overview"]["shopping"]["unclassified_effective_count"], 5)
        self.assertIn("请核查低星正文", model["overview"]["shopping"]["attention_label"])
        model = reporting.build_report_model()
        self.assertIn("不足以判断", model["overview"]["shopping"]["attention_label"])

    def test_three_star_positive_body_is_eligible_but_not_a_negative_finding(self):
        model = reporting.build_report_model(reviews=[{"review_id": "r1", "star": 3,
            "text": "尺码合适，穿着很舒适", "valid_for_analysis": True}])
        self.assertEqual(model["overview"]["valid_review_count"], 1)
        self.assertEqual(model["overview"]["shopping"]["negative_review_count"], 0)
        self.assertEqual(model["overview"]["shopping"]["aspect_count"], 0)
        self.assertNotIn("已发现负面反馈", model["overview"]["shopping"]["attention_label"])

    def test_history_scope_filter_preserves_specifications_and_effects(self):
        claims = [
            {"claim_id": "spec", "aspect": "尺寸与规格", "claim_type": "size_spec", "normalized_claim": "商品为680g×2沐浴露", "original_text": "净含量680g×2"},
            {"claim_id": "effect", "aspect": "功效", "claim_type": "functional", "normalized_claim": "商品为补水保湿配方", "original_text": "补水保湿配方"},
            {"claim_id": "category", "aspect": "其他", "claim_type": "objective_param", "normalized_claim": "商品为沐浴露", "original_text": "补水保湿沐浴露680g×2"},
            {"claim_id": "platform", "aspect": "价格与性价比", "claim_type": "price_value", "normalized_claim": "划线价格仅供参考", "original_text": "划线价格仅供参考"},
        ]
        model = reporting.build_report_model(claims=claims, gaps=[{"claim_id": "platform"}])
        self.assertEqual({c["claim_id"] for c in model["claims"]}, {"spec", "effect"})
        self.assertEqual({c["claim_id"] for c in model["excluded_claims"]}, {"category", "platform"})
        self.assertEqual(model["gaps"], [])

    def test_inputs_unchanged_all_problem_evidence_available(self):
        data = case()
        before = copy.deepcopy(data)
        model = reporting.build_report_model(**data)
        self.assertEqual(data, before)
        for aspect in model["aspects"]:
            self.assertEqual(len(aspect["shopping"]["evidence"]), aspect["shopping"]["negative_count"])
            self.assertEqual({r["review_id"] for r in aspect["shopping"]["evidence"]}, set(aspect["shopping"]["review_ids"]))

    def test_pdf_includes_unclaimed_shopping_problems(self):
        from pypdf import PdfReader
        model = reporting.build_report_model(**case())
        pdf = PdfReader(io.BytesIO(reporting.generate_pdf_bytes(model)))
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        for phrase in ("购物问题与证据", "做工质量", "气味", "11.00%", "重点关注", "未发现该方面的可比宣传承诺"):
            self.assertIn(phrase, text)
        self.assertNotIn("最高风险指数", text)
        self.assertLessEqual(len(pdf.pages), 3)


if __name__ == "__main__":
    unittest.main()
