"""人工介入判定与说明的单元测试。"""
from __future__ import annotations

import unittest

from intervention import build, classify_page


def page(href="https://detail.tmall.com/item.htm?id=123", **kwargs):
    base = {
        "href": href,
        "title": "测试商品-天猫",
        "text": "商品详情 评价",
        "ready_state": "complete",
        "has_comment_dom": True,
    }
    base.update(kwargs)
    return base


class ClassifyPageTest(unittest.TestCase):
    def test_normal_page(self):
        result = classify_page(page(), "123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "ok")

    def test_captcha_page(self):
        result = classify_page(
            page(href="https://detail.tmall.com/item.htm?id=123&_____tmd_____=x"), "123"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "platform_validate")

    def test_captcha_by_title(self):
        result = classify_page(page(title="验证码拦截"), "123")
        self.assertEqual(result["kind"], "platform_validate")

    def test_login_page(self):
        result = classify_page(
            page(href="https://login.taobao.com/member/login.jhtml"), "123"
        )
        self.assertEqual(result["kind"], "login_required")

    def test_wrong_product(self):
        result = classify_page(page(), "999")
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "wrong_product")
        self.assertEqual(result["diagnostics"]["found_product_id"], "123")

    def test_product_gone(self):
        result = classify_page(page(text="很抱歉，您查看的宝贝不存在"), "123")
        self.assertEqual(result["kind"], "product_unavailable")

    def test_page_not_loaded(self):
        result = classify_page(page(ready_state="loading"), "123")
        self.assertEqual(result["kind"], "page_not_loaded")

    def test_non_item_page(self):
        result = classify_page(page(href="https://www.tmall.com/"), "123")
        self.assertEqual(result["kind"], "page_unexpected")

    def test_empty_href(self):
        result = classify_page(page(href=""), "123")
        self.assertEqual(result["kind"], "browser_disconnected")


class GuideTest(unittest.TestCase):
    def test_guide_contains_product_id_and_steps(self):
        payload = build("platform_validate", "123456")
        self.assertEqual(payload["kind"], "platform_validate")
        self.assertTrue(payload["can_resume"])
        self.assertTrue(payload["steps"])
        self.assertTrue(any("123456" in step for step in payload["steps"]))
        self.assertIn("验证", payload["reason"])

    def test_unavailable_is_not_resumable(self):
        payload = build("product_unavailable", "123")
        self.assertFalse(payload["can_resume"])

    def test_unknown_kind_falls_back(self):
        payload = build("something_new", "123")
        self.assertEqual(payload["kind"], "something_new")
        self.assertTrue(payload["steps"])


if __name__ == "__main__":
    unittest.main()
