import json
import tempfile
import types
import unittest
from pathlib import Path

from analysis.gap_analyzer import GapAnalyzer, GapConfig, claim_candidates


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.prompts = []

    def chat_json(self, system, user):
        self.calls += 1
        self.prompts.append(user)
        return types.SimpleNamespace(
            content=json.dumps(self.payload, ensure_ascii=False), usage={}, latency_ms=10
        )


def claim(text, aspect, claim_type="functional", source="title", ocr=None):
    return {
        "claim_id": "PC1",
        "aspect": aspect,
        "claim_type": claim_type,
        "normalized_claim": text,
        "original_text": text,
        "source": source,
        "ocr_confidence": ocr,
    }


def cluster(cid, obj, aspect, name, review_ids, directions=None):
    return {
        "cluster_id": cid,
        "object": obj,
        "aspect": aspect,
        "problem_name": name,
        "description": name,
        "review_count": len(review_ids),
        "review_ids": review_ids,
        "direction_distribution": directions or {},
        "representative_reviews": [
            {"review_id": rid, "evidence": name, "star": 1} for rid in review_ids[:2]
        ],
    }


SAMPLE = {"product_id": "p1", "valid_review_count": 29}


class GapAnalyzerTest(unittest.TestCase):
    def analyze(self, claims, clusters, payload):
        client = FakeClient(payload)
        with tempfile.TemporaryDirectory() as tmp:
            analyzer = GapAnalyzer(client, "fake", Path(tmp), GapConfig())
            result = analyzer.analyze(claims, clusters, SAMPLE)
        return result, client

    def test_object_mismatch_is_not_candidate(self):
        candidate = cluster("C1", "后跟", "穿着舒适性", "后跟偏硬", ["r1"])
        self.assertEqual(claim_candidates(claim("鞋面亲肤细腻", "穿着舒适性"), [candidate]), [])

    def test_size_range_does_not_require_llm(self):
        clusters = [cluster("C1", "尺码", "尺码与鞋型", "尺码偏小", ["r1", "r2"])]
        result, client = self.analyze(
            [claim("尺码覆盖39-44码", "尺码与鞋型", "size_spec")], clusters, {}
        )
        self.assertEqual(client.calls, 0)
        self.assertEqual(result["gaps"][0]["gap_type"], "no_direct_conflict_found")

    def test_capacity_spec_uses_generic_wording_without_shoe_claims(self):
        result, client = self.analyze([claim("规格为20ml*2", "尺寸与规格", "size_spec")], [], {})
        gap = result["gaps"][0]
        self.assertEqual(client.calls, 0)
        self.assertEqual(gap["gap_type"], "no_direct_conflict_found")
        self.assertIn("容量", gap["finding"])
        self.assertNotIn("合脚", gap["finding"])
        self.assertNotIn("尺码范围", gap["finding"])

    def test_conflict_without_evidence_is_downgraded(self):
        clusters = [cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1"])]
        result, _ = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")],
            clusters,
            {
                "gap_type": "potential_conflict",
                "related_cluster_ids": ["C1"],
                "evidence_review_ids": [],
                "finding": "评价说很滑",
            },
        )
        gap = result["gaps"][0]
        self.assertEqual(gap["gap_type"], "insufficient_evidence")
        self.assertEqual(gap["confidence_level"], "insufficient")

    def test_valid_conflict_gets_low_confidence_for_single_review(self):
        clusters = [cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1"])]
        result, _ = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")],
            clusters,
            {
                "gap_type": "potential_conflict",
                "related_cluster_ids": ["C1"],
                "evidence_review_ids": ["r1"],
                "finding": "个别评价反映鞋底打滑",
            },
        )
        gap = result["gaps"][0]
        self.assertEqual(gap["gap_type"], "potential_conflict")
        self.assertEqual(gap["confidence_level"], "low")
        self.assertEqual(gap["independent_review_count"], 1)
        self.assertIn("仅覆盖采集到的低星评价", gap["limitations"][0])

    def test_marketing_slogan_is_insufficient(self):
        result, client = self.analyze(
            [claim("时尚百搭", "其他", "marketing_slogan")], [], {}
        )
        self.assertEqual(client.calls, 0)
        self.assertEqual(result["gaps"][0]["gap_type"], "insufficient_evidence")

    def test_illegal_gap_type_is_downgraded(self):
        clusters = [cluster("C1", "鞋底", "穿着舒适性", "鞋底偏硬", ["r1"])]
        result, _ = self.analyze(
            [claim("商品具有舒适脚感", "穿着舒适性", "comfort_experience")],
            clusters,
            {
                "gap_type": "totally_wrong",
                "related_cluster_ids": ["C1"],
                "evidence_review_ids": ["r1"],
                "finding": "x",
            },
        )
        self.assertEqual(result["gaps"][0]["gap_type"], "insufficient_evidence")

    def test_partial_gap_with_two_evidence(self):
        clusters = [cluster("C1", "鞋底", "穿着舒适性", "鞋底偏硬", ["r1", "r2"])]
        result, _ = self.analyze(
            [claim("商品具有舒适脚感", "穿着舒适性", "comfort_experience")],
            clusters,
            {
                "gap_type": "partial_gap",
                "related_cluster_ids": ["C1"],
                "evidence_review_ids": ["r1", "r2"],
                "finding": "个别场景下偏硬",
            },
        )
        gap = result["gaps"][0]
        self.assertEqual(gap["gap_type"], "partial_gap")
        self.assertEqual(gap["confidence_level"], "medium")

    def test_no_clusters_gives_no_direct_conflict(self):
        result, client = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")], [], {}
        )
        self.assertEqual(client.calls, 0)
        self.assertEqual(result["gaps"][0]["gap_type"], "no_direct_conflict_found")
        self.assertEqual(result["gaps"][0]["confidence_level"], "insufficient")

    def test_unseen_member_cannot_inflate_evidence_strength(self):
        c = cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1", "r2", "r3", "r4"])
        result, client = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")], [c],
            {"gap_type": "potential_conflict", "related_cluster_ids": ["C1"],
             "evidence_review_ids": ["r1", "r2", "r3", "r4"], "finding": "大量评价说不防滑"},
        )
        gap = result["gaps"][0]
        self.assertEqual(gap["evidence_review_ids"], ["r1", "r2"])
        self.assertEqual(gap["independent_review_count"], 2)
        self.assertEqual(gap["confidence_level"], "medium")
        self.assertEqual(gap["evidence_validation"]["rejected_review_ids"], ["r3", "r4"])
        self.assertNotIn("大量", gap["finding"])
        prompt = json.loads(client.prompts[0].split("\n", 1)[1])
        self.assertEqual(prompt["candidates"][0]["review_ids"], ["r1", "r2"])

    def test_only_unseen_citations_downgrade_conflict(self):
        c = cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1", "r2", "r3"])
        result, _ = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")], [c],
            {"gap_type": "potential_conflict", "related_cluster_ids": ["C1"],
             "evidence_review_ids": ["r3"], "finding": "鞋底不防滑"},
        )
        gap = result["gaps"][0]
        self.assertEqual(gap["gap_type"], "insufficient_evidence")
        self.assertEqual(gap["review_evidence"], [])
        self.assertIn("证据不足", gap["finding"])

    def test_evidence_without_cluster_membership_is_not_exposed(self):
        c = cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1"])
        c["review_evidence"] = {"r1": "鞋底很滑", "outsider": "根本不防滑"}
        result, client = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")], [c],
            {"gap_type": "potential_conflict", "related_cluster_ids": ["C1"],
             "evidence_review_ids": ["outsider"], "finding": "鞋底不防滑"},
        )
        prompt = json.loads(client.prompts[0].split("\n", 1)[1])
        self.assertEqual(prompt["candidates"][0]["review_ids"], ["r1"])
        self.assertEqual(result["gaps"][0]["gap_type"], "insufficient_evidence")

    def test_zero_ocr_confidence_caps_strength_at_low(self):
        c = cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1", "r2"])
        result, _ = self.analyze(
            [claim("商品具有防滑性能", "功能与性能", source="image_ocr", ocr=0.0)], [c],
            {"gap_type": "potential_conflict", "related_cluster_ids": ["C1"],
             "evidence_review_ids": ["r1", "r2"], "finding": "少量评价说不防滑"},
        )
        self.assertEqual(result["gaps"][0]["confidence_level"], "low")

    def test_no_conflict_does_not_get_high_confidence_from_unrelated_count(self):
        c = cluster("C1", "鞋底", "功能与性能", "防滑问题不明确", ["r1", "r2"])
        result, _ = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")], [c],
            {"gap_type": "no_direct_conflict_found", "related_cluster_ids": ["C1"],
             "evidence_review_ids": ["r1", "r2"], "finding": "未发现直接反驳"},
        )
        self.assertEqual(result["gaps"][0]["confidence_level"], "insufficient")

    def test_cache_tracks_actual_evidence_not_just_ids(self):
        c = cluster("C1", "鞋底", "功能与性能", "鞋底问题", ["r1"])
        client = FakeClient({"gap_type": "no_direct_conflict_found", "finding": "未发现直接反驳"})
        with tempfile.TemporaryDirectory() as tmp:
            analyzer = GapAnalyzer(client, "fake", Path(tmp))
            item = claim("商品具有防滑性能", "功能与性能")
            analyzer.analyze([item], [c], SAMPLE)
            analyzer.analyze([item], [c], SAMPLE)
            self.assertEqual(client.calls, 1)
            c["review_evidence"] = {"r1": "鞋底一下雨就打滑"}
            analyzer.analyze([item], [c], SAMPLE)
            self.assertEqual(client.calls, 2)

    def test_generic_ineffectiveness_cannot_refute_certification_or_ingredients(self):
        for text in ("产品通过第三方认证", "产品采用植物配方", "商品抑制色素转运"):
            with self.subTest(text=text):
                c = cluster("C1", "整体", "功能与性能", "用了没有任何效果", ["r1"])
                result, _ = self.analyze(
                    [claim(text, "功能与性能")], [c],
                    {"gap_type": "potential_conflict", "related_cluster_ids": ["C1"],
                     "evidence_review_ids": ["r1"], "finding": "没有效果反驳宣传"},
                )
                gap = result["gaps"][0]
                self.assertEqual(gap["gap_type"], "no_direct_conflict_found")
                self.assertEqual(gap["confidence_level"], "insufficient")
                self.assertEqual(gap["evidence_review_ids"], [])
                self.assertEqual(gap["decision_validation"]["reason"], "evidence_does_not_address_claim_scope")

    def test_direct_certification_evidence_is_not_blanket_skipped(self):
        c = cluster("C1", "整体", "功能与性能", "认证编号查不到且客服承认认证过期", ["r1"])
        result, _ = self.analyze(
            [claim("商品通过认证", "功能与性能")], [c],
            {"gap_type": "potential_conflict", "related_cluster_ids": ["C1"],
             "evidence_review_ids": ["r1"], "finding": "个别评价反映认证已过期"},
        )
        self.assertEqual(result["gaps"][0]["gap_type"], "potential_conflict")
        self.assertNotIn("decision_validation", result["gaps"][0])

    def test_saved_quote_is_visible_when_review_occurs_in_multiple_clusters(self):
        first = cluster("C1", "鞋底", "功能与性能", "鞋底打滑", ["same"])
        second = cluster("C2", "鞋底", "功能与性能", "其他问题", ["other", "same"])
        second["review_evidence"] = {"same": "这段没有被展示的原文不能覆盖第一簇证据"}
        client = FakeClient({
            "gap_type": "potential_conflict", "related_cluster_ids": ["C1", "C2"],
            "evidence_review_ids": ["same"], "finding": "个别评价说鞋底打滑",
        })
        with tempfile.TemporaryDirectory() as tmp:
            analyzer = GapAnalyzer(client, "fake", Path(tmp), GapConfig(max_evidence_per_cluster=1))
            result = analyzer.analyze([claim("鞋底防滑", "功能与性能")], [first, second], SAMPLE)
        prompt = json.loads(client.prompts[0].split("\n", 1)[1])
        visible = {(c["cluster_id"], e["review_id"], e["evidence"])
                   for c in prompt["candidates"] for e in c["evidence"]}
        saved = result["gaps"][0]["review_evidence"][0]
        self.assertIn((saved["cluster_id"], saved["review_id"], saved["evidence"]), visible)
        self.assertEqual(saved["cluster_id"], "C1")

    def test_ingredient_or_formula_outcome_can_have_direct_experience_conflict(self):
        for text, evidence in (("成分具有淡化印记功效", "用了一个月痘印都没淡"),
                               ("配方温和亲肤", "涂完过敏刺痛")):
            with self.subTest(text=text):
                c = cluster("C1", "整体", "功能与性能", evidence, ["r1"])
                result, _ = self.analyze(
                    [claim(text, "功能与性能")], [c],
                    {"gap_type": "potential_conflict", "related_cluster_ids": ["C1"],
                     "evidence_review_ids": ["r1"], "finding": "个别评价有相反体验"},
                )
                self.assertEqual(result["gaps"][0]["gap_type"], "potential_conflict")

    def test_json_repair_result_does_not_reuse_original_request_cache(self):
        class SequenceClient:
            def __init__(self):
                self.prompts = []

            def chat_json(self, system, user):
                self.prompts.append(user)
                return types.SimpleNamespace(
                    content="not json" if len(self.prompts) == 1 else json.dumps({
                        "gap_type": "no_direct_conflict_found", "finding": "没有直接证据"
                    }), usage={}, latency_ms=1,
                )

        client = SequenceClient()
        with tempfile.TemporaryDirectory() as tmp:
            analyzer = GapAnalyzer(client, "fake", Path(tmp))
            claims = [claim("商品防滑", "功能与性能")]
            candidates = [cluster("C1", "鞋底", "功能与性能", "鞋底问题", ["r1"])]
            analyzer.analyze(claims, candidates, SAMPLE)
            self.assertEqual(len(client.prompts), 2)
            self.assertIn("上一次输出无法解析", client.prompts[1])
            analyzer.analyze(claims, candidates, SAMPLE)
            self.assertEqual(len(client.prompts), 3)
            self.assertEqual(client.prompts[0], client.prompts[2])
            analyzer.analyze(claims, candidates, SAMPLE)
            self.assertEqual(len(client.prompts), 3)


