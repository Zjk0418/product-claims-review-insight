import unittest

from analysis.preprocessing import (
    clean_text,
    is_low_information,
    parse_date,
    parse_sku,
    parse_star,
)


class PreprocessingTest(unittest.TestCase):
    def test_clean_text_removes_control_chars_only(self):
        raw = "鞋底\x01开胶了\u200b，\r\n\r\n\r\n很严重  "
        cleaned = clean_text(raw)
        self.assertNotIn("\x01", cleaned)
        self.assertNotIn("\u200b", cleaned)
        self.assertIn("鞋底开胶了", cleaned)
        self.assertIn("很严重", cleaned)

    def test_low_information(self):
        self.assertTrue(is_low_information(""))
        self.assertTrue(is_low_information("该用户未填写评价内容"))
        self.assertTrue(is_low_information("默认好评"))
        self.assertTrue(is_low_information("好"))
        self.assertFalse(is_low_information("鞋底开胶了"))
        self.assertFalse(is_low_information("非常垃圾"))

    def test_parse_sku(self):
        color, size = parse_sku("米色.【旗舰款】；41[【标准运动码】现货秒发]")
        self.assertEqual(size, "41")
        self.assertIn("米色", color)
        self.assertEqual(parse_sku("黑色；39码")[1], "39")
        self.assertEqual(parse_sku("")[1], "")

    def test_parse_date_and_star(self):
        self.assertEqual(parse_date("2026-09-29"), "2026-09-29")
        self.assertEqual(parse_date("2026年9月29日"), "2026-09-29")
        self.assertEqual(parse_date("bad"), "")
        self.assertEqual(parse_star("3"), 3)
        self.assertIsNone(parse_star("9"))
        self.assertIsNone(parse_star(""))


if __name__ == "__main__":
    unittest.main()
