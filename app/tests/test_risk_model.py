"""风险指数模型的性质测试。

risk-v2 起，这些测试验证的是"方向性性质"（比较关系、去重、缺失处理、
证据分类与充分性的边界），不是对具体数值的背书——参数仍是工程启发式，
未经统计校准。
"""

import unittest

import reporting
import risk_model

CONCRETE = "穿了两天袖口就起球了，成分标签写的是聚酯纤维"
CONCRETE2 = "洗了一次就掉色，领口也变形，和宣传的纯棉不一致"
CONCRETE3 = "面料和标签对不上，标的是100%聚酯纤维，穿一天就闷汗"
VAGUE = "质量不好"
TOPIC_ONLY = "这个成分我关注过，和图片差不多，买之前也看过介绍"


def unit(review_id, relation="contradiction", text=CONCRETE, **extra):
    return {"review_id": review_id, "relation": relation, "text": text, **extra}


class RiskModelPropertyTest(unittest.TestCase):
    def assess(self, units, **kwargs):
        options = {"scanned_count": 300, "effective_review_count": 7, "scan_complete": True}
        options.update(kwargs)
        return risk_model.assess(units, **options)

    def test_more_independent_contradictions_outweigh_one_vague_comment(self):
        one_vague = self.assess([unit("r1", text=VAGUE)])
        three_specific = self.assess([unit("r1"), unit("r2", text=CONCRETE2), unit("r3", text=CONCRETE3)])
        self.assertLess(one_vague["risk_index"], three_specific["risk_index"])
        # v2：3 条证据进入中风险，但不再直接跳到“高”
        self.assertGreaterEqual(three_specific["risk_index"], risk_model.PARAMS["band_medium"])
        self.assertLess(three_specific["risk_index"], risk_model.PARAMS["band_high"])

    def test_duplicate_review_ids_count_once(self):
        once = self.assess([unit("r1")])
        twice = self.assess([unit("r1"), unit("r1")])
        self.assertEqual(once["risk_index"], twice["risk_index"])

    def test_append_review_is_not_an_independent_evidence(self):
        merged = self.assess([unit("r1"), unit("r1", text="一个月后起球更严重，还掉色")])
        self.assertEqual(merged["contradiction_count"], 1)
        self.assertEqual(merged["risk_index"], self.assess([unit("r1")])["risk_index"])

    def test_identical_text_from_different_users_collapses(self):
        same = self.assess([unit("r1"), unit("r2", text=CONCRETE)])
        self.assertEqual(same["contradiction_count"], 1)

    def test_high_star_review_with_explicit_negative_is_counted(self):
        result = self.assess([unit("r1", star=5)])
        self.assertEqual(result["contradiction_count"], 1)
        self.assertIsNotNone(result["risk_index"])

    def test_support_evidence_lowers_risk(self):
        base = [unit("r1"), unit("r2", text=CONCRETE2), unit("r3", text=CONCRETE3)]
        without = self.assess(base)
        with_support = self.assess(base + [
            unit("s1", relation="support", text="穿了三个月没有起球，洗了五次也没掉色"),
            unit("s2", relation="support", text="成分标签确实是100%棉，手感一致"),
        ])
        self.assertLess(with_support["risk_index"], without["risk_index"])
        self.assertEqual(with_support["support_count"], 2)

    def test_vague_support_never_offsets_risk(self):
        base = [unit("r1")]
        with_vague_support = self.assess(base + [unit("s1", relation="support", text="挺好的")])
        self.assertEqual(with_vague_support["support_count"], 1)
        self.assertEqual(with_vague_support["calculation"]["weight_support"], 0.0)

    def test_topic_mentions_do_not_score(self):
        texts = ["这个成分我关注过，和图片差不多", "包装写着成分表，买之前看过",
                 "介绍里提到过这个成分，我留意了", "商品页有说明，我下单前读过"]
        units = [unit(f"r{i}", relation="partial", text=text) for i, text in enumerate(texts)]
        result = self.assess(units)
        self.assertIsNone(result["risk_index"])
        self.assertEqual(result["relation_structure"]["topic_only"], 4)
        self.assertEqual(result["relation_structure"]["related_negative"], 0)
        self.assertEqual(result["evidence_sufficiency"], "none")

    def test_related_negative_scores_below_contradiction(self):
        related = self.assess([unit("r1", relation="partial", text="分区有点不合理，放东西不太顺手")])
        contradict = self.assess([unit("r1", relation="contradiction", text="分区有点不合理，放东西不太顺手")])
        self.assertLess(related["risk_index"], contradict["risk_index"])
        self.assertEqual(related["relation_structure"]["related_negative"], 1)

    def test_indirect_only_never_reaches_high_band(self):
        units = [unit(f"r{i}", relation="partial", text=f"第{i}次穿感觉有点薄，和图片不太一样")
                 for i in range(6)]
        result = self.assess(units)
        self.assertEqual(result["contradiction_count"], 0)
        self.assertNotEqual(result["risk_band"], "high")
        self.assertLessEqual(result["risk_index"], risk_model.PARAMS["indirect_only_cap"])

    def test_no_negative_evidence_is_not_reported_as_safe(self):
        result = self.assess([])
        self.assertIsNone(result["risk_index"])
        self.assertEqual(result["risk_band"], "none")
        self.assertEqual(result["evidence_sufficiency"], "none")
        self.assertEqual(result["null_reason"], "no_scoring_evidence")
        summary = risk_model.summarize([{"aspect": "功效", "risk_index": None, "risk_band": "none",
                                         "risk_band_label": "暂未形成指数", "contradiction_count": 0,
                                         "possible_count": 0, "evidence_sufficiency": "none"}])
        self.assertIsNone(summary["risk_index"])
        self.assertEqual(summary["risk_band"], "none")

    def test_more_reviews_do_not_dilute_existing_evidence(self):
        few = self.assess([unit("r1"), unit("r2", text=CONCRETE2)], scanned_count=300)
        many = self.assess([unit("r1"), unit("r2", text=CONCRETE2)], scanned_count=30000)
        self.assertEqual(few["risk_index"], many["risk_index"])

    def test_incomplete_scan_caps_sufficiency(self):
        units = [unit("r1"), unit("r2", text=CONCRETE2), unit("r3", text=CONCRETE3)]
        complete = self.assess(units, scan_complete=True)
        partial = self.assess(units, scan_complete=False)
        self.assertEqual(complete["evidence_sufficiency"], "preliminary")
        self.assertEqual(partial["evidence_sufficiency"], "limited")

    def test_sufficiency_never_claims_full_without_high_star_reviews(self):
        units = [unit(f"r{i}", text=f"第{i}天开始起球，成分标签写的是聚酯纤维，洗了{i % 3 + 1}次就变形")
                 for i in range(8)]
        result = self.assess(units, scan_complete=True, reverse_evidence_covered=False)
        self.assertEqual(result["evidence_sufficiency"], "preliminary")
        self.assertTrue(any("4–5 星" in reason for reason in result["sufficiency_reasons"]))

    def test_bounded_scan_can_reach_preliminary_but_not_multi_source(self):
        units = [unit(f"r{i}", text=f"第{i}天开始起球，成分标签写的是聚酯纤维，洗了{i % 3 + 1}次就变形")
                 for i in range(5)]
        bounded = self.assess(units, scan_complete=False, coverage_state="bounded")
        self.assertEqual(bounded["evidence_sufficiency"], "preliminary")
        self.assertIn("明确上限", " ".join(bounded["sufficiency_reasons"]))
        interrupted = self.assess(units, scan_complete=False, coverage_state="interrupted")
        self.assertEqual(interrupted["evidence_sufficiency"], "limited")

    def test_quality_ordering_is_monotonic(self):
        vague = self.assess([unit("r1", text=VAGUE)])
        concrete = self.assess([unit("r1")])
        self.assertLess(vague["risk_index"], concrete["risk_index"])
        self.assertEqual(concrete["evidence_units"][0]["quality"], "concrete")

    def test_media_does_not_auto_upgrade_quality(self):
        plain = risk_model.quality_of("看起来和图片不太一样，有点色差", has_media=False)
        with_media = risk_model.quality_of("看起来和图片不太一样，有点色差", has_media=True)
        self.assertEqual(plain, with_media)

    def test_paraphrased_quote_weights_less(self):
        verbatim = self.assess([unit("r1", quote_status="verbatim")])
        paraphrase = self.assess([unit("r1", quote_status="paraphrase")])
        self.assertLess(paraphrase["risk_index"], verbatim["risk_index"])
        self.assertLess(paraphrase["calculation"]["weight_contradiction"],
                        verbatim["calculation"]["weight_contradiction"])

    def test_workmanship_terms_are_moderate(self):
        self.assertEqual(risk_model.severity_of("到处都是线头，做工粗糙"), "moderate")
        self.assertEqual(risk_model.severity_of("裤子的裆破了"), "major")

    def test_severity_distribution_keeps_mix_of_evidence(self):
        result = self.assess([unit("r1", text="用完过敏红肿"), unit("r2", text=CONCRETE)])
        self.assertEqual(result["severity"], "critical")
        self.assertEqual(result["severity_distribution"]["critical"], 1)
        self.assertEqual(result["severity_distribution"]["major"], 1)

    def test_severity_is_reported_separately_from_frequency(self):
        result = self.assess([unit("r1", text="用完过敏红肿，脸刺痛")])
        self.assertEqual(result["severity"], "critical")
        self.assertTrue(result["safety_alert"])
        # 安全提醒不把单条证据抬成高风险
        self.assertLess(result["risk_index"], risk_model.PARAMS["band_high"])

    def test_params_are_versioned(self):
        result = self.assess([unit("r1")])
        self.assertEqual(result["calculation"]["params_version"], risk_model.PARAMS["version"])
        self.assertIn("formula", result["calculation"])
        self.assertFalse(result["risk_index_calibrated"])

    def test_v1_can_be_recomputed_for_comparison(self):
        units = [unit("r1"), unit("r2", text=CONCRETE2), unit("r3", text=CONCRETE3)]
        v1 = risk_model.assess(units, scanned_count=300, scan_complete=True, model_version="v1")
        v2 = self.assess(units)
        self.assertEqual(v1["risk_model_version"], "risk-v1")
        self.assertGreater(v1["risk_index"], v2["risk_index"])
        self.assertEqual(v1["evidence_sufficiency"], "sufficient")


