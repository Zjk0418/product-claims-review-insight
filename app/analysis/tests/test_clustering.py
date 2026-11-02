import unittest

import numpy as np

from analysis.clustering import _merge_same_key, _split_by_rules, cluster_issues
from analysis.embeddings import embed_issues
from analysis.issue_schema import Issue, make_issue_id


def make_issue(review_id: str, obj: str, aspect: str, manifestation: str, direction: str) -> Issue:
    return Issue(
        review_id=review_id,
        object=obj,
        aspect=aspect,
        manifestation=manifestation,
        direction=direction,
        severity="medium",
        evidence=manifestation,
        evidence_check="exact",
        confidence=0.9,
        sku_id="sku",
        star=1,
        date="2026-01-01",
        issue_id=make_issue_id(review_id, obj, aspect, manifestation, direction),
    )


class ClusteringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.issues = [
            make_issue("r1", "鞋头", "尺码与鞋型", "鞋头空间偏小", "too_small"),
            make_issue("r2", "鞋头", "尺码与鞋型", "前脚掌空间太窄", "too_narrow"),
            make_issue("r3", "鞋头", "尺码与鞋型", "鞋头挤脚趾", "too_small"),
            make_issue("r4", "尺码", "尺码与鞋型", "鞋码偏大", "too_large"),
            make_issue("r5", "尺码", "尺码与鞋型", "鞋码偏小", "too_small"),
            make_issue("r6", "鞋底", "穿着舒适性", "鞋底太硬", "too_hard"),
            make_issue("r7", "鞋底", "穿着舒适性", "鞋底偏软", "too_soft"),
            make_issue("r8", "后跟", "穿着舒适性", "后跟磨脚", "rubbing"),
            make_issue("r9", "后跟", "穿着舒适性", "后跟磨脚", "rubbing"),
        ]
        _, cls.vectors, _ = embed_issues(cls.issues, cache_path=None)

    def test_synonyms_merge(self):
        result = cluster_issues(self.issues, self.vectors, {}, sim_threshold=0.78)
        groups = [
            set(cluster["member_issue_ids"]) for cluster in result["clusters"]
        ]
        head_ids = {issue.issue_id for issue in self.issues[:3]}
        self.assertTrue(any(head_ids <= group for group in groups))

    def test_opposites_not_merged(self):
        result = cluster_issues(self.issues, self.vectors, {}, sim_threshold=0.78)
        assignments = {row["issue_id"]: row["cluster_id"] for row in result["issue_assignments"]}
        self.assertNotEqual(assignments[self.issues[3].issue_id], assignments[self.issues[4].issue_id])
        self.assertNotEqual(assignments[self.issues[5].issue_id], assignments[self.issues[6].issue_id])
        self.assertEqual(result["metrics"]["conflict_clusters_after_split"], 0)

    def test_review_count_is_unique(self):
        issues = [
            make_issue("r8", "后跟", "穿着舒适性", "后跟磨脚", "rubbing"),
            make_issue("r8", "后跟", "穿着舒适性", "后跟磨脚疼", "rubbing"),
        ]
        _, vectors, _ = embed_issues(issues, cache_path=None)
        result = cluster_issues(issues, vectors, {}, sim_threshold=0.78)
        for cluster in result["clusters"]:
            self.assertEqual(len(cluster["review_ids"]), len(set(cluster["review_ids"])))

    def test_defect_signature_prevents_wrong_merge(self):
        """长痘 vs 过敏 向量相似度 0.915，但是两个具体缺陷，不能合并。"""
        issues = [
            make_issue("a1", "皮肤", "功效与效果", "用了长痘了", "irritation"),
            make_issue("a2", "皮肤", "功效与效果", "过敏了", "irritation"),
        ]
        _, vectors, _ = embed_issues(issues, cache_path=None)
        result = cluster_issues(issues, vectors, {}, sim_threshold=0.78)
        assignments = {row["issue_id"]: row["cluster_id"] for row in result["issue_assignments"]}
        self.assertNotEqual(assignments[issues[0].issue_id], assignments[issues[1].issue_id])

    def test_same_defect_different_severity_merges(self):
        """没效果 vs 效果一般 是同一缺陷的不同程度，应合并。"""
        issues = [
            make_issue("b1", "整体", "功效与效果", "一点作用没有", "ineffective"),
            make_issue("b2", "整体", "功效与效果", "效果一般般", "other"),
        ]
        _, vectors, _ = embed_issues(issues, cache_path=None)
        result = cluster_issues(issues, vectors, {}, sim_threshold=0.78)
        assignments = {row["issue_id"]: row["cluster_id"] for row in result["issue_assignments"]}
        self.assertEqual(assignments[issues[0].issue_id], assignments[issues[1].issue_id])

    def test_signature_can_be_disabled_for_ablation(self):
        issues = [
            make_issue("c1", "皮肤", "功效与效果", "用了长痘了", "irritation"),
            make_issue("c2", "皮肤", "功效与效果", "过敏了", "irritation"),
        ]
        _, vectors, _ = embed_issues(issues, cache_path=None)
        result = cluster_issues(
            issues, vectors, {}, sim_threshold=0.78, use_defect_signature=False
        )
        assignments = {row["issue_id"]: row["cluster_id"] for row in result["issue_assignments"]}
        self.assertEqual(assignments[issues[0].issue_id], assignments[issues[1].issue_id])

    def test_unknown_signature_cannot_bridge_distinct_defects(self):
        """单条无法识别的客服抱怨不能把态度差与失联间接串成一簇。"""
        issues = [
            make_issue("bridge1", "客服", "客服与售后", "服务态度恶劣", "other"),
            make_issue("bridge2", "客服", "客服与售后", "这体验说不上来", "other"),
            make_issue("bridge3", "客服", "客服与售后", "买完后马上消失", "other"),
        ]
        # 即使向量完全相同，两次规则合并也必须保持缺陷约束。
        vectors = np.array([[1.0, 0.0]] * len(issues), dtype=np.float32)
        split = _split_by_rules(np.array([0, 0, 0]), issues, vectors)
        self.assertNotEqual(split[0], split[2])
        merged = _merge_same_key(np.array([0, 1, 2]), issues, vectors)
        self.assertNotEqual(merged[0], merged[2])

    def test_identity_ad_mismatch_and_brand_are_distinct(self):
        issues = [
            make_issue("identity1", "整体", "其他", "疑似假货", "other"),
            make_issue("identity2", "整体", "其他", "图文不符", "other"),
            make_issue("identity3", "整体", "其他", "品牌不明", "other"),
        ]
        vectors = np.array([[1.0, 0.0]] * len(issues), dtype=np.float32)
        split = _split_by_rules(np.array([0, 0, 0]), issues, vectors)
        result = _merge_same_key(split, issues, vectors)
        self.assertEqual(len(set(result)), 3)


if __name__ == "__main__":
    unittest.main()
