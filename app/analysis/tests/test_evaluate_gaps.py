import unittest

from analysis.evaluate_gaps import evaluate


class EvaluateGapsTest(unittest.TestCase):
    def test_accuracy_and_mismatch(self):
        gaps = {
            "c1": {"gap_type": "potential_conflict", "confidence_level": "medium"},
            "c2": {"gap_type": "insufficient_evidence", "confidence_level": "insufficient"},
        }
        gold = {
            "criteria": "test",
            "cases": [
                {
                    "claim_id": "c1",
                    "expected_gap_type": "potential_conflict",
                    "expected_confidence": "medium",
                },
                {
                    "claim_id": "c2",
                    "expected_gap_type": "no_direct_conflict_found",
                    "expected_confidence": "insufficient",
                },
            ],
        }
        result = evaluate(gaps, gold)
        self.assertEqual(result["evaluated"], 2)
        self.assertEqual(result["correct"], 1)
        self.assertEqual(result["accuracy"], 0.5)
        self.assertEqual(result["confidence_accuracy"], 1.0)
        self.assertEqual(len(result["mismatches"]), 1)
        self.assertEqual(result["per_type"]["potential_conflict"]["correct"], 1)
        self.assertEqual(result["per_type"]["no_direct_conflict_found"]["support"], 1)
        self.assertEqual(result["per_type"]["no_direct_conflict_found"]["correct"], 0)

    def test_missing_claim(self):
        gold = {"cases": [{"claim_id": "missing", "expected_gap_type": "insufficient_evidence"}]}
        result = evaluate({}, gold)
        self.assertEqual(result["evaluated"], 0)
        self.assertEqual(result["accuracy"], 0.0)
        self.assertEqual(result["missing_claim_ids"], ["missing"])

    def test_per_class_prf_and_confusion(self):
        gaps = {
            "c1": {"gap_type": "potential_conflict"},
            "c2": {"gap_type": "potential_conflict"},
            "c3": {"gap_type": "no_direct_conflict_found"},
            "c4": {"gap_type": "insufficient_evidence"},
        }
        gold = {
            "cases": [
                {"claim_id": "c1", "expected_gap_type": "potential_conflict"},
                {"claim_id": "c2", "expected_gap_type": "no_direct_conflict_found"},
                {"claim_id": "c3", "expected_gap_type": "no_direct_conflict_found"},
                {"claim_id": "c4", "expected_gap_type": "insufficient_evidence"},
            ]
        }
        result = evaluate(gaps, gold)
        cls = result["classification"]
        conflict = cls["per_class"]["potential_conflict"]
        self.assertEqual(conflict["support"], 1)
        self.assertEqual(conflict["predicted"], 2)
        self.assertEqual(conflict["precision"], 0.5)
        self.assertEqual(conflict["recall"], 1.0)
        self.assertAlmostEqual(conflict["f1"], 0.6667, places=4)
        no_direct = cls["per_class"]["no_direct_conflict_found"]
        self.assertEqual(no_direct["precision"], 1.0)
        self.assertEqual(no_direct["recall"], 0.5)
        self.assertEqual(cls["confusion_matrix"]["potential_conflict"], {"potential_conflict": 1})
        self.assertEqual(cls["confusion_matrix"]["no_direct_conflict_found"]["potential_conflict"], 1)
        self.assertIsNone(cls["per_class"]["partial_gap"]["precision"])
        self.assertEqual(cls["unknown_gold_labels"], 0)

    def test_evidence_metrics(self):
        gaps = {
            "c1": {"gap_type": "potential_conflict", "evidence_review_ids": ["r1", "r2"]},
            "c2": {"gap_type": "insufficient_evidence", "evidence_review_ids": []},
        }
        gold = {
            "cases": [
                {
                    "claim_id": "c1",
                    "expected_gap_type": "potential_conflict",
                    "expected_evidence_review_ids": ["r1"],
                },
                {
                    "claim_id": "c2",
                    "expected_gap_type": "insufficient_evidence",
                    "expected_evidence_review_ids": ["r9"],
                },
            ]
        }
        result = evaluate(gaps, gold)
        evidence = result["evidence"]
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["cases"], 2)
        self.assertEqual(evidence["any_overlap_rate"], 0.5)
        self.assertEqual(evidence["mean_recall"], 0.5)
        self.assertEqual(evidence["mean_precision"], 0.25)

    def test_evidence_metrics_absent(self):
        result = evaluate({"c1": {"gap_type": "potential_conflict"}}, {"cases": [{"claim_id": "c1", "expected_gap_type": "potential_conflict"}]})
        self.assertFalse(result["evidence"]["available"])


if __name__ == "__main__":
    unittest.main()
