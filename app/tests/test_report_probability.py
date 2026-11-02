"""Probability meaning and counts survive report/export integration."""
import copy
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reporting
from test_issue_evidence import case


def report_case():
    data = case()
    return dict(summary={**data["summary"], "product_id": data["product_id"], "analysis_status": "completed"},
                reviews=data["reviews"], statistics={"clusters": data["clusters"]}, issues=data["issues"],
                collection=data["collection"],
                extraction={"reviews": [{"review_id": r["review_id"], "status": "ok", "errors": [], "content": r["content"]}
                                         for r in data["reviews"]]})


class ReportProbabilityTest(unittest.TestCase):
    def test_public_json_preserves_probability_definition_and_no_purchasing_probability(self):
        args = report_case()
        before = copy.deepcopy(args)
        model = reporting.build_report_model(**args)
        value = model["issue_evidence"]["windows"]["30"]["overall"]
        self.assertAlmostEqual(value["probability_estimate"]["estimate"], .9)
        self.assertIsNone(value["purchase_probability"])
        self.assertEqual(args, before)
        exported = json.loads(reporting.artifact_bytes(model, "reviews_json"))
        self.assertEqual(exported["issue_evidence"], model["issue_evidence"])
        self.assertFalse(exported["method"]["probability_calibrated"])

    def test_pdf_states_conditional_probability_interval_and_sample_counts(self):
        from pypdf import PdfReader
        model = reporting.build_report_model(**report_case())
        pdf = PdfReader(io.BytesIO(reporting.generate_pdf_bytes(model)))
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        for expected in ("同类问题复现概率", "90.0%", "95%模型可信区间", "4/4", "不是购买后故障率", "样本少"):
            self.assertIn(expected, text)
        self.assertLessEqual(len(pdf.pages), 3)

    def test_xlsx_probability_is_numeric_and_k_n_are_exported(self):
        from openpyxl import load_workbook
        model = reporting.build_report_model(**report_case())
        wb = load_workbook(io.BytesIO(reporting.generate_xlsx_bytes(model)))
        ws = wb["风险概览"]
        data_rows = [row for row in ws.iter_rows() if row[0].value == model["issue_evidence"]["windows"]["30"]["overall"]["name"]]
        self.assertEqual(len(data_rows), 1)
        row = data_rows[0]
        self.assertAlmostEqual(row[7].value, .9)
        self.assertEqual(row[7].number_format, "0.0%")
        self.assertEqual((row[10].value, row[11].value), (4, 4))

    def test_missing_extraction_does_not_invent_probability_from_repeat_count(self):
        args = report_case()
        args.pop("extraction")
        model = reporting.build_report_model(**args)
        result = model["issue_evidence"]["windows"]["30"]["overall"]
        self.assertEqual(result["repeat_count"], 4)
        self.assertIsNone(result["probability"])
        self.assertFalse(result["probability_estimate"]["coverage"]["extraction_provided"])
        self.assertIn("尚未估计", reporting._probability_text(result))


if __name__ == "__main__":
    unittest.main()