class SampleCaseTest(unittest.TestCase):
    """需求中的 300 条样本：维度1=3 明确+4 关联，维度2=1 明确+1 关联。"""

    def build(self):
        aspects = {
            "维度1": [unit("a1"), unit("a2", text=CONCRETE2), unit("a3", text=CONCRETE3)] +
                     [unit(f"a{i}", relation="partial", text=f"第{i}天觉得和宣传有点差别，但还能接受")
                      for i in range(4, 8)],
            "维度2": [unit("b1")] + [unit("b2", relation="partial", text="有的批次颜色略深，和图片有点差别")],
            "维度3-9": [],
        }
        return {name: risk_model.assess(units, scanned_count=300, effective_review_count=7,
                                        scan_complete=True) for name, units in aspects.items()}

    def test_aspect1_has_stronger_risk_than_aspect2(self):
        result = self.build()
        self.assertGreater(result["维度1"]["risk_index"], result["维度2"]["risk_index"])
        self.assertGreater(result["维度1"]["contradiction_count"], result["维度2"]["contradiction_count"])

    def test_legacy_rates_remain_available_for_comparison(self):
        result = self.build()
        self.assertAlmostEqual(result["维度1"]["contradiction_count"] / 300, 0.01, places=4)
        self.assertAlmostEqual(result["维度2"]["contradiction_count"] / 300, 0.0033, places=4)

    def test_dimensions_without_evidence_have_no_index(self):
        result = self.build()
        self.assertIsNone(result["维度3-9"]["risk_index"])

    def test_summary_uses_max_aspect_not_sum(self):
        result = self.build()
        aspects = [{"aspect": name, **data} for name, data in result.items()]
        summary = risk_model.summarize(aspects)
        self.assertEqual(summary["risk_index"], result["维度1"]["risk_index"])
        self.assertEqual(summary["top_aspect"], "维度1")


