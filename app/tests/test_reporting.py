"""报告模型采样口径、证据统计、可移植预览和内存导出的行为验收。"""
import copy
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import reporting


def fixture():
    reviews = [{"review_id": "001", "star": 1, "text": "用了过敏", "valid_for_analysis": True},
               {"review_id": "002", "star": 2, "text": "使用后泛红", "valid_for_analysis": True},
               {"review_id": "003", "star": 3, "text": "使用以后刺痛", "valid_for_analysis": True},
               {"review_id": "004", "star": 1, "text": "该用户未填写评价内容", "valid_for_analysis": False}]
    claims = [{"claim_id": "c1", "claim_group": "same", "aspect": "使用舒适性", "normalized_claim": "配方温和", "original_text": "温和"},
              {"claim_id": "c2", "claim_group": "same", "aspect": "使用舒适性", "normalized_claim": "温和亲肤", "original_text": "亲肤"}]
    gaps = [{"claim_id": "c1", "gap_type": "potential_conflict", "confidence_level": "high", "finding": "部分评价有相反体验", "evidence_review_ids": ["001", "002"], "review_evidence": [
                {"review_id": "001", "evidence": "用了过敏"}, {"review_id": "002", "evidence": "使用后泛红"}]},
            {"claim_id": "c2", "gap_type": "potential_conflict", "confidence_level": "medium", "finding": "部分评价有相反体验", "evidence_review_ids": ["002", "003"], "review_evidence": [
                {"review_id": "002", "evidence": "使用后泛红"}, {"review_id": "003", "evidence": "使用以后刺痛"}]}]
    return {"summary": {"product_id": "123", "valid_review_count": 3, "analysis_status": "completed", "issue_count": 3},
            "statistics": {"clusters": [{"cluster_id": "C1", "aspect": "使用舒适性", "problem_name": "皮肤刺激", "review_ids": ["001", "002", "003"]}]},
            "reviews": reviews, "claims": claims, "gaps": gaps,
            "product": {"title": "测试商品", "api_key": "never-export-this", "attributes": {"成分": "植物", "Authorization": "another-secret"}}}


def document_case(gap_type=None, collection=None, n_low=6, n_high=494, valid_low=1, evidence=1):
    """文档 §13 的 500/6/1 边界样例；valid_low 控制 E，gap_type 控制 M/U 是否已评估。"""
    reviews = []
    for index in range(n_low + n_high):
        low = index < n_low
        reviews.append({
            "review_id": f"r{index:03d}", "star": 1 if low else 5,
            "text": "使用后过敏" if low else "很好",
            "valid_for_analysis": low and index < valid_low,
        })
    claims = [{"claim_id": "c1", "claim_group": "g1", "aspect": "使用舒适性",
               "normalized_claim": "配方温和", "original_text": "温和"}]
    clusters = [{"cluster_id": "C1", "aspect": "使用舒适性", "problem_name": "皮肤刺激",
                 "review_ids": [f"r{index:03d}" for index in range(min(valid_low, n_low))]}]
    gaps = []
    if gap_type:
        ids = [f"r{index:03d}" for index in range(min(evidence, valid_low))]
        gaps = [{"claim_id": "c1", "gap_type": gap_type, "confidence_level": "high",
                 "finding": "使用后出现刺激", "evidence_review_ids": ids,
                 "review_evidence": [{"review_id": rid, "evidence": "使用后过敏"} for rid in ids]}]
    return reporting.build_report_model(
        summary={"product_id": "doc", "valid_review_count": valid_low, "analysis_status": "completed"},
        statistics={"clusters": clusters}, reviews=reviews, claims=claims, gaps=gaps,
        product={"title": "文档样例"}, collection=collection or {})


