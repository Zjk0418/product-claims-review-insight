"""样本负面占比不得混入宣传指数，不得对重叠评论重复计数。"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reporting
import sample_risk


def dataset():
    reviews = [{"review_id": f"r{i}", "star": 2, "text": "线头多，异味很大", "valid_for_analysis": i < 22}
               for i in range(33)]
    return {"reviews": reviews, "statistics": {"clusters": [
        {"cluster_id": "a", "aspect": "做工质量", "problem_name": "线头多", "review_ids": [f"r{i}" for i in range(10)]},
        {"cluster_id": "b", "aspect": "气味", "problem_name": "异味", "review_ids": [f"r{i}" for i in range(5, 13)]},
        {"cluster_id": "c", "aspect": "耐用性", "problem_name": "起球", "review_ids": ["r14"]}]},
        "claims": [{"claim_id": "c1", "aspect": "耐用性", "normalized_claim": "不易起球"}],
        "collection": {"scanned_count": 300, "matched_count": 33}}


class SampleProportionTests(unittest.TestCase):
    def test_explicit_numerator_denominator_and_color(self):
        value = sample_risk.signal([f"r{i}" for i in range(13)], 300, 22, "collection_info")
        self.assertEqual(value["scanned"]["numerator"], 13)
        self.assertEqual(value["scanned"]["denominator"], 300)
        self.assertEqual(value["scanned"]["rate"], .043333)
        self.assertEqual(value["scanned"]["band"], "medium")
        self.assertEqual(value["eligible"]["rate"], .590909)
        self.assertEqual(value["eligible"]["band"], "high")
        self.assertTrue(value["eligible"]["small_sample"])
        self.assertFalse(value["calibrated"])

    def test_threshold_boundaries_and_null_vs_zero(self):
        for count, total, band in ((0, 0, "none"), (0, 300, "none"), (1, 300, "low"),
                                   (9, 300, "medium"), (29, 300, "medium"), (30, 300, "high")):
            with self.subTest(count=count, total=total):
                value = sample_risk.proportion(count, total)
                self.assertEqual(value["band"], band)
                self.assertEqual(value["rate"], None if not total else round(count / total, 6))
        self.assertIsNone(sample_risk.proportion(0, 0)["ci95"])
        self.assertGreater(sample_risk.proportion(0, 300)["ci95"][1], 0)
        for count, total in ((-1, 300), (301, 300), (0, -1)):
            with self.assertRaises(ValueError):
                sample_risk.proportion(count, total)

    def test_wilson_matches_existing_reference_and_contains_observed_rate(self):
        self.assertEqual(sample_risk.wilson_interval(1, 500), [.000353, .011241])
        for total in (1, 22, 300):
            for count in (0, total // 2, total):
                lo, hi = sample_risk.wilson_interval(count, total)
                self.assertLessEqual(lo, count / total)
                self.assertGreaterEqual(hi, count / total)

    def test_id_union_not_sum_and_unadvertised_is_independent_of_index(self):
        model = reporting.build_report_model(**dataset())
        overall = model["overview"]["sample_risk"]
        extra = model["overview"]["unadvertised_risk"]
        self.assertEqual(overall["review_count"], 14)
        self.assertEqual(extra["review_count"], 13)  # 10+8 overlap5, not18
        self.assertEqual(extra["aspect_count"], 2)
        self.assertEqual(extra["problem_count"], 2)
        self.assertEqual(extra["scanned"]["rate"], .043333)
        self.assertEqual(extra["eligible"]["rate"], .590909)
        self.assertEqual(extra["warning_level"], "medium")
        self.assertIn("较多未对应商品宣传", extra["warning_text"])
        self.assertIsNone(model["overview"]["risk_summary"]["risk_index"])

    def test_repeated_problem_rows_do_not_inflate_summary(self):
        data = dataset()
        data["statistics"]["clusters"].append(copy.deepcopy(data["statistics"]["clusters"][0]))
        model = reporting.build_report_model(**data)
        self.assertEqual(model["overview"]["unadvertised_risk"]["review_count"], 13)

    def test_complete_problem_evidence_is_preserved_beyond_12_rows_and_20_ids(self):
        data = dataset()
        ids = [f"r{i}" for i in range(22)]
        data["statistics"]["clusters"] = [{"cluster_id": f"p{i}", "aspect": "做工质量", "problem_name": f"问题{i}",
                                            "review_ids": ids} for i in range(15)]
        model = reporting.build_report_model(**data)
        self.assertEqual(len(model["product_issues"]), 15)
        self.assertEqual(len(model["product_issues"][0]["review_ids"]), 22)
        self.assertEqual(model["product_issues"][0]["sample_risk"]["scanned"]["numerator"], 22)
        self.assertEqual(model["overview"]["unadvertised_risk"]["review_count"], 22)

    def test_invalid_review_and_topic_only_cannot_create_extra_warning(self):
        data = dataset()
        data["statistics"]["clusters"] = [{"aspect": "气味", "problem_name": "异味", "review_ids": ["r32", "missing"]}]
        model = reporting.build_report_model(**data)
        extra = model["overview"]["unadvertised_risk"]
        self.assertEqual(extra["review_count"], 0)
        self.assertEqual(extra["warning_level"], "none")
        self.assertEqual(extra["warning_text"], "")

    def test_safety_warning_not_silenced_by_low_rate(self):
        data = dataset()
        data["statistics"]["clusters"] = [{"aspect": "安全", "problem_name": "漏电", "review_ids": ["r1"]}]
        extra = reporting.build_report_model(**data)["overview"]["unadvertised_risk"]
        self.assertEqual(extra["scanned"]["band"], "low")
        self.assertEqual(extra["warning_level"], "high")
        self.assertIn("安全相关线索", extra["warning_text"])

    def test_json_export_and_missing_scan_basis_are_explicit(self):
        data = dataset()
        data.pop("collection")
        before = copy.deepcopy(data)
        model = reporting.build_report_model(**data)
        self.assertEqual(data, before)
        self.assertEqual(model["overview"]["sample_risk"]["basis"], "analysis_input")
        exported = json.loads(reporting.artifact_bytes(model, "reviews_json"))
        self.assertEqual(exported["overview"]["unadvertised_risk"], model["overview"]["unadvertised_risk"])
        self.assertFalse(exported["method"]["probability_calibrated"])


if __name__ == "__main__":
    unittest.main()
