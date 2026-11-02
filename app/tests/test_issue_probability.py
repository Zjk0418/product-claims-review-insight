"""Conditional feedback prediction: coherent cohorts, no purchase-risk fiction."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issue_evidence
from test_issue_evidence import case, overall

OTHERS = (
    "快递盒被压扁了，里面衣服没受损，送到门口花了整整五天。",
    "尺码比尺码表小一号，肩膀位置很紧，换了大码后才勉强能穿。",
    "图片颜色偏差明显，实物是暗灰色，搭配我原来的裤子不合适。",
    "客服没有答复我的换货问题，等了半天才看到一句自动回复。",
    "到手发现少寄了一根腰带，联系商家补发，还需要等下周。",
    "袖子有长短差异，摊开量左右差了两厘米，做工比较随意。",
)


def dataset():
    value = case()
    value["extraction"] = {"reviews": [{
        "review_id": r["review_id"], "status": "ok", "errors": [],
        "content": r["text"], "star": r["star"], "date": r["date"], "sku_id": r["sku_id"],
        "issues": [i for i in value["issues"] if i["review_id"] == r["review_id"]],
    } for r in value["reviews"]]}
    return value


def add_other(value, index, *, star=2, status="empty", sku="sku-A", date="2026-10-28"):
    rid = f"other-{index}"
    text = OTHERS[index % len(OTHERS)]
    review = {"review_id": rid, "product_id": value["product_id"], "star": star,
        "date": date, "sku_id": sku, "sku": sku, "text": text, "content": text,
        "valid_for_analysis": True}
    record = {"review_id": rid, "status": status, "errors": [], "content": text,
        "star": star, "date": date, "sku_id": sku, "issues": []}
    value["reviews"].append(review)
    value["extraction"]["reviews"].append(record)
    return review, record


def probability(value, days=30):
    return overall(issue_evidence.build(**value), days)["probability_estimate"]


class IssueProbabilityTests(unittest.TestCase):
    def test_explicit_empty_snapshot_cannot_validate_positive_flat_issues(self):
        value = dataset()
        for record in value["extraction"]["reviews"]:
            record.update(status="empty", issues=[])
        result = probability(value)
        self.assertIsNone(result["estimate"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["coverage"]["excluded"]["snapshot_mismatch"], 4)

    def test_conflicting_issue_snapshot_is_excluded_instead_of_counting_as_zero(self):
        value = dataset()
        value["extraction"]["reviews"][0].update(status="empty", issues=[])
        result = probability(value)
        self.assertEqual((result["successes"], result["total"]), (3, 3))
        self.assertAlmostEqual(result["estimate"], .875)

    def test_snapshot_body_comparison_preserves_decimal_points(self):
        value = dataset()
        review, record = value["reviews"][0], value["extraction"]["reviews"][0]
        review["text"] += "容量1.5L。"
        review["content"] = review["text"]
        record["content"] += "容量15L。"
        result = probability(value)
        self.assertEqual((result["successes"], result["total"]), (3, 3))
        self.assertEqual(result["coverage"]["excluded"]["snapshot_mismatch"], 1)

    def test_status_only_records_allow_estimate_with_provenance_caveat(self):
        value = dataset()
        for record in value["extraction"]["reviews"]:
            record.pop("issues")
        result = probability(value)
        self.assertAlmostEqual(result["estimate"], .9)
        self.assertTrue(any("未提供具体问题清单" in text for text in result["assumptions"]))

    def test_four_of_four_has_real_probability_and_wide_interval(self):
        value = dataset()
        result = overall(issue_evidence.build(**value))
        p = result["probability_estimate"]
        self.assertAlmostEqual(p["estimate"], .9)
        self.assertAlmostEqual(p["interval"][0], .5552373823423603)
        self.assertGreater(p["interval"][1], .999)
        self.assertEqual((p["successes"], p["total"]), (4, 4))
        self.assertTrue(p["small_sample"])
        self.assertEqual(p["tone"], "high")
        self.assertEqual(result["probability"], p["estimate"])
        self.assertIsNone(result["purchase_probability"])
        self.assertFalse(p["calibrated"])
        self.assertEqual(p["target"], "next_comparable_low_star_review_mentions_problem")
        self.assertIn("可分析低星", p["scope_label"])

    def test_four_of_ten_uses_all_completed_comparable_low_star_feedback(self):
        value = dataset()
        for index in range(6):
            add_other(value, index)
        p = probability(value)
        self.assertEqual((p["successes"], p["total"]), (4, 10))
        self.assertAlmostEqual(p["estimate"], 4.5 / 11)
        self.assertAlmostEqual(p["interval"][0], .15306710112617594)
        self.assertAlmostEqual(p["interval"][1], .6963205143995119)
        self.assertEqual(p["tone"], "medium")

    def test_window_changes_both_successes_and_matching_denominator(self):
        value = dataset()
        seven, thirty = probability(value, 7), probability(value, 30)
        self.assertEqual((seven["successes"], seven["total"]), (2, 2))
        self.assertAlmostEqual(seven["estimate"], 2.5 / 3)
        self.assertEqual((thirty["successes"], thirty["total"]), (4, 4))
        self.assertEqual(probability(value, 90)["estimate"], thirty["estimate"])

    def test_display_threshold_is_probability_policy_and_preserves_small_sample_flag(self):
        value = dataset()
        self.assertEqual(probability(value)["tone"], "high")  # n=4 keeps its probability hue
        add_other(value, 0)
        p = probability(value)
        self.assertEqual(p["total"], 5)
        self.assertFalse(p["small_sample"])
        self.assertEqual(p["tone"], "high")  # 4.5/6, n>=5

    def test_missing_star_without_platform_classification_does_not_join_cohort(self):
        value = dataset()
        review, record = add_other(value, 0, star=None)
        p = probability(value)
        self.assertEqual((p["successes"], p["total"]), (4, 4))
        self.assertEqual(p["coverage"]["excluded"]["not_confirmed_low_star"], 1)

    def test_generic_high_stars_never_dilute_the_conditional_probability(self):
        value = dataset()
        before = probability(value)
        for index in range(1000):
            add_other(value, index, star=5)
        after = probability(value)
        for key in ("estimate", "interval", "successes", "total", "tone", "sku_key"):
            self.assertEqual(before[key], after[key])

    def test_failed_partial_unprocessed_and_error_records_do_not_become_zeros(self):
        for status in ("validation_failed", "partial_validation", "running", "failed", ""):
            with self.subTest(status=status):
                value = dataset()
                add_other(value, 0, status=status)
                self.assertEqual(probability(value)["total"], 4)
        value = dataset()
        _, record = add_other(value, 0)
        record["errors"] = ["incomplete result"]
        self.assertEqual(probability(value)["total"], 4)
        record["errors"] = []
        value["extraction"]["reviews"].remove(record)
        self.assertEqual(probability(value)["total"], 4)

    def test_missing_all_extraction_metadata_does_not_show_prior_fifty_percent(self):
        for extraction in (None, {}, {"reviews": []}):
            with self.subTest(extraction=extraction):
                value = dataset()
                value["extraction"] = extraction
                p = probability(value)
                self.assertIsNone(p["estimate"])
                self.assertIsNone(p["interval"])
                self.assertEqual(p["total"], 0)

    def test_cached_and_empty_records_are_completed_when_no_errors(self):
        value = dataset()
        for row in value["extraction"]["reviews"]:
            row["status"] = "cached"
        add_other(value, 0)
        self.assertEqual((probability(value)["successes"], probability(value)["total"]), (4, 5))

    def test_same_source_collapses_numerator_and_denominator_together(self):
        value = dataset()
        for row in value["reviews"]:
            row["buyer_id"] = "single-source"
        p = probability(value)
        self.assertEqual((p["successes"], p["total"]), (1, 1))
        self.assertEqual(p["identified_source_count"], 1)
        self.assertAlmostEqual(p["estimate"], .75)

    def test_long_copy_and_same_source_keep_positive_member_by_or(self):
        for same_source in (False, True):
            with self.subTest(same_source=same_source):
                value = dataset()
                # First review has no extracted pilling fact; the later member
                # does. Merging must preserve that positive event.
                value["issues"] = [value["issues"][3]]
                value["clusters"][0]["review_ids"] = ["r4"]
                template = "衣服洗了一次就起球，袖口出现一大片毛球，按照洗标冷水手洗并自然晾干，没有使用烘干机。"
                for index, (review, record) in enumerate(zip(value["reviews"], value["extraction"]["reviews"])):
                    text = template if not same_source else review["text"]
                    review.update(content=text, text=text)
                    record["content"] = text
                    record["issues"] = []
                    if same_source:
                        review["reviewer_id"] = "same-reviewer"
                value["issues"][0]["evidence"] = value["reviews"][3]["text"]
                value["extraction"]["reviews"][3]["issues"] = [value["issues"][0]]
                p = probability(value)
                self.assertEqual((p["successes"], p["total"]), (1, 1))
                self.assertEqual(p["positive_review_ids"], ["r4"])

    def test_shared_short_quotes_do_not_merge_different_full_review_bodies(self):
        value = dataset()
        for issue in value["issues"]:
            issue["evidence"] = "起球"
        p = probability(value)
        self.assertEqual((p["successes"], p["total"]), (4, 4))
        self.assertIsNone(p["identified_source_count"])

    def test_cross_sku_never_pools_and_primary_scope_is_not_largest_percentage(self):
        value = dataset()
        value["reviews"][3].update(sku_id="sku-B", sku="白色L")
        value["extraction"]["reviews"][3]["sku_id"] = "sku-B"
        for index in range(6):
            add_other(value, index)
        p = probability(value)
        self.assertEqual(p["sku_key"], "id:sku-A")  # three supported A vs one B
        self.assertEqual((p["successes"], p["total"]), (3, 9))
        b = next(row for row in p["per_sku"] if row["sku_key"] == "id:sku-B")
        self.assertEqual((b["successes"], b["total"]), (1, 1))
        self.assertLess(p["estimate"], b["estimate"])

    def test_failed_most_supported_sku_does_not_switch_to_a_tiny_high_rate_sku(self):
        value = dataset()
        value["reviews"][3].update(sku_id="sku-B", sku="白色L")
        value["extraction"]["reviews"][3]["sku_id"] = "sku-B"
        for row in value["extraction"]["reviews"][:3]:
            row["status"] = "validation_failed"
        p = probability(value)
        self.assertEqual(p["sku_key"], "id:sku-A")
        self.assertIsNone(p["estimate"])

    def test_bad_dates_wrong_products_and_unknown_sku_do_not_dilute(self):
        for change in ({"date": "2026-08-01"}, {"date": "2026-10-30"}, {"date": ""},
                       {"product_id": "wrong-product"}, {"sku_id": "", "sku": ""}):
            with self.subTest(change=change):
                value = dataset()
                review, record = add_other(value, 0)
                review.update(change)
                record.update({key: val for key, val in change.items() if key in {"date", "sku_id"}})
                self.assertEqual(probability(value)["total"], 4)

    def test_all_snapshot_echo_mismatches_are_conservatively_excluded(self):
        for key, different in (("content", "已经更换了另一条完全不同的评论。"), ("date", "2026-10-27"),
                               ("sku_id", "wrong-sku"), ("star", 1)):
            with self.subTest(key=key):
                value = dataset()
                _, record = add_other(value, 0)
                record[key] = different
                p = probability(value)
                self.assertEqual(p["total"], 4)
                self.assertEqual(p["coverage"]["excluded"]["snapshot_mismatch"], 1)

    def test_platform_low_star_label_can_confirm_missing_star_but_high_star_wins(self):
        value = dataset()
        review, record = add_other(value, 0, star=None)
        review["rate_type"] = "-1"
        self.assertEqual(probability(value)["total"], 5)
        review["star"] = record["star"] = 5
        self.assertEqual(probability(value)["total"], 4)

    def test_unknown_or_inferred_problem_definition_is_not_estimated_as_zero_risk(self):
        for modification in ({"basis": "inferred"}, {"evidence": "这不是实际原文"}):
            with self.subTest(modification=modification):
                value = dataset()
                for issue in value["issues"]:
                    issue.update(modification)
                p = probability(value)
                self.assertIsNone(p["estimate"])
                self.assertIn("具体证据", p["reason"])

    def test_new_unrelated_append_never_refreshes_initial_problem(self):
        value = dataset()
        for review, record in zip(value["reviews"], value["extraction"]["reviews"]):
            review.update(date="2026-06-01", append_content="客服已经回复，处理态度可以。", append_date="2026-10-28")
            record["date"] = "2026-06-01"
        p = probability(value)
        self.assertEqual(p["successes"], 0)
        self.assertEqual(p["total"], 0)
        self.assertIsNone(p["estimate"])

    def test_genuine_append_problem_requires_snapshot_coverage_and_counts_once(self):
        value = dataset()
        review, record, issue = value["reviews"][0], value["extraction"]["reviews"][0], value["issues"][0]
        review.update(date="2026-06-01", content="刚收到，尚未使用。", text="刚收到，尚未使用。",
            append_content="后来发现袖口起球，表面有一大片毛球。", append_date="2026-10-28")
        record.update(date=review["date"], content=review["text"], issues=[])
        issue["evidence"] = review["append_content"]
        without = probability(value)
        self.assertEqual((without["successes"], without["total"]), (3, 3))
        record["issues"] = [copy.deepcopy(issue)]
        covered = probability(value)
        self.assertEqual((covered["successes"], covered["total"]), (4, 4))

    def test_safety_override_remains_even_when_conditional_percentage_is_low(self):
        value = dataset()
        value["issues"] = [value["issues"][0]]
        review, record, issue = value["reviews"][0], value["extraction"]["reviews"][0], value["issues"][0]
        text = "使用这件衣服后接触位置过敏，皮肤出现大片红疹。"
        review.update(text=text, content=text)
        record["content"] = text
        issue.update(evidence=text, object="皮肤", aspect="使用舒适性", manifestation="接触后过敏")
        for index in range(6):
            add_other(value, index)
        result = overall(issue_evidence.build(**value))
        self.assertEqual(result["attention_level"], "safety")
        self.assertLess(result["probability"], .3)
        self.assertEqual(result["probability_estimate"]["tone"], "low")

    def test_no_source_identifiers_leak_and_no_input_is_mutated(self):
        value = dataset()
        for index, review in enumerate(value["reviews"]):
            review["buyer_id"] = f"private-account-{index}"
        before = copy.deepcopy(value)
        result = issue_evidence.build(**value)
        self.assertEqual(value, before)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private-account-", encoded)
        self.assertNotIn("source:", encoded)

    def test_probabilities_are_reproducible_across_issue_and_review_order(self):
        value = dataset()
        for index in range(6):
            add_other(value, index)
        before = probability(value)
        value["reviews"].reverse()
        value["issues"].reverse()
        value["extraction"]["reviews"].reverse()
        self.assertEqual(before, probability(value))


if __name__ == "__main__":
    unittest.main()