class ReportingIntegrationTest(unittest.TestCase):
    def build_model(self, reviews, gaps):
        claims = [{"claim_id": "c1", "aspect": "质量与做工", "normalized_claim": "100%纯棉不起球",
                   "original_text": "100%纯棉不起球"}]
        return reporting.build_report_model(
            summary={"product_id": "p", "valid_review_count": len(reviews)},
            reviews=reviews, claims=claims, gaps=gaps, product={"title": "测试"},
        )

    def test_report_exposes_risk_index_and_legacy_rate(self):
        reviews = [{"review_id": "r1", "star": 2, "content": CONCRETE, "valid_for_analysis": True}]
        gaps = [{"claim_id": "c1", "gap_type": "potential_conflict", "confidence_level": "high",
                 "evidence_review_ids": ["r1"], "finding": "起球", "review_evidence": []}]
        model = self.build_model(reviews, gaps)
        aspect = model["aspects"][0]
        self.assertIsNotNone(aspect["risk_index"])
        self.assertEqual(aspect["contradiction_count"], 1)
        self.assertIsNotNone(aspect["legacy_direct_rate"])
        self.assertIn("risk_distribution", model["overview"])
        self.assertIn("risk_summary", model["overview"])
        self.assertEqual(aspect["risk_model_version"], "risk-v2")
        self.assertIn("legacy_risk", aspect)
        self.assertFalse(aspect["risk_index_calibrated"])

    def test_duplicate_evidence_across_claims_counts_once(self):
        reviews = [{"review_id": "r1", "star": 2, "content": CONCRETE, "valid_for_analysis": True}]
        gaps = [
            {"claim_id": "c1", "gap_type": "potential_conflict", "evidence_review_ids": ["r1"], "finding": ""},
            {"claim_id": "c2", "gap_type": "potential_conflict", "evidence_review_ids": ["r1"], "finding": ""},
        ]
        model = self.build_model(reviews, gaps)
        aspect = model["aspects"][0]
        self.assertEqual(aspect["contradiction_count"], 1)
        self.assertEqual(aspect["risk_calculation"]["weight_contradiction"],
                         risk_model.assess([{"review_id": "r1", "relation": "contradiction",
                                             "text": CONCRETE, "quote_status": "original_review"}]
                                           )["calculation"]["weight_contradiction"])

    def test_aspect_without_gap_has_null_risk(self):
        reviews = [{"review_id": "r1", "star": 2, "content": CONCRETE, "valid_for_analysis": True}]
        gaps = [{"claim_id": "c1", "gap_type": "no_direct_conflict_found", "evidence_review_ids": [],
                 "finding": "未发现"}]
        model = self.build_model(reviews, gaps)
        aspect = model["aspects"][0]
        self.assertIsNone(aspect["risk_index"])
        self.assertEqual(aspect["risk_band"], "none")

    def test_topic_only_partial_gap_is_not_scored(self):
        reviews = [{"review_id": "r1", "star": 2, "content": TOPIC_ONLY, "valid_for_analysis": True}]
        gaps = [{"claim_id": "c1", "gap_type": "partial_gap", "evidence_review_ids": ["r1"],
                 "finding": "略有差异", "review_evidence": []}]
        model = self.build_model(reviews, gaps)
        aspect = model["aspects"][0]
        self.assertIsNone(aspect["risk_index"])
        self.assertEqual(aspect["evidence_structure"]["topic_only"], 1)

    def test_severity_uses_full_review_not_only_the_quote(self):
        reviews = [{"review_id": "r1", "star": 1,
                    "content": "用了两天脸就过敏红肿，刺痒得厉害，包装也很简单",
                    "valid_for_analysis": True}]
        gaps = [{"claim_id": "c1", "gap_type": "potential_conflict", "evidence_review_ids": ["r1"],
                 "finding": "过敏",
                 "evidence_detail": [{"review_id": "r1", "quote": "包装也很简单",
                                      "relation": "contradiction", "quality": "concrete"}]}]
        model = self.build_model(reviews, gaps)
        aspect = model["aspects"][0]
        self.assertEqual(aspect["severity"], "critical")
        self.assertTrue(aspect["safety_alert"])

    def test_issue_only_aspects_are_listed_separately(self):
        reviews = [{"review_id": "r1", "star": 1, "content": "到处都是线头，做工粗糙",
                    "valid_for_analysis": True}]
        claims = [{"claim_id": "c1", "aspect": "版型与合身度",
                   "normalized_claim": "版型宽松", "original_text": "宽松"}]
        gaps = [{"claim_id": "c1", "gap_type": "no_direct_conflict_found",
                 "evidence_review_ids": [], "finding": ""}]
        statistics = {"clusters": [{"cluster_id": "C1", "aspect": "做工质量",
                                    "problem_name": "整体线头", "description": "",
                                    "review_ids": ["r1"]}]}
        model = reporting.build_report_model(
            summary={"product_id": "p", "valid_review_count": 1},
            statistics=statistics, reviews=reviews, claims=claims, gaps=gaps,
            product={"title": "测试"},
        )
        self.assertEqual(len(model["product_issues"]), 1)
        issue = model["product_issues"][0]
        self.assertEqual(issue["aspect"], "做工质量")
        self.assertEqual(issue["problem_name"], "整体线头")
        self.assertEqual(model["overview"]["issue_only_review_count"], 1)
        by_name = {row["aspect"]: row for row in model["aspects"]}
        self.assertEqual(by_name["版型与合身度"]["null_reason"], "no_scoring_evidence")
        self.assertEqual(by_name["做工质量"]["null_reason"], "no_claim")

    def test_quote_status_is_verified_against_original_text(self):
        reviews = [{"review_id": "r1", "star": 2, "content": CONCRETE, "valid_for_analysis": True}]
        gaps = [{"claim_id": "c1", "gap_type": "potential_conflict", "evidence_review_ids": ["r1"],
                 "finding": "起球",
                 "evidence_detail": [{"review_id": "r1", "quote": "袖口就起球了", "relation": "contradiction",
                                      "quality": "concrete", "severity": "major"}]}]
        model = self.build_model(reviews, gaps)
        evidence = model["aspects"][0]["risk_evidence"][0]
        self.assertEqual(evidence["quote_status"], "verbatim")

        gaps[0]["evidence_detail"][0]["quote"] = "完全不一样的句子，原文里没有"
        model = self.build_model(reviews, gaps)
        evidence = model["aspects"][0]["risk_evidence"][0]
        self.assertEqual(evidence["quote_status"], "paraphrase")


if __name__ == "__main__":
    unittest.main()
