"""同一具体问题的近期重复反馈：离线规则回归，不推断购买发生概率。"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issue_evidence


PRODUCT_ID = "evidence-fixture-product"
AS_OF = "2026-10-29T15:00:00+08:00"
ASPECT = "耐用性"
TEXTS = (
    "衣服才穿两天袖口就起球了，手臂摩擦的位置密密麻麻，清理起来很麻烦。",
    "按照洗标手洗晾干后，下摆出现明显起球，表面的毛球用手也很难弄掉。",
    "出门坐车回来发现背包接触的衣服后背起球，之前另一件衣服没有这种情况。",
    "领口起球很严重，贴着脖子有粗糙感，用了一次去球器还是影响穿着。",
)
DATES = ("2026-10-20", "2026-10-21", "2026-10-26", "2026-10-28")


def case(count=4):
    """相同商品、规格、具体问题；措辞和事实细节不同，避免把模板当独立证据。"""
    reviews = []
    issues = []
    for index in range(count):
        rid = f"r{index + 1}"
        reviews.append({
            "review_id": rid, "product_id": PRODUCT_ID, "star": 2,
            "date": DATES[index], "sku_id": "sku-A", "sku": "黑色；M码",
            "content": TEXTS[index], "text": TEXTS[index], "valid_for_analysis": True,
        })
        issues.append({
            "issue_id": f"i{index + 1}", "review_id": rid, "object": "衣服",
            "aspect": ASPECT, "direction": "pilling", "manifestation": "衣服起球",
            "evidence": TEXTS[index], "basis": "explicit",
        })
    clusters = [{
        "cluster_id": "C01", "object": "衣服", "aspect": ASPECT,
        "problem_name": "衣服起球", "description": "使用或清洗后出现起球",
        "review_ids": [r["review_id"] for r in reviews],
        "member_issue_ids": [i["issue_id"] for i in issues],
        "review_evidence": {i["review_id"]: i["evidence"] for i in issues},
    }]
    return {
        "reviews": reviews, "clusters": clusters, "issues": issues,
        "product_id": PRODUCT_ID, "claim_aspects": (),
        "collection": {"finished_at": AS_OF, "scanned_count": 30},
        "summary": {"created_at": "2026-10-28T15:00:00+08:00"},
    }


def window(result, days=30):
    return result["windows"][str(days)]


def overall(result, days=30):
    return window(result, days)["overall"]


def signal_fields(signal):
    """分母等描述信息可变，主提醒的支持强度不能跟着泛好评变。"""
    return {key: signal[key] for key in (
        "attention_level", "tone", "repeat_count", "max_same_sku_count",
        "identified_source_count", "rule_progress",
    )}


class IssueEvidenceTests(unittest.TestCase):
    def test_four_recent_specific_same_sku_reviews_create_repeat_with_unknown_identity(self):
        result = issue_evidence.build(**case())
        problem = window(result)["problems"][0]
        expected = {
            "attention_level", "tone", "repeat_count", "max_same_sku_count",
            "recent_review_count", "review_count", "specific_count",
            "identified_source_count", "rule_progress", "review_ids",
            "cluster_ids", "name", "aspect",
        }
        self.assertLessEqual(expected, problem.keys())
        self.assertEqual(problem["attention_level"], "repeat")
        self.assertEqual(problem["tone"], "medium")
        self.assertEqual(problem["repeat_count"], 4)
        self.assertEqual(problem["max_same_sku_count"], 4)
        self.assertEqual(problem["review_count"], 4)
        self.assertEqual(problem["recent_review_count"], 4)
        self.assertEqual(problem["specific_count"], 4)
        self.assertIsNone(problem["identified_source_count"])
        self.assertEqual(set(problem["review_ids"]), {"r1", "r2", "r3", "r4"})
        self.assertEqual(problem["cluster_ids"], ["C01"])
        self.assertEqual(overall(result)["attention_level"], "repeat")

    def test_two_and_three_reviews_are_repeated_one_is_single(self):
        for count, expected in ((1, "single"), (2, "repeated"), (3, "repeated")):
            with self.subTest(count=count):
                signal = overall(issue_evidence.build(**case(count)))
                self.assertEqual(signal["attention_level"], expected)
                self.assertEqual(signal["repeat_count"], count)

    def test_four_stable_distinct_sources_allow_high_tone(self):
        for identity_key in ("reviewer_id", "buyer_id", "user_id"):
            with self.subTest(identity_key=identity_key):
                data = case()
                for index, review in enumerate(data["reviews"]):
                    review[identity_key] = f"verified-source-{index}"
                signal = overall(issue_evidence.build(**data))
                self.assertEqual(signal["attention_level"], "repeat")
                self.assertEqual(signal["tone"], "high")
                self.assertEqual(signal["identified_source_count"], 4)

    def test_masked_nicknames_never_verify_distinct_sources(self):
        for names in (("张**",) * 4, ("张**", "李**", "王**", "赵**")):
            with self.subTest(names=names):
                data = case()
                for review, nick in zip(data["reviews"], names):
                    review["nick"] = nick
                signal = overall(issue_evidence.build(**data))
                self.assertEqual(signal["repeat_count"], 4)
                self.assertIsNone(signal["identified_source_count"])
                self.assertEqual(signal["tone"], "medium")

    def test_same_stable_buyer_multiple_review_ids_counts_once(self):
        data = case()
        for review in data["reviews"]:
            review["buyer_id"] = "one-buyer"
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["repeat_count"], 1)
        self.assertEqual(signal["max_same_sku_count"], 1)
        self.assertEqual(signal["identified_source_count"], 1)
        self.assertEqual(signal["attention_level"], "single")

    def test_scan_count_and_one_thousand_generic_positive_reviews_cannot_dilute_warning(self):
        data = case()
        before = overall(issue_evidence.build(**data))
        data["collection"]["scanned_count"] += 1000
        data["reviews"].extend({
            "review_id": f"positive-{index}", "product_id": PRODUCT_ID,
            "star": 5, "date": "2026-10-28", "sku_id": "sku-A",
            "content": "很好，满意。", "text": "很好，满意。", "valid_for_analysis": False,
        } for index in range(1000))
        after = overall(issue_evidence.build(**data))
        self.assertEqual(signal_fields(before), signal_fields(after))

    def test_exact_and_near_duplicate_long_templates_do_not_supply_four_units(self):
        template = (
            "衣服洗了一次就起球，袖口和衣摆都是毛球。"
            "我按照洗标选择了冷水手洗并且自然晾干，没有使用烘干机，"
            "平常也没有和尖锐的物品放在一起，收到时包装完整，"
            "但是清理完表面的毛球后很快又重新出现，无法正常穿出门，"
            "这种实际体验与我的预期相差很大，写下这些细节供其他购买者参考。"
        )
        for near_copy in (False, True):
            with self.subTest(near_copy=near_copy):
                data = case()
                for index, (review, issue) in enumerate(zip(data["reviews"], data["issues"])):
                    value = template + (f"记录编号{index}。" if near_copy else "")
                    review.update(content=value, text=value, buyer_id=f"buyer-{index}")
                    issue["evidence"] = value
                    data["clusters"][0]["review_evidence"][review["review_id"]] = value
                signal = overall(issue_evidence.build(**data))
                self.assertEqual(signal["repeat_count"], 1)
                self.assertEqual(signal["attention_level"], "single")

    def test_same_review_and_duplicate_cluster_do_not_multiply_evidence(self):
        data = case()
        data["reviews"].append(copy.deepcopy(data["reviews"][0]))
        data["issues"].append(copy.deepcopy(data["issues"][0]))
        duplicate = copy.deepcopy(data["clusters"][0])
        duplicate["cluster_id"] = "C02"
        data["clusters"].append(duplicate)
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["repeat_count"], 4)
        self.assertEqual(signal["max_same_sku_count"], 4)

    def test_different_specific_problems_in_one_aspect_cannot_make_four(self):
        data = case()
        labels = (("起球", "pilling"), ("掉色", "color_fading"),
                  ("开线", "seam_split"), ("变形", "deformed"))
        data["clusters"] = []
        for index, (review, issue, (label, direction)) in enumerate(zip(data["reviews"], data["issues"], labels)):
            value = f"衣服穿了{index + 1}天就{label}，发生在袖口位置，已经影响正常穿着。"
            review.update(content=value, text=value)
            issue.update(manifestation=f"衣服{label}", direction=direction, evidence=value)
            data["clusters"].append({
                "cluster_id": f"C{index + 1:02}", "object": "衣服", "aspect": ASPECT,
                "problem_name": f"衣服{label}", "description": f"使用后{label}",
                "review_ids": [review["review_id"]], "member_issue_ids": [issue["issue_id"]],
                "review_evidence": {review["review_id"]: value},
            })
        result = issue_evidence.build(**data)
        self.assertEqual(overall(result)["repeat_count"], 1)
        self.assertEqual(overall(result)["attention_level"], "single")
        self.assertEqual(len(window(result)["problems"]), 4)

    def test_two_per_sku_is_cross_sku_not_four_for_one_sku(self):
        data = case()
        for review in data["reviews"][2:]:
            review.update(sku_id="sku-B", sku="白色；L码")
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["repeat_count"], 4)
        self.assertEqual(signal["max_same_sku_count"], 2)
        self.assertEqual(signal["attention_level"], "cross_sku")

    def test_mixed_defects_in_one_legacy_cluster_do_not_become_one_repeated_problem(self):
        data = case()
        data["clusters"][0].update(problem_name="衣服质量问题", description="出现多种质量问题")
        for index in (2, 3):
            value = ("手洗一次后衣服的侧缝开线，线头从接缝处整段散开。" if index == 2 else
                     "外出回来发现肩部接缝开线，针脚松散，抬手时破口明显。")
            review, issue = data["reviews"][index], data["issues"][index]
            review.update(content=value, text=value)
            issue.update(manifestation="衣服开线", direction="seam_split", evidence=value)
            data["clusters"][0]["review_evidence"][review["review_id"]] = value
        result = issue_evidence.build(**data)
        self.assertEqual(overall(result)["repeat_count"], 2)
        self.assertEqual(overall(result)["attention_level"], "repeated")
        self.assertEqual(sorted(p["repeat_count"] for p in window(result)["problems"]), [2, 2])

    def test_unknown_sku_cannot_be_assumed_same_as_known_sku(self):
        data = case()
        for review in data["reviews"][2:]:
            review.update(sku_id="", sku="")
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["repeat_count"], 4)
        self.assertEqual(signal["max_same_sku_count"], 2)
        self.assertNotEqual(signal["attention_level"], "repeat")

    def test_expired_missing_future_dates_do_not_complete_recent_four(self):
        for rejected_date in ("2026-08-01", "", "not-a-date", "2026-10-30"):
            with self.subTest(date=rejected_date):
                data = case()
                data["reviews"][3]["date"] = rejected_date
                signal = overall(issue_evidence.build(**data))
                self.assertEqual(signal["repeat_count"], 3)
                self.assertEqual(signal["max_same_sku_count"], 3)
                self.assertEqual(signal["attention_level"], "repeated")

    def test_seven_thirty_and_ninety_day_windows_are_separate(self):
        result = issue_evidence.build(**case())
        self.assertEqual(set(result["windows"]), {"7", "30", "90"})
        self.assertEqual(overall(result, 7)["repeat_count"], 2)
        self.assertEqual(overall(result, 7)["attention_level"], "repeated")
        self.assertEqual(overall(result, 30)["repeat_count"], 4)
        self.assertEqual(overall(result, 90)["repeat_count"], 4)

    def test_wrong_product_and_invalid_review_cannot_complete_four(self):
        for bad_field, bad_value in (("product_id", "another-product"), ("valid_for_analysis", False)):
            with self.subTest(field=bad_field):
                data = case()
                data["reviews"][3][bad_field] = bad_value
                signal = overall(issue_evidence.build(**data))
                self.assertEqual(signal["repeat_count"], 3)
                self.assertEqual(signal["attention_level"], "repeated")

    def test_vague_quality_claims_do_not_become_concrete_from_cluster_name(self):
        data = case()
        for review, issue in zip(data["reviews"], data["issues"]):
            value = "质量太差了，很不满意，不推荐购买。"
            review.update(content=value, text=value)
            issue.update(manifestation="衣服质量差", direction="other", evidence=value)
            data["clusters"][0]["review_evidence"][review["review_id"]] = value
        # 故意保留过度具体的簇标签：具体性必须由真实引文确认。
        result = issue_evidence.build(**data)
        self.assertEqual(overall(result)["repeat_count"], 0)
        for problem in window(result)["problems"]:
            self.assertEqual(problem["specific_count"], 0)
            self.assertNotEqual(problem["attention_level"], "repeat")

    def test_issue_quote_not_in_original_cannot_complete_four(self):
        data = case()
        data["reviews"][3].update(content="物流很快，包装完整。", text="物流很快，包装完整。")
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["repeat_count"], 3)
        self.assertEqual(signal["attention_level"], "repeated")

    def test_legacy_cluster_quotes_are_supported_without_issues(self):
        data = case()
        data["issues"] = None
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["repeat_count"], 4)
        self.assertEqual(signal["attention_level"], "repeat")

    def test_missing_old_snapshot_quotes_do_not_invent_specific_evidence(self):
        data = case()
        data["issues"] = None
        data["clusters"][0]["review_evidence"] = {}
        signal = overall(issue_evidence.build(**data))
        self.assertNotEqual(signal["attention_level"], "repeat")
        self.assertEqual(signal["repeat_count"], 0)

    def test_new_unrelated_append_does_not_refresh_old_initial_problem(self):
        data = case(1)
        data["reviews"][0].update(
            date="2026-06-01", append_content="客服已经回复，处理态度可以。",
            append_date="2026-10-28", appended=True,
        )
        self.assertEqual(overall(issue_evidence.build(**data))["repeat_count"], 0)

    def test_problem_first_reported_in_append_uses_append_date_once(self):
        data = case(1)
        review, issue = data["reviews"][0], data["issues"][0]
        append = "后来再穿才发现衣服后背起球，摩擦位置已经有一大片毛球。"
        review.update(date="2026-06-01", content="刚收到，先试穿一下。", text="刚收到，先试穿一下。",
                      append_content=append, append_date="2026-10-28", appended=True)
        issue["evidence"] = append
        data["clusters"][0]["review_evidence"][review["review_id"]] = append
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["repeat_count"], 1)
        self.assertEqual(signal["attention_level"], "single")

    def test_same_review_multiple_initial_and_append_issues_still_counts_once(self):
        data = case(1)
        review = data["reviews"][0]
        review.update(append_content="现在袖口起球更多了，不能正常穿出门。", append_date="2026-10-28", appended=True)
        issue = copy.deepcopy(data["issues"][0])
        issue.update(issue_id="i-append", evidence=review["append_content"])
        data["issues"].append(issue)
        data["clusters"][0]["member_issue_ids"].append(issue["issue_id"])
        self.assertEqual(overall(issue_evidence.build(**data))["repeat_count"], 1)

    def test_unadvertised_filter_does_not_change_overall_problem_warning(self):
        data = case()
        before = issue_evidence.build(**data)
        self.assertEqual(window(before)["unadvertised"]["repeat_count"], 4)
        data["claim_aspects"] = (ASPECT,)
        after = issue_evidence.build(**data)
        self.assertEqual(signal_fields(overall(before)), signal_fields(overall(after)))
        self.assertEqual(window(after)["unadvertised"]["repeat_count"], 0)

    def test_safety_belongs_to_this_problem_quote_not_other_sentences(self):
        data = case(1)
        review, issue = data["reviews"][0], data["issues"][0]
        old_quote = issue["evidence"]
        review.update(content=old_quote + "另外用了护肤品过敏了。", text=old_quote + "另外用了护肤品过敏了。")
        ordinary = overall(issue_evidence.build(**data))
        self.assertNotEqual(ordinary["attention_level"], "safety")
        allergy = "使用这件衣服后接触位置过敏，皮肤出现大片红疹。"
        review.update(content=allergy, text=allergy)
        issue.update(object="皮肤", aspect="使用舒适性", manifestation="接触后过敏", direction="irritation", evidence=allergy)
        data["clusters"][0].update(object="皮肤", aspect="使用舒适性", problem_name="接触后过敏",
                                   description="使用后过敏", review_evidence={"r1": allergy})
        self.assertEqual(overall(issue_evidence.build(**data))["attention_level"], "safety")

    def test_collection_then_summary_then_max_valid_date_determine_reproducible_snapshot(self):
        data = case()
        data["summary"]["created_at"] = "2026-12-31T12:00:00+08:00"
        original = copy.deepcopy(data)
        first = issue_evidence.build(**data)
        self.assertEqual(first, issue_evidence.build(**copy.deepcopy(data)))
        self.assertEqual(data, original)
        self.assertEqual(overall(first)["repeat_count"], 4)  # finished_at takes priority.
        data["collection"] = {}
        self.assertEqual(overall(issue_evidence.build(**data))["repeat_count"], 0)
        data["summary"] = {}
        self.assertEqual(overall(issue_evidence.build(**data))["repeat_count"], 4)

    def test_negated_defect_does_not_name_or_escalate_the_actual_problem(self):
        samples = (
            ("不起球，但是洗了一次就掉色，白色内搭也染上颜色了。", "起球", "掉色"),
            ("没有过敏，只是味道有异味，晾晒后仍有明显气味。", "过敏", "异味"),
        )
        for quote, absent_defect, actual_defect in samples:
            with self.subTest(quote=quote):
                data = case(1)
                data["reviews"][0].update(content=quote, text=quote)
                data["issues"][0].update(evidence=quote, manifestation="衣服存在问题", direction="other")
                data["clusters"][0]["review_evidence"] = {"r1": quote}
                signal = overall(issue_evidence.build(**data))
                self.assertEqual(signal["repeat_count"], 1)
                self.assertNotIn(absent_defect, signal["name"])
                self.assertIn(actual_defect, signal["name"])
                self.assertNotEqual(signal["attention_level"], "safety")

    def test_old_initial_and_recent_same_problem_append_are_order_independent(self):
        data = case(1)
        review, original = data["reviews"][0], data["issues"][0]
        review.update(date="2026-06-01", append_content="穿了一阵子衣服后背又起球，摩擦的地方满是毛球。",
                      append_date="2026-10-28", appended=True)
        appended_issue = copy.deepcopy(original)
        appended_issue.update(issue_id="i-append", evidence=review["append_content"])
        data["issues"].append(appended_issue)
        first = issue_evidence.build(**data)
        data["issues"].reverse()
        reverse = issue_evidence.build(**data)
        for days in (7, 30, 90):
            with self.subTest(window=days):
                signal = overall(first, days)
                self.assertEqual(signal["repeat_count"], 1)
                self.assertEqual(signal["attention_level"], "single")
                self.assertEqual(signal_fields(signal), signal_fields(overall(reverse, days)))

    def test_sku_text_fallback_preserves_decimal_and_dimension_separators(self):
        for first_sku, second_sku in (("容量1.5L", "容量15L"), ("尺寸10×20厘米", "尺寸1020厘米")):
            with self.subTest(skus=(first_sku, second_sku)):
                data = case()
                for index, review in enumerate(data["reviews"]):
                    review.update(sku_id="", sku=first_sku if index < 2 else second_sku)
                signal = overall(issue_evidence.build(**data))
                self.assertEqual(signal["repeat_count"], 4)
                self.assertEqual(signal["max_same_sku_count"], 2)
                self.assertEqual(signal["attention_level"], "cross_sku")

    def test_sku_text_fallback_allows_unicode_and_whitespace_normalization(self):
        data = case()
        for index, review in enumerate(data["reviews"]):
            review.update(sku_id="", sku="容量１．５Ｌ" if index < 2 else "容量 1.5L")
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["max_same_sku_count"], 4)
        self.assertEqual(signal["attention_level"], "repeat")

    def test_wrong_product_or_ineligible_future_record_cannot_move_fallback_snapshot(self):
        for rejected_field, rejected_value in (("product_id", "different-product"), ("valid_for_analysis", False)):
            with self.subTest(field=rejected_field):
                data = case()
                data.update(collection={}, summary={})
                data["reviews"][3].update(date="2030-10-28", **{rejected_field: rejected_value})
                result = issue_evidence.build(**data)
                self.assertEqual(result["as_of"], "2026-10-26")
                self.assertEqual(result["as_of_source"], "latest_review_date_fallback")
                self.assertEqual(overall(result)["repeat_count"], 3)
                self.assertEqual(overall(result)["attention_level"], "repeated")

    def test_explicit_issue_is_not_suppressed_by_same_day_inferred_sibling(self):
        data = case(1)
        inferred = copy.deepcopy(data["issues"][0])
        inferred.update(issue_id="i-inferred", basis="inferred")
        data["issues"].insert(0, inferred)
        first = overall(issue_evidence.build(**data))
        data["issues"].reverse()
        reverse = overall(issue_evidence.build(**data))
        self.assertEqual(first["repeat_count"], 1)
        self.assertEqual(first["attention_level"], "single")
        self.assertEqual(signal_fields(first), signal_fields(reverse))

    def test_later_positive_defect_occurrence_survives_earlier_negation(self):
        data = case(1)
        quote = "刚开始不起球，但是洗过以后袖口起球了，毛球附在袖口很难清理。"
        data["reviews"][0].update(content=quote, text=quote)
        data["issues"][0]["evidence"] = quote
        data["clusters"][0]["review_evidence"] = {"r1": quote}
        signal = overall(issue_evidence.build(**data))
        self.assertEqual(signal["repeat_count"], 1)
        self.assertIn("起球", signal["name"])
        self.assertEqual(signal["attention_level"], "single")

    def test_real_append_date_is_included_in_fallback_snapshot(self):
        data = case(1)
        append = "现在后背起球了，摩擦位置满是毛球。"
        data["reviews"][0].update(content="初到手没发现问题。", text="初到手没发现问题。",
                                  date="2026-06-01", append_content=append, append_date="2026-10-28")
        data["issues"][0]["evidence"] = append
        data["clusters"][0]["review_evidence"] = {"r1": append}
        data.update(collection={}, summary={})
        result = issue_evidence.build(**data)
        self.assertEqual(result["as_of"], "2026-10-28")
        self.assertEqual(result["as_of_source"], "latest_review_date_fallback")
        self.assertEqual(overall(result)["repeat_count"], 1)
        self.assertEqual(overall(result)["excluded"]["future_date"], 0)

    def test_two_or_three_cross_sku_reports_are_repeated_without_claiming_four_same_sku(self):
        for count in (2, 3):
            with self.subTest(count=count):
                data = case(count)
                for index, review in enumerate(data["reviews"]):
                    review.update(sku_id=f"sku-{index}", sku=f"规格{index}")
                signal = overall(issue_evidence.build(**data))
                self.assertEqual(signal["repeat_count"], count)
                self.assertEqual(signal["max_same_sku_count"], 1)
                self.assertEqual(signal["attention_level"], "repeated")
                self.assertEqual(signal["tone"], "medium")
                self.assertNotEqual(signal["attention_level"], "repeat")


if __name__ == "__main__":
    unittest.main()
