import unittest

from analysis.categories import get_profile
from analysis.claim_schema import Claim, dedupe_claims, filter_actionable_claims, filter_claim_dicts, validate_claim


RAW = {"T01": {"text_id": "T01", "text": "防滑耐磨减震透气", "source": "image_ocr", "confidence": 0.99}}


def claim(text, aspect, claim_type, norm, conf=0.9):
    return Claim(
        claim_id="x",
        text_id="T01",
        aspect=aspect,
        aspect_raw=aspect,
        claim_type=claim_type,
        normalized_claim=norm,
        original_text=text,
        source="image_ocr",
        confidence=conf,
    )


class ClaimSchemaTest(unittest.TestCase):
    def test_attribute_slogans_become_functional_claims(self):
        """“阔腿显瘦/亲肤弹力”这类短口号是可核验承诺，不能按噪声丢掉。"""
        raw = {"T01": {"text_id": "T01", "text": "阔腿显瘦 亲肤舒适 柔软弹力 抗皱有型", "source": "image_ocr"}}
        apparel = get_profile("apparel")
        fit, errors = validate_claim(
            {"text_id": "T01", "aspect": "其他", "claim_type": "marketing_slogan",
             "normalized_claim": "阔腿显瘦", "original_text": "阔腿显瘦"}, raw, profile=apparel)
        self.assertEqual(errors, [])
        self.assertEqual(fit.claim_type, "functional")
        self.assertEqual(fit.aspect, "版型与合身度")
        self.assertTrue(fit.to_dict()["verifiable"])

        elastic, errors = validate_claim(
            {"text_id": "T01", "aspect": "其他", "claim_type": "marketing_slogan",
             "normalized_claim": "面料柔软有弹力", "original_text": "柔软弹力"}, raw, profile=apparel)
        self.assertEqual(errors, [])
        self.assertEqual(elastic.claim_type, "functional")
        self.assertEqual(elastic.aspect, "材质与用料")

    def test_english_noise_stays_filtered(self):
        claims = [{"claim_type": "irrelevant_noise", "normalized_claim": "TREND", "original_text": "TREND"}]
        kept, reasons, _ = filter_claim_dicts(claims)
        self.assertEqual(kept, [])
        self.assertEqual(reasons.get("抽象口号或无关文字"), 1)

    def test_thickness_and_fit_params_map_to_comparable_aspects(self):
        raw = {"T01": {"text_id": "T01", "text": "夏季薄款 直筒 宽松", "source": "title"}}
        apparel = get_profile("apparel")
        thin, errors = validate_claim(
            {"text_id": "T01", "aspect": "其他", "claim_type": "objective_param",
             "normalized_claim": "商品为薄款", "original_text": "薄款"}, raw, profile=apparel)
        self.assertEqual(errors, [])
        self.assertEqual(thin.aspect, "材质与用料")
        fit, errors = validate_claim(
            {"text_id": "T01", "aspect": "其他", "claim_type": "objective_param",
             "normalized_claim": "版型为直筒", "original_text": "直筒"}, raw, profile=apparel)
        self.assertEqual(errors, [])
        self.assertEqual(fit.aspect, "版型与合身度")

    def test_valid_claim(self):
        item, errors = validate_claim(
            {
                "text_id": "T01",
                "aspect": "功能与性能",
                "claim_type": "functional",
                "normalized_claim": "商品具有防滑性能",
                "original_text": "防滑",
                "confidence": 0.95,
            },
            RAW,
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(item)
        self.assertTrue(item.claim_id.startswith("PC"))
        self.assertTrue(item.to_dict()["verifiable"])

    def test_original_text_must_exist(self):
        item, errors = validate_claim(
            {
                "text_id": "T01",
                "aspect": "功能与性能",
                "claim_type": "functional",
                "normalized_claim": "商品具有防水性能",
                "original_text": "防水",
            },
            RAW,
        )
        self.assertIsNone(item)
        self.assertIn("original_text 无法在输入文本中定位", errors)

    def test_unknown_text_id(self):
        item, errors = validate_claim(
            {
                "text_id": "T99",
                "aspect": "功能与性能",
                "claim_type": "functional",
                "normalized_claim": "x",
                "original_text": "x",
            },
            RAW,
        )
        self.assertIsNone(item)
        self.assertTrue(any("未知 text_id" in error for error in errors))

    def test_wear_resistance_maps_to_durability(self):
        item, _ = validate_claim(
            {
                "text_id": "T01",
                "aspect": "功能与性能",
                "claim_type": "functional",
                "normalized_claim": "商品具有耐磨性能",
                "original_text": "耐磨",
            },
            RAW,
        )
        self.assertEqual(item.aspect, "耐用性")

    def test_color_attribute_maps_to_appearance(self):
        raw = {"T02": {"text_id": "T02", "text": "颜色分类：米色、白色", "source": "attribute"}}
        item, _ = validate_claim(
            {
                "text_id": "T02",
                "aspect": "其他",
                "claim_type": "objective_param",
                "normalized_claim": "颜色包含米色、白色",
                "original_text": "颜色分类：米色、白色",
            },
            raw,
        )
        self.assertEqual(item.aspect, "外观与色差")

    def test_dedupe_by_containment(self):
        claims = [
            claim("柔软内里", "穿着舒适性", "comfort_experience", "鞋内里柔软"),
            claim("舒适柔软内里", "穿着舒适性", "comfort_experience", "鞋内里柔软舒适"),
            claim("防滑", "功能与性能", "functional", "商品具有防滑性能"),
        ]
        result = dedupe_claims(claims)
        self.assertEqual(len(result), 2)
        norms = {item.normalized_claim for item in result}
        self.assertIn("鞋内里柔软舒适", norms)
        self.assertIn("商品具有防滑性能", norms)


if __name__ == "__main__":
    unittest.main()


class ActionableFilterTest(unittest.TestCase):
    def test_filter_drops_meta_dates_platform_and_slogans(self):
        claims = [
            claim("官方立减14元", "价格与性价比", "price_value", "官方立减14元"),
            claim("活动时间：10月15日-11月13日", "其他", "objective_param", "活动时间：10月15日-11月13日"),
            claim("商品为沐浴露", "其他", "objective_param", "商品为沐浴露"),
            claim("文本为平台促销活动信息", "其他", "irrelevant_noise", "文本为平台促销活动信息"),
            claim("促销装宣传，无具体价格信息", "其他", "price_value", "促销装宣传，无具体价格信息"),
            claim("天猫超市双11", "其他", "price_value", "天猫超市双11"),
            claim("美丽自信", "其他", "marketing_slogan", "美丽自信"),
            claim("持久留香30小时", "功效与效果", "functional", "持久留香30小时"),
        ]
        kept, reasons, samples = filter_actionable_claims(claims)
        kept_texts = [item.normalized_claim for item in kept]
        self.assertIn("官方立减14元", kept_texts)
        self.assertIn("持久留香30小时", kept_texts)
        for dropped in ("商品为沐浴露", "活动时间：10月15日-11月13日", "天猫超市双11", "美丽自信", "文本为平台促销活动信息"):
            self.assertNotIn(dropped, kept_texts)
        self.assertEqual(sum(reasons.values()), 6)
        self.assertTrue(samples)
        self.assertTrue(all(item["reason"] for item in samples))

    def test_capacity_and_spec_claims_survive(self):
        claims = [
            claim("680G*2", "规格与容量", "size_spec", "680g×2 组合装"),
            claim("正品保障", "质量与做工", "material_craft", "官方正品保障"),
            claim("不伤手", "使用体验", "comfort_experience", "温和不伤手"),
        ]
        kept, reasons, _samples = filter_actionable_claims(claims)
        self.assertEqual(len(kept), 3)
        self.assertEqual(reasons, {})

    def test_design_words_are_not_meta_narration(self):
        """“商品为直筒版型”是版型承诺，不能当成描述文本本身丢掉。"""
        claims = [
            claim("直筒", "版型与合身度", "objective_param", "商品为直筒版型"),
            claim("宽松", "版型与合身度", "objective_param", "商品为宽松版型"),
            claim("夏季薄款", "其他", "objective_param", "商品为夏季薄款"),
            claim("牛仔裤", "其他", "objective_param", "商品为牛仔裤"),
            claim("休闲长裤", "其他", "objective_param", "商品为休闲长裤"),
        ]
        kept, reasons, _samples = filter_actionable_claims(claims)
        kept_texts = [item.normalized_claim for item in kept]
        self.assertIn("商品为直筒版型", kept_texts)
        self.assertIn("商品为宽松版型", kept_texts)
        self.assertIn("商品为夏季薄款", kept_texts)
        self.assertNotIn("商品为牛仔裤", kept_texts)
        self.assertNotIn("商品为休闲长裤", kept_texts)
        self.assertEqual(reasons.get("描述文本或图片本身"), 2)


class DictFilterTest(unittest.TestCase):
    def test_platform_price_explanations_are_not_product_price_claims(self):
        texts = [
            "划线价格并非原价，仅供参考",
            "划线价格非原价",
            "划线价¥199为厂商指导价，并非实际成交价",
            "未划线价格是商品的实时标价，具体成交价格以订单结算页价格为准",
            "商品到手价格以订单结算页展示的价格为准",
            "价格说明：划线价可能为厂商指导价，不作为实际成交价格依据",
        ]
        rows = [{"claim_type": "price_value", "normalized_claim": text, "original_text": text} for text in texts]
        kept, reasons, _ = filter_claim_dicts(rows)
        self.assertEqual(kept, [])
        self.assertEqual(reasons, {"平台价格说明或成交规则": len(texts)})

    def test_bare_size_table_labels_do_not_create_analysis_dimensions(self):
        texts = ["尺码表", "建议尺码", "尺码 / 身高 / 体重", "SIZE 身高(CM) 体重(KG)", "腰围：", "尺码对照表"]
        rows = [{"claim_type": "size_spec", "normalized_claim": text, "original_text": text} for text in texts]
        kept, reasons, _ = filter_claim_dicts(rows)
        self.assertEqual(kept, [])
        self.assertEqual(reasons, {"尺码表标题或裸标签": len(texts)})

    def test_mixed_price_rules_keep_actual_prices_specifications_and_benefits(self):
        rows = [
            {"claim_type": "price_value", "normalized_claim": "到手价89元，划线价格仅供参考", "original_text": "到手价89元，划线价格仅供参考"},
            {"claim_type": "price_value", "normalized_claim": "商品价格为89元，划线价格仅供参考", "original_text": "商品价格为89元，划线价格仅供参考"},
            {"claim_type": "size_spec", "normalized_claim": "净含量680g×2，价格说明以页面为准", "original_text": "净含量680g×2，价格说明以页面为准"},
            {"claim_type": "functional", "normalized_claim": "持久留香30小时，划线价并非原价", "original_text": "持久留香30小时，划线价并非原价"},
            {"claim_type": "functional", "normalized_claim": "温和不伤手，划线价仅供参考", "original_text": "温和不伤手，划线价仅供参考"},
            {"claim_type": "size_spec", "normalized_claim": "尺码表：适用身高160-170cm", "original_text": "尺码表：适用身高160-170cm"},
            {"claim_type": "size_spec", "normalized_claim": "建议尺码XL", "original_text": "建议尺码XL"},
            {"claim_type": "size_spec", "normalized_claim": "尺码为均码", "original_text": "尺码"},
            {"claim_type": "size_spec", "normalized_claim": "适用尺码39-44", "original_text": "尺码表"},
        ]
        kept, reasons, _ = filter_claim_dicts(rows)
        self.assertEqual(kept, rows)
        self.assertEqual(reasons, {})

    def test_object_and_historical_dict_filter_have_identical_new_rules(self):
        objects = [
            claim("划线价格并非原价", "价格与性价比", "price_value", "划线价格并非原价"),
            claim("尺码表", "尺寸与尺码", "size_spec", "尺码表"),
            claim("到手价89元", "价格与性价比", "price_value", "到手价89元"),
        ]
        kept_objects, object_reasons, _ = filter_actionable_claims(objects)
        kept_dicts, dict_reasons, _ = filter_claim_dicts([item.to_dict() for item in objects])
        self.assertEqual([item.normalized_claim for item in kept_objects], [item["normalized_claim"] for item in kept_dicts])
        self.assertEqual(object_reasons, dict_reasons)

    def test_dict_filter_mirrors_object_filter(self):
        rows = [
            {"claim_type": "functional", "normalized_claim": "持久留香30小时", "original_text": "持久留香30小时"},
            {"claim_type": "price_value", "normalized_claim": "活动时间：10月15日-11月13日", "original_text": "活动时间：10月15日-11月13日"},
            {"claim_type": "irrelevant_noise", "normalized_claim": "文本为英文单词LIGHT", "original_text": "LIGHT"},
        ]
        kept, reasons, samples = filter_claim_dicts(rows)
        self.assertEqual([item["normalized_claim"] for item in kept], ["持久留香30小时"])
        self.assertEqual(sum(reasons.values()), 2)
        self.assertEqual(len(samples), 2)
