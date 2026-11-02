"""标签规范化测试（v1.9）。

守住三类风险：
1. 同义表达要统一（面部/脸/肌肤 → 皮肤）；
2. 不同对象绝不合并（鞋底/鞋垫、包装/整体、尺码/鞋头、痘印/痘痘）；
3. 跨品类同名不同义（材质、分区）不能一律套用。
"""
from __future__ import annotations

import unittest

from analysis.categories import get_profile
from analysis.issue_schema import validate_issue
from analysis.normalization import (
    defect_signatures,
    normalize_aspect_label,
    normalize_direction_label,
    normalize_issue_labels,
    normalize_object,
    primary_defect_signature,
    signatures_compatible,
)

BEAUTY = get_profile("beauty")
SHOE = get_profile("shoe")
BAG = get_profile("bag")
HOME = get_profile("home")
FOOD = get_profile("food")


class ObjectNormalizationTest(unittest.TestCase):
    def test_face_synonyms_merge_to_skin(self) -> None:
        for raw in ("面部", "脸", "脸上", "肌肤", "面部皮肤"):
            value, method, _ambiguous = normalize_object(raw, BEAUTY)
            self.assertEqual(value, "皮肤", raw)
            self.assertIn(method, {"synonym", "canonical_alias", "exact"})

    def test_acne_and_acne_mark_stay_separate(self) -> None:
        self.assertEqual(normalize_object("痘痘", BEAUTY)[0], "痘痘")
        self.assertEqual(normalize_object("痘印", BEAUTY)[0], "痘印")
        self.assertNotEqual(
            normalize_object("痘痘", BEAUTY)[0], normalize_object("痘印", BEAUTY)[0]
        )

    def test_shoe_sole_and_insole_never_merge(self) -> None:
        self.assertEqual(normalize_object("鞋底材质", SHOE)[0], "鞋底")
        self.assertEqual(normalize_object("鞋垫", SHOE)[0], "鞋垫")
        self.assertNotEqual(normalize_object("鞋底", SHOE)[0], normalize_object("鞋垫", SHOE)[0])

    def test_shoe_size_and_toe_never_merge(self) -> None:
        self.assertEqual(normalize_object("尺码", SHOE)[0], "尺码")
        self.assertEqual(normalize_object("鞋头偏窄", SHOE)[0], "鞋头")

    def test_packaging_is_not_whole_product(self) -> None:
        # 修复旧 object_key 的并列长度问题：不能把“商品包装”归成“整体”
        self.assertEqual(normalize_object("商品包装", SHOE)[0], "包装")
        self.assertEqual(normalize_object("外包装", SHOE)[0], "包装")
        self.assertNotEqual(normalize_object("包装", SHOE)[0], normalize_object("整体", SHOE)[0])

    def test_back_heel_synonyms(self) -> None:
        for raw in ("后跟", "脚跟", "后脚跟"):
            self.assertEqual(normalize_object(raw, SHOE)[0], "后跟", raw)

    def test_unknown_label_is_kept_and_flagged(self) -> None:
        value, method, ambiguous = normalize_object("磁吸扣", BAG)
        self.assertEqual(value, "磁吸扣")
        self.assertEqual(method, "unmapped")
        self.assertTrue(ambiguous)

    def test_cross_category_same_word_different_object(self) -> None:
        # “分区”在箱包是内袋，在美妆不是同一对象：不能粗暴套用
        self.assertEqual(normalize_object("分区", BAG)[0], "内袋")
        value, method, ambiguous = normalize_object("分区", BEAUTY)
        self.assertEqual(value, "分区")
        self.assertEqual(method, "unmapped_other_category")
        self.assertTrue(ambiguous)

    def test_material_in_bag_vs_home(self) -> None:
        self.assertEqual(normalize_object("材质", BAG)[0], "面料")
        self.assertEqual(normalize_object("材质", HOME)[0], "材质")

    def test_empty_object(self) -> None:
        value, method, ambiguous = normalize_object("", BEAUTY)
        self.assertEqual(value, "未知")
        self.assertEqual(method, "empty")
        self.assertTrue(ambiguous)


class AspectNormalizationTest(unittest.TestCase):
    def test_adverse_reaction_maps_to_efficacy(self) -> None:
        for text in ("本来不长痘的，用了长痘了", "过敏了", "皮肤泛红刺痛"):
            aspect, method, _ = normalize_aspect_label("使用舒适性", BEAUTY, text)
            self.assertEqual(aspect, "功效与效果", text)
            self.assertEqual(method, "context_rule")

    def test_texture_stays_comfort(self) -> None:
        for text in ("用完感觉脸很黏腻", "上脸搓泥", "拔干紧绷"):
            aspect, _method, _ = normalize_aspect_label("使用舒适性", BEAUTY, text)
            self.assertEqual(aspect, "使用舒适性", text)

    def test_negated_reaction_not_remapped(self) -> None:
        aspect, _method, _ = normalize_aspect_label("使用舒适性", BEAUTY, "用了没有过敏")
        self.assertEqual(aspect, "使用舒适性")

    def test_shoe_rubbing_keeps_legacy_label(self) -> None:
        aspect, method, _ = normalize_aspect_label("使用舒适性", SHOE, "磨脚")
        self.assertEqual(aspect, "穿着舒适性")

    def test_food_taste_keyword(self) -> None:
        aspect, method, _ = normalize_aspect_label("口感太淡", FOOD)
        self.assertEqual(aspect, "口感与味道")

    def test_unknown_aspect_falls_back_with_flag(self) -> None:
        aspect, method, ambiguous = normalize_aspect_label("玄学方面", BEAUTY)
        self.assertEqual(aspect, "其他")
        self.assertEqual(method, "fallback_other")
        self.assertTrue(ambiguous)