class GapProgressTest(unittest.TestCase):
    def test_on_progress_reports_each_claim_that_needs_the_model(self):
        clusters = [cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1"])]
        claims = [
            {**claim("商品具有防滑性能", "功能与性能"), "claim_id": "PC1"},
            {**claim("鞋底耐磨", "功能与性能"), "claim_id": "PC2"},
            {**claim("大底抓地力强", "功能与性能"), "claim_id": "PC3"},
            # 规格类主张由规则判断，不消耗模型调用，也不该出现在进度里
            {**claim("规格为20ml*2", "尺寸与规格", "size_spec"), "claim_id": "PC4"},
        ]
        seen = []
        client = FakeClient({
            "gap_type": "potential_conflict",
            "related_cluster_ids": ["C1"],
            "evidence_review_ids": ["r1"],
            "finding": "评价说很滑",
        })
        with tempfile.TemporaryDirectory() as tmp:
            GapAnalyzer(client, "fake", Path(tmp), GapConfig()).analyze(
                claims, clusters, SAMPLE,
                on_progress=lambda done, total: seen.append((done, total)),
            )
        self.assertEqual(sorted(done for done, _ in seen), [1, 2, 3])
        self.assertTrue(all(total == 3 for _, total in seen))
        self.assertEqual(seen[-1][0], 3)

    def test_progress_callback_errors_do_not_break_analysis(self):
        clusters = [cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1"])]
        client = FakeClient({
            "gap_type": "potential_conflict",
            "related_cluster_ids": ["C1"],
            "evidence_review_ids": ["r1"],
            "finding": "评价说很滑",
        })

        def boom(_done, _total):
            raise RuntimeError("progress failed")

        with tempfile.TemporaryDirectory() as tmp:
            result = GapAnalyzer(client, "fake", Path(tmp), GapConfig()).analyze(
                [claim("商品具有防滑性能", "功能与性能")], clusters, SAMPLE, on_progress=boom,
            )
        self.assertEqual(result["gap_count"], 1)


class EvidenceKindTest(unittest.TestCase):
    """v6 起，逐条证据类型决定冲突判断能否成立。"""

    def analyze(self, claims, clusters, payload):
        client = FakeClient(payload)
        with tempfile.TemporaryDirectory() as tmp:
            analyzer = GapAnalyzer(client, "fake", Path(tmp), GapConfig())
            return analyzer.analyze(claims, clusters, SAMPLE), client

    def test_topic_only_evidence_downgrades_conflict(self):
        clusters = [cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1"])]
        result, _ = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")],
            clusters,
            {
                "gap_type": "potential_conflict",
                "related_cluster_ids": ["C1"],
                "evidence_review_ids": ["r1"],
                "evidence": [{
                    "review_id": "r1",
                    "relation": "indirect",
                    "evidence_kind": "topic_only",
                    "negative_fact": False,
                    "quote": "买之前看过防滑介绍",
                }],
                "finding": "提到防滑",
            },
        )
        gap = result["gaps"][0]
        self.assertEqual(gap["gap_type"], "no_direct_conflict_found")
        self.assertEqual(gap["evidence_review_ids"], [])
        self.assertEqual(gap["decision_validation"]["reason"], "evidence_is_topic_mention_only")
        self.assertIn("只提到相关属性", gap["finding"])

    def test_related_negative_without_contradiction_becomes_partial(self):
        clusters = [cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1"])]
        result, _ = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")],
            clusters,
            {
                "gap_type": "potential_conflict",
                "related_cluster_ids": ["C1"],
                "evidence_review_ids": ["r1"],
                "evidence": [{
                    "review_id": "r1",
                    "relation": "indirect",
                    "evidence_kind": "related_negative",
                    "negative_fact": True,
                    "quote": "雨天走楼梯有点打滑",
                }],
                "finding": "个别评价提到打滑",
            },
        )
        gap = result["gaps"][0]
        self.assertEqual(gap["gap_type"], "partial_gap")
        self.assertEqual(gap["evidence_review_ids"], ["r1"])

    def test_evidence_detail_records_kind_and_negative_fact(self):
        clusters = [cluster("C1", "鞋底", "功能与性能", "不防滑", ["r1"])]
        result, _ = self.analyze(
            [claim("商品具有防滑性能", "功能与性能")],
            clusters,
            {
                "gap_type": "potential_conflict",
                "related_cluster_ids": ["C1"],
                "evidence_review_ids": ["r1"],
                "evidence": [{
                    "review_id": "r1",
                    "relation": "contradiction",
                    "evidence_kind": "contradiction",
                    "negative_fact": True,
                    "fact_quality": "concrete",
                    "severity": "major",
                    "quote": "下雨天鞋底很滑",
                }],
                "finding": "个别评价反映鞋底打滑",
            },
        )
        detail = result["gaps"][0]["evidence_detail"][0]
        self.assertEqual(detail["evidence_kind"], "contradiction")
        self.assertTrue(detail["negative_fact"])
        self.assertEqual(detail["quality"], "concrete")
        self.assertEqual(detail["severity"], "major")


if __name__ == "__main__":
    unittest.main()
