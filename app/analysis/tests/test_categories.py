"""品类识别与品类配置的单元测试。"""
import unittest

from analysis.categories import detect_category, get_profile
from analysis.issue_schema import normalize_aspect, object_key


class CategoryDetectionTest(unittest.TestCase):
    def test_detect_beauty_from_title(self):
        info = detect_category(
            {"title": "DRDM祛痘印烟酰胺精华修复凝胶淡化痘印", "attributes": {"颜色分类": ["一瓶装"]}}
        )
        self.assertEqual(info["key"], "beauty")
        self.assertEqual(info["label"], "美妆个护")
        self.assertIn("精华", "".join(info["evidence"]))

    def test_detect_bag(self):
        info = detect_category({"title": "旅行包男大容量双肩包行李包"})
        self.assertEqual(info["key"], "bag")

    def test_detect_shoe(self):
        info = detect_category({"title": "森马男鞋透气网面运动鞋"})
        self.assertEqual(info["key"], "shoe")

    def test_detect_unknown_returns_generic(self):
        info = detect_category({"title": "某某品牌官方旗舰店"})
        self.assertEqual(info["key"], "generic")
        self.assertEqual(info["confidence"], 0.0)

    def test_empty_product_is_generic(self):
        self.assertEqual(detect_category({})["key"], "generic")


class ProfileNormalizationTest(unittest.TestCase):
    def test_shoe_keeps_legacy_aspect(self):
        shoe = get_profile("shoe")
        self.assertEqual(normalize_aspect("尺码偏小", shoe), "尺码与鞋型")
        self.assertEqual(normalize_aspect("磨脚", shoe), "穿着舒适性")

    def test_beauty_maps_efficacy(self):
        beauty = get_profile("beauty")
        self.assertEqual(normalize_aspect("用了一个月没效果", beauty), "功效与效果")
        self.assertEqual(normalize_aspect("脸上泛红刺痛", beauty), "使用舒适性")

    def test_generic_has_no_shoe_labels(self):
        generic = get_profile("generic")
        self.assertNotIn("尺码与鞋型", generic.allowed_aspects)
        self.assertEqual(normalize_aspect("尺码偏小", generic), "尺寸与规格")

    def test_object_synonyms_are_profile_scoped(self):
        beauty = get_profile("beauty")
        shoe = get_profile("shoe")
        self.assertEqual(object_key("脸上的痘印", beauty), "痘印")
        self.assertEqual(object_key("鞋底开胶", shoe), "鞋底")
        # 通用配置不认识鞋类部件，但会原样保留对象名，不会强行改成鞋类
        self.assertEqual(object_key("鞋底开胶", get_profile("generic")), "鞋底开胶")

    def test_prompt_mentions_category(self):
        from analysis.issue_extractor import build_system_prompt

        prompt = build_system_prompt(get_profile("beauty"))
        self.assertIn("美妆个护", prompt)
        self.assertIn("功效与效果", prompt)
        self.assertNotIn("鞋头、后跟", prompt)

    def test_claim_prompt_mentions_category(self):
        from analysis.claim_extractor import build_system_prompt

        prompt = build_system_prompt(get_profile("bag"))
        self.assertIn("箱包", prompt)
        self.assertIn("容量", prompt)


if __name__ == "__main__":
    unittest.main()