class ReportModelTests(unittest.TestCase):
    def test_sampling_and_evidence_rates_are_explicit(self):
        model = reporting.build_report_model(**fixture())
        sampling = model["sampling"]
        self.assertEqual(sampling["scanned_review_count"], 4)
        self.assertEqual(sampling["low_star_review_count"], 4)
        self.assertEqual(sampling["effective_negative_review_count"], 3)
        self.assertAlmostEqual(sampling["low_star_rate"], 1.0)
        self.assertAlmostEqual(sampling["effective_low_star_rate"], 0.75)
        aspect = model["aspects"][0]
        self.assertEqual(aspect["evidence_status"], "confirmed")
        self.assertEqual(aspect["contradiction_count"], 3)
        self.assertEqual(aspect["possible_count"], 0)
        self.assertEqual(aspect["negative_count"], 3)
        self.assertAlmostEqual(aspect["gap_evidence_rate"], 0.75)
        self.assertEqual(aspect["gap_evidence_ci"], [0.300636, 0.954414])
        self.assertAlmostEqual(aspect["negative_concentration"], 1.0)
        self.assertIn(aspect["severity"], {"minor", "moderate", "major", "critical", "unknown"})
        self.assertEqual(aspect["severity_status"],
                         "not_labeled" if aspect["severity"] == "unknown" else "labeled")
        self.assertGreaterEqual(aspect["risk_index"], 0)
        self.assertLessEqual(aspect["risk_index"], 100)
        self.assertIn(aspect["risk_band"], {"high", "medium", "low"})
        self.assertEqual(model["overview"]["gap_evidence_count"], 3)
        self.assertEqual(model["overview"]["claim_matching_status"], "completed")

    def test_invalid_or_unknown_reviews_cannot_support_conflict(self):
        data = fixture()
        for gap in data["gaps"]:
            gap["evidence_review_ids"] = ["004", "unknown"]
        model = reporting.build_report_model(**data)
        aspect = model["aspects"][0]
        self.assertEqual(aspect["contradiction_count"], 0)
        self.assertEqual(aspect["evidence_status"], "insufficient")
        self.assertIsNone(aspect["gap_evidence_rate"])
        self.assertTrue(model["warnings"])

    def test_missing_denominator_is_not_evaluated_not_zero(self):
        data = fixture()
        data["reviews"] = []
        data["summary"]["valid_review_count"] = 0
        model = reporting.build_report_model(**data)
        aspect = model["aspects"][0]
        self.assertIsNone(aspect["gap_evidence_rate"])
        self.assertIsNone(aspect["gap_evidence_ci"])
        self.assertEqual(model["sampling"]["scanned_review_count"], 0)

    def test_inputs_unchanged_and_no_configuration_leaks(self):
        data = fixture()
        before = copy.deepcopy(data)
        model = reporting.build_report_model(**data)
        self.assertEqual(data, before)
        for artifact in ("product_json", "reviews_json", "report_html"):
            output = reporting.artifact_bytes(model, artifact).decode("utf-8")
            self.assertNotIn("never-export-this", output)
            self.assertNotIn("another-secret", output)

    def test_html_embedded_data_cannot_close_script(self):
        data = fixture()
        payload = '</script><img src=x onerror="alert(1)">'
        data["product"]["title"] = payload
        data["reviews"][0]["text"] = payload
        model = reporting.build_report_model(**data)
        output = reporting.render_html(model)
        self.assertNotIn(payload, output)
        self.assertIn("\\u003c/script\\u003e", output)
        self.assertNotIn("https://cdn", output)
        self.assertNotIn("fetch(", output)

    def test_xlsx_reopens_and_user_text_is_never_a_formula(self):
        from openpyxl import load_workbook
        data = fixture()
        data["reviews"][0]["text"] = '=HYPERLINK("http://invalid","click")'
        model = reporting.build_report_model(**data)
        payload = reporting.generate_xlsx_bytes(model)
        wb = load_workbook(io.BytesIO(payload), data_only=False)
        self.assertEqual(wb.sheetnames, ["风险概览", "差评明细", "宣传与落差", "商品宣传信息"])
        ws = wb["差评明细"]
        self.assertEqual(ws["A4"].value, "001")
        self.assertEqual(ws["E4"].data_type, "s")
        self.assertEqual(ws["E4"].value, data["reviews"][0]["text"])
        self.assertEqual(ws.freeze_panes, "B4")
        self.assertTrue(ws.tables)
        risk = wb["风险概览"]
        self.assertEqual(risk["A3"].value, "方面")
        self.assertEqual(risk["B3"].value, "宣传矛盾指数")
        self.assertEqual(risk["E3"].value, "明确矛盾数")
        self.assertEqual(risk["E4"].value, 3)
        self.assertEqual(risk["F4"].value, 0)
        self.assertIn(risk["C4"].value, {"高风险指数", "中风险指数", "低风险指数"})
        self.assertEqual(risk["J3"].value, "负面提及数")
        self.assertEqual(risk["K4"].number_format, "0.00%")
        self.assertEqual(risk["L4"].number_format, "0.00%")
        self.assertEqual(risk["M3"].value, "购物提醒")
        self.assertEqual(risk["O4"].value, 1)

    def test_pdf_is_readable_and_contains_chinese_and_counts(self):
        from pypdf import PdfReader
        model = reporting.build_report_model(**fixture())
        data = reporting.generate_pdf_bytes(model)
        pdf = PdfReader(io.BytesIO(data))
        self.assertGreaterEqual(len(pdf.pages), 1)
        self.assertLessEqual(len(pdf.pages), 3)
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        self.assertIn("商品宣传与低星体验分析", text)
        self.assertIn("使用舒适性", text)
        self.assertIn("不是全体买家", text)
        self.assertIn("明确矛盾", text)

    def test_bytes_export_does_not_write_to_disk(self):
        model = reporting.build_report_model(**fixture())
        with patch.object(Path, "write_bytes", side_effect=AssertionError("unexpected write")), \
             patch.object(Path, "write_text", side_effect=AssertionError("unexpected write")):
            for artifact in reporting.artifact_catalog(model):
                self.assertIsInstance(reporting.artifact_bytes(model, artifact["id"]), bytes)

    def test_empty_report_and_unknown_artifact(self):
        model = reporting.build_report_model()
        self.assertEqual(model["overview"]["valid_review_count"], 0)
        self.assertEqual(model["sampling"]["scanned_review_count"], 0)
        self.assertIsNone(model["overview"]["gap_evidence_rate"])
        self.assertIsInstance(model["overview"]["evidence_distribution"], list)
        self.assertIsInstance(reporting.generate_pdf_bytes(model), bytes)
        self.assertIsInstance(reporting.generate_xlsx_bytes(model), bytes)
        with self.assertRaises(KeyError):
            reporting.artifact_bytes(model, "unknown")
        with self.assertRaises(KeyError):
            reporting.render_preview_html(model, "unknown")

    def test_table_preview_escapes_comment_html(self):
        data = fixture()
        data["reviews"][0]["text"] = "<script>bad</script>"
        output = reporting.render_preview_html(reporting.build_report_model(**data), "reviews_xlsx")
        self.assertNotIn("<script>bad</script>", output)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", output)

    def test_product_preserves_raw_promotion_without_secret_or_local_paths(self):
        data = fixture()
        data["product"]["promotional_claims"] = [{"text": "<原始OCR温和>", "source": "image_ocr", "confidence": 0.97,
            "source_image": "C:/private/user/sku/main_01.jpg", "api_key": "raw-secret", "local_path": "C:/secret"}]
        data["product"]["images"] = [{"url": "https://images.example/item.jpg", "kind": "main", "local_path": "C:/private/image.jpg"},
                                     {"url": "file:///C:/private/image.jpg", "kind": "main"}]
        model = reporting.build_report_model(**data)
        raw = model["product"]["promotional_claims"][0]
        self.assertEqual(raw["text"], "<原始OCR温和>")
        self.assertEqual(raw["source_image"], "main_01.jpg")
        self.assertEqual(len(model["product"]["images"]), 1)
        output = reporting.artifact_bytes(model, "product_json").decode("utf-8")
        self.assertNotIn("C:/private", output)
        self.assertNotIn("raw-secret", output)
        preview = reporting.render_preview_html(model, "product_json")
        self.assertIn("原始宣传文本（1 条）", preview)
        self.assertIn("&lt;原始OCR温和&gt;", preview)
        self.assertIn("植物", preview)

    def test_html_has_ordered_decisions_and_separate_ad_extra_sections(self):
        """采集概况先于总览，宣传与额外问题独立展示，方法说明按需展开。"""
        output = reporting.render_html(reporting.build_report_model(**fixture()))
        for token in ("综合关注指数", "主要矛盾维度", "宣传对照与评论依据",
                      "额外体验问题", "查看估计依据", "sampleDenominator", "prefers-reduced-motion"):
            self.assertIn(token, output)
        self.assertLess(output.index('id="metrics"'), output.index('id="issueEvidencePanel"'))
        self.assertLess(output.index('id="advertisedSection"'), output.index('id="extraSection"'))
        self.assertNotIn("宣传矛盾指数分档", output)
        self.assertNotIn("整体宣传落差风险：低", output)
        self.assertNotIn("风险概率分布", output)
        self.assertNotIn("购买者遇到问题的概率是", output)
        self.assertNotIn("https://cdn", output)
        self.assertNotIn("fetch(", output)


