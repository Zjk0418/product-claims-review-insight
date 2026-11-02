import unittest

from analysis.evaluate import evaluate_reviews, match_score


def issue(obj, aspect, evidence, direction="other"):
    return {
        "object": obj,
        "aspect": aspect,
        "manifestation": evidence,
        "evidence": evidence,
        "direction": direction,
        "severity": "medium",
    }


class EvaluateTest(unittest.TestCase):
    def test_match_exact(self):
        score = match_score(
            issue("鞋底", "穿着舒适性", "鞋底太硬", "too_hard"),
            issue("鞋底", "穿着舒适性", "鞋底太硬", "too_hard"),
        )
        self.assertGreater(score, 0)

    def test_opposite_direction_not_matched(self):
        score = match_score(
            issue("尺码", "尺码与鞋型", "尺码偏大", "too_large"),
            issue("尺码", "尺码与鞋型", "尺码偏小", "too_small"),
        )
        self.assertEqual(score, 0)

    def test_evidence_mismatch_not_matched(self):
        score = match_score(
            issue("鞋底", "穿着舒适性", "鞋底太硬"),
            issue("鞋底", "穿着舒适性", "鞋垫很薄"),
        )
        self.assertEqual(score, 0)

    def test_metrics(self):
        predictions = {"r1": [issue("鞋底", "穿着舒适性", "鞋底太硬", "too_hard")]}
        gold = {
            "r1": [
                issue("鞋底", "穿着舒适性", "鞋底太硬", "too_hard"),
                issue("后跟", "穿着舒适性", "后跟磨脚", "rubbing"),
            ]
        }
        result = evaluate_reviews(predictions, gold)
        self.assertEqual(result["tp"], 1)
        self.assertEqual(result["fp"], 0)
        self.assertEqual(result["fn"], 1)
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["recall"], 0.5)


if __name__ == "__main__":
    unittest.main()