class DirectionNormalizationTest(unittest.TestCase):
    def test_basic_aliases(self) -> None:
        self.assertEqual(normalize_direction_label("偏硬")[0], "too_hard")
        self.assertEqual(normalize_direction_label("太窄")[0], "too_narrow")
        self.assertEqual(normalize_direction_label("开胶")[0], "unglued")
        self.assertEqual(normalize_direction_label("ineffective")[0], "ineffective")

    def test_negated_direction_not_mapped(self) -> None:
        value, method, ambiguous = normalize_direction_label("不掉色")
        self.assertEqual(value, "other")
        self.assertEqual(method, "negated")
        self.assertTrue(ambiguous)

    def test_unknown_direction(self) -> None:
        value, method, ambiguous = normalize_direction_label("说不上来")
        self.assertEqual(value, "other")
        self.assertEqual(method, "other")
        self.assertTrue(ambiguous)


class DefectSignatureTest(unittest.TestCase):
    def test_same_defect_different_severity_is_compatible(self) -> None:
        self.assertEqual(primary_defect_signature("一点作用没有"), "efficacy_insufficient")
        self.assertEqual(primary_defect_signature("效果一般般"), "efficacy_insufficient")
        self.assertTrue(signatures_compatible("一点作用没有", "效果一般般"))

    def test_different_defects_are_not_compatible(self) -> None:
        pairs = [
            ("用了长痘了", "过敏了"),
            ("其中一瓶是空瓶", "里面才装一点点，用4天就没了"),
            ("服务态度差", "售后一直拖着不给处理"),
            ("顾问反复推荐加购", "一天几个电话骚扰用户"),
            ("不好吃了", "香味有点淡"),
        ]
        for left, right in pairs:
            self.assertFalse(signatures_compatible(left, right), (left, right))

    def test_unknown_signature_does_not_block(self) -> None:
        self.assertEqual(defect_signatures("说不上来的问题"), set())
        self.assertTrue(signatures_compatible("说不上来的问题", "用了长痘了"))

    def test_service_outcome_is_not_hidden_by_sales_context(self) -> None:
        # 推销/忽悠是销售背景，明确的失联和隐私后果才是此问题的主缺陷。
        self.assertEqual(
            primary_defect_signature("忽悠买完后马上消失"), "disappeared"
        )
        self.assertEqual(primary_defect_signature("加微信推销产品后就不理人"), "disappeared")
        self.assertEqual(primary_defect_signature("泄露电话号码后有人推销"), "privacy")

    def test_other_composite_defects_use_earliest_mention(self) -> None:
        self.assertEqual(primary_defect_signature("越用痘越多，皮肤也烂了"), "acne")

    def test_contact_channel_and_advertisement_are_not_defects(self) -> None:
        self.assertIsNone(primary_defect_signature("客服通过电话和短信联系我"))
        self.assertNotIn("ad_mismatch", defect_signatures("不停推送广告骚扰用户"))
        self.assertEqual(primary_defect_signature("频繁回访反复问同样问题"), "follow_up")

    def test_missing_general_expressions_have_signatures(self) -> None:
        self.assertEqual(primary_defect_signature("一点效果都没有"), "efficacy_insufficient")
        self.assertEqual(primary_defect_signature("不知道是什么牌子"), "brand_unknown")


class IssueValidationTest(unittest.TestCase):
    def test_validate_keeps_raw_and_methods(self) -> None:
        review = {"review_id": "R1", "text": "用了长痘了，脸也烂了", "star": 1}
        issue, errors = validate_issue(
            {
                "object": "面部",
                "aspect": "使用舒适性",
                "manifestation": "用了长痘，脸也烂了",
                "direction": "irritation",
                "severity": "high",
                "evidence": "用了长痘了，脸也烂了",
                "confidence": 0.9,
            },
            review,
            profile=BEAUTY,
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(issue)
        payload = issue.to_dict()
        self.assertEqual(payload["object_raw"], "面部")
        self.assertEqual(payload["object"], "皮肤")
        self.assertEqual(payload["object_normalized"], "皮肤")
        self.assertEqual(payload["object_method"], "canonical_alias")
        self.assertEqual(payload["aspect_raw"], "使用舒适性")
        self.assertEqual(payload["aspect"], "功效与效果")
        self.assertEqual(payload["aspect_method"], "context_rule")
        self.assertFalse(payload["normalization_ambiguous"])

    def test_normalize_issue_labels_does_not_mutate_input(self) -> None:
        original = {
            "object": "面部",
            "aspect": "使用舒适性",
            "direction": "irritation",
            "manifestation": "长痘",
            "evidence": "长痘",
        }
        before = dict(original)
        result = normalize_issue_labels(original, BEAUTY)
        self.assertEqual(original, before)
        self.assertEqual(result["object"], "皮肤")
        self.assertEqual(result["aspect"], "功效与效果")
        self.assertEqual(result["direction"], "irritation")


if __name__ == "__main__":
    unittest.main()