class EvidenceStatisticsTests(unittest.TestCase):
    def test_wilson_matches_documented_example(self):
        interval = reporting._wilson_interval(1, 500)
        self.assertAlmostEqual(interval[0], 0.000353, places=6)
        self.assertAlmostEqual(interval[1], 0.011241, places=6)

    def test_documented_case_before_matching_is_not_evaluated(self):
        model = document_case()
        sampling = model["sampling"]
        self.assertEqual(sampling["scanned_review_count"], 500)
        self.assertEqual(sampling["low_star_review_count"], 6)
        self.assertEqual(sampling["effective_negative_review_count"], 1)
        self.assertAlmostEqual(sampling["low_star_rate"], 0.012)
        self.assertAlmostEqual(sampling["effective_low_star_rate"], 0.166667, places=5)
        aspect = model["aspects"][0]
        self.assertEqual(aspect["evidence_status"], "not_evaluated")
        self.assertIsNone(aspect["gap_evidence_rate"])
        self.assertEqual(aspect["negative_count"], 1)
        self.assertAlmostEqual(aspect["negative_mention_rate"], 0.002)
        self.assertAlmostEqual(aspect["negative_concentration"], 1.0)
        self.assertEqual(model["overview"]["gap_evidence_count"], 0)
        self.assertIsNone(model["overview"]["gap_evidence_rate"])

    def test_documented_case_after_matching_reports_zero_point_two_percent(self):
        model = document_case(gap_type="potential_conflict")
        aspect = model["aspects"][0]
        self.assertEqual(aspect["evidence_status"], "confirmed")
        self.assertEqual(aspect["contradiction_count"], 1)
        self.assertAlmostEqual(aspect["gap_evidence_rate"], 0.002)
        self.assertEqual(aspect["gap_evidence_ci"], [0.000353, 0.011241])
        self.assertAlmostEqual(model["overview"]["gap_evidence_rate"], 0.002)
        self.assertEqual(model["overview"]["gap_evidence_count"], 1)

    def test_collection_scan_count_is_the_denominator(self):
        collection = {"scanned_count": 500, "matched_count": 6, "is_complete": False,
                      "stop_reason": "达到扫描上限 500 条", "api_total": 10000}
        model = document_case(gap_type="potential_conflict", collection=collection, n_high=0)
        self.assertEqual(model["sampling"]["scanned_review_count"], 500)
        self.assertEqual(model["sampling"]["platform_total"], 10000)
        self.assertAlmostEqual(model["aspects"][0]["gap_evidence_rate"], 0.002)
        self.assertIn("500", model["sampling"]["coverage_note"])

    def test_possible_contradiction_is_not_counted_as_confirmed(self):
        model = document_case(gap_type="partial_gap", n_low=10, n_high=0, valid_low=10, evidence=2)
        aspect = model["aspects"][0]
        self.assertEqual(aspect["evidence_status"], "possible")
        self.assertEqual(aspect["contradiction_count"], 0)
        self.assertEqual(aspect["possible_count"], 2)
        self.assertEqual(aspect["gap_evidence_rate"], 0)
        self.assertAlmostEqual(aspect["possible_evidence_rate"], 0.2)
        self.assertEqual(model["overview"]["gap_evidence_count"], 0)

    def test_no_conflict_is_zero_with_upper_bound_not_missing(self):
        model = document_case(gap_type="no_direct_conflict_found", n_low=10, n_high=0,
                              valid_low=10, evidence=0)
        aspect = model["aspects"][0]
        self.assertEqual(aspect["evidence_status"], "none")
        self.assertEqual(aspect["gap_evidence_rate"], 0)
        self.assertEqual(aspect["gap_evidence_ci"][0], 0)
        self.assertGreater(aspect["gap_evidence_ci"][1], 0)

    def test_unmatched_aspect_keeps_negative_rate_without_fake_zero_gap(self):
        model = document_case(n_low=10, n_high=90, valid_low=10)
        aspect = model["aspects"][0]
        self.assertEqual(aspect["evidence_status"], "not_evaluated")
        self.assertIsNone(aspect["gap_evidence_rate"])
        self.assertAlmostEqual(aspect["negative_mention_rate"], 0.1)

    def test_distribution_only_counts_evaluated_aspects(self):
        model = reporting.build_report_model(**fixture())
        distribution = model["overview"]["evidence_distribution"]
        self.assertEqual(sum(item["aspect_count"] for item in distribution),
                         model["overview"]["evaluated_aspect_count"])
        shares = [item["share"] for item in distribution if item["aspect_count"]]
        self.assertAlmostEqual(sum(shares), 1.0, places=4)


if __name__ == "__main__":
    unittest.main()
