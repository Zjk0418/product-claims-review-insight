"""近义主张分组与分组计数的单元测试。"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import analysis.claim_dedup as claim_dedup

from analysis.claim_dedup import (
    grouped_by_type,
    grouped_conflict_evidence,
    group_near_duplicate_claims,
)


def _claim(cid: str, norm: str, group: str = "") -> dict:
    return {"claim_id": cid, "normalized_claim": norm, "claim_group": group}


class GroupNearDuplicateTest(unittest.TestCase):
    def test_frozen_bundle_model_is_resolved_and_passed_to_embedder(self):
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "_MEIPASS", tmp, create=True):
                module = importlib.reload(claim_dedup)
                expected = Path(tmp).resolve() / "models/bge-small-zh-v1.5"
                self.assertEqual(module.DEFAULT_MODEL_DIR, expected)
                embedder = Mock()
                embedder.encode.return_value = np.array([[1., 0.], [1., 0.]])
                with patch("analysis.embeddings.BGEEmbedder", return_value=embedder) as factory:
                    groups, mapping = module.group_near_duplicate_claims([
                        _claim("c1", "容量680ml"), _claim("c2", "容量680ml")])
                factory.assert_called_once_with(model_dir=expected)
                self.assertEqual(len(groups), 1)
                self.assertEqual(mapping["c1"], mapping["c2"])
                self.assertEqual(groups[0]["grouping"]["status"], "ok")
                self.assertEqual(groups[0]["grouping"]["method"], "bge_cosine")
        finally:
            importlib.reload(claim_dedup)

    def test_model_failure_is_visible_without_leaking_exception_paths(self):
        from analysis.embeddings import ModelUnavailable

        private_path = "C:/private-user/model-token-secret"
        with patch("analysis.embeddings.BGEEmbedder", side_effect=ModelUnavailable(private_path)), self.assertWarns(RuntimeWarning):
            groups, mapping = group_near_duplicate_claims([_claim("c1", "相同主张"), _claim("c2", "相同主张")])
        self.assertEqual(len(groups), 2)
        self.assertNotEqual(mapping["c1"], mapping["c2"])
        self.assertTrue(all(g["grouping"]["status"] == "degraded" for g in groups))
        self.assertEqual(groups[0]["grouping"]["failure_stage"], "model_load")
        self.assertEqual(groups[0]["grouping"]["error_type"], "ModelUnavailable")
        self.assertNotIn(private_path, json.dumps(groups))

    def test_invalid_or_failed_embeddings_are_not_reported_as_successful_grouping(self):
        for value in (np.array([[np.nan, 0.], [1., 0.]]), np.zeros((2, 2)), np.ones((1, 2))):
            with self.subTest(shape=value.shape), patch("analysis.embeddings.BGEEmbedder") as factory, self.assertWarns(RuntimeWarning):
                factory.return_value.encode.return_value = value
                groups, mapping = group_near_duplicate_claims([_claim("c1", "主张甲"), _claim("c2", "主张乙")])
                self.assertEqual(len(groups), 2)
                self.assertEqual(groups[0]["grouping"]["status"], "degraded")
                self.assertEqual(groups[0]["grouping"]["failure_stage"], "vector_validation")
        with patch("analysis.embeddings.BGEEmbedder") as factory, self.assertWarns(RuntimeWarning):
            factory.return_value.encode.side_effect = RuntimeError("unavailable")
            groups, _ = group_near_duplicate_claims([_claim("c1", "主张")])
            self.assertEqual(groups[0]["grouping"]["failure_stage"], "encode")

    def test_explicit_model_directory_still_overrides_bundle_default(self):
        embedder = Mock()
        embedder.encode.return_value = np.array([[1., 0.]])
        with patch("analysis.embeddings.BGEEmbedder", return_value=embedder) as factory:
            groups, _ = group_near_duplicate_claims([_claim("c1", "主张")], model_dir=Path("custom-model"))
        factory.assert_called_once_with(model_dir=Path("custom-model"))
        self.assertEqual(groups[0]["grouping"]["status"], "ok")

    def test_empty_text_groups_explicitly_skip_model_work(self):
        with patch("analysis.embeddings.BGEEmbedder") as factory:
            groups, _ = group_near_duplicate_claims([_claim("c1", ""), _claim("c2", " ")])
        factory.assert_not_called()
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(g["grouping"]["status"] == "not_applicable" for g in groups))

    def test_groups_semantic_duplicates(self):
        claims = [
            _claim("c1", "商品可淡化痘印"),
            _claim("c2", "商品能精准淡化痘印"),
            _claim("c3", "商品可去除黑色素沉淀"),
            _claim("c4", ""),
        ]
        groups, mapping = group_near_duplicate_claims(claims)
        self.assertEqual(len(groups), 3)  # 痘印两条同组 + 黑色素 + 空文本自成组
        self.assertEqual(mapping["c1"], mapping["c2"])
        self.assertNotEqual(mapping["c1"], mapping["c3"])
        self.assertEqual(mapping["c4"], groups[-1]["group_id"])
        sizes = {g["group_id"]: g["size"] for g in groups}
        self.assertEqual(sizes[mapping["c1"]], 2)
        for group in groups:
            self.assertTrue(group["representative"] or group["size"] == 1)

    def test_empty_claims_degrade_to_no_groups(self):
        self.assertEqual(group_near_duplicate_claims([]), ([], {}))

    def test_duplicate_extra_accounting(self):
        claims = [_claim(f"c{i}", "商品具有防滑性能") for i in range(5)]
        claims.append(_claim("c9", "商品具有减震性能"))
        groups, mapping = group_near_duplicate_claims(claims)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(claims) - len(groups), 4)


class GroupedByTypeTest(unittest.TestCase):
    def test_group_takes_strongest_verdict_once(self):
        mapping = {"c1": "CG01", "c2": "CG01", "c3": "CG02"}
        gaps = [
            {"gap_id": "g1", "claim_id": "c1", "gap_type": "potential_conflict",
             "evidence_review_ids": ["r1", "r2"]},
            {"gap_id": "g2", "claim_id": "c2", "gap_type": "potential_conflict",
             "evidence_review_ids": ["r1"]},
            {"gap_id": "g3", "claim_id": "c3", "gap_type": "insufficient_evidence",
             "evidence_review_ids": []},
        ]
        counts = grouped_by_type(gaps, mapping)
        self.assertEqual(counts, {"potential_conflict": 1, "insufficient_evidence": 1})

    def test_partial_gap_beats_no_conflict(self):
        mapping = {"c1": "CG01", "c2": "CG01"}
        gaps = [
            {"gap_id": "g1", "claim_id": "c1", "gap_type": "no_direct_conflict_found",
             "evidence_review_ids": []},
            {"gap_id": "g2", "claim_id": "c2", "gap_type": "partial_gap",
             "evidence_review_ids": ["r5"]},
        ]
        self.assertEqual(grouped_by_type(gaps, mapping), {"partial_gap": 1})

    def test_ungrouped_claims_count_individually(self):
        gaps = [
            {"gap_id": "g1", "claim_id": "cx", "gap_type": "potential_conflict",
             "evidence_review_ids": ["r1"]},
        ]
        self.assertEqual(grouped_by_type(gaps, {}), {"potential_conflict": 1})

    def test_grouped_conflict_evidence_merges_ids(self):
        mapping = {"c1": "CG01", "c2": "CG01"}
        gaps = [
            {"gap_id": "g1", "claim_id": "c1", "gap_type": "potential_conflict",
             "evidence_review_ids": ["r1", "r2"]},
            {"gap_id": "g2", "claim_id": "c2", "gap_type": "potential_conflict",
             "evidence_review_ids": ["r2", "r3"]},
        ]
        merged = grouped_conflict_evidence(gaps, mapping)
        self.assertEqual(merged["CG01"], ["r1", "r2", "r3"])


if __name__ == "__main__":
    unittest.main()
