import json
import tempfile
import unittest
from pathlib import Path

from analysis.issue_extractor import ExtractConfig, IssueExtractor


class FakeResponse:
    def __init__(self, content: str):
        self.content = content
        self.usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        self.latency_ms = 12


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat_json(self, system: str, user: str):
        self.calls += 1
        content = self.responses.pop(0)
        if callable(content):
            content = content(user)
        return FakeResponse(content)


def review(review_id: str, text: str) -> dict:
    return {
        "review_id": review_id,
        "text": text,
        "content": text,
        "star": 1,
        "date": "2026-01-01",
        "sku_id": "sku-1",
        "valid_for_analysis": True,
    }


class IssueExtractorTest(unittest.TestCase):
    def test_invalid_evidence_is_excluded(self):
        reviews = [review("r1", "鞋底太硬，走路脚疼"), review("r2", "鞋头太小挤脚")]
        response = json.dumps(
            {
                "results": [
                    {
                        "review_id": "r1",
                        "issues": [
                            {
                                "object": "鞋底",
                                "aspect": "穿着舒适性",
                                "manifestation": "鞋底过硬",
                                "direction": "too_hard",
                                "severity": "high",
                                "evidence": "鞋底太硬",
                                "basis": "explicit",
                                "confidence": 0.9,
                            }
                        ],
                    },
                    {
                        "review_id": "r2",
                        "issues": [
                            {
                                "object": "鞋头",
                                "aspect": "尺码与鞋型",
                                "manifestation": "鞋头偏窄",
                                "direction": "too_narrow",
                                "severity": "medium",
                                "evidence": "鞋头很窄",  # 原文不存在
                                "basis": "explicit",
                                "confidence": 0.8,
                            }
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            extractor = IssueExtractor(
                FakeClient([response]), "fake-model", Path(tmp), ExtractConfig(batch_size=5)
            )
            result = extractor.run(reviews)
            self.assertEqual(result["issue_count"], 1)
            self.assertEqual(result["validation_failed_count"], 1)
            by_id = {row["review_id"]: row for row in result["reviews"]}
            self.assertEqual(by_id["r1"]["status"], "ok")
            self.assertEqual(by_id["r2"]["status"], "validation_failed")
            self.assertTrue(by_id["r2"]["errors"])
            self.assertTrue((Path(tmp) / "validation_errors.jsonl").exists())

    def test_cache_avoids_second_call(self):
        reviews = [review("r1", "鞋底太硬，走路脚疼")]
        response = json.dumps(
            {
                "results": [
                    {
                        "review_id": "r1",
                        "issues": [
                            {
                                "object": "鞋底",
                                "aspect": "穿着舒适性",
                                "manifestation": "鞋底过硬",
                                "direction": "too_hard",
                                "severity": "high",
                                "evidence": "鞋底太硬",
                                "basis": "explicit",
                                "confidence": 0.9,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient([response])
            extractor = IssueExtractor(client, "fake-model", Path(tmp))
            first = extractor.run(reviews)
            self.assertEqual(first["issue_count"], 1)
            self.assertEqual(client.calls, 1)

            second_client = FakeClient([])
            second = IssueExtractor(second_client, "fake-model", Path(tmp)).run(reviews)
            self.assertEqual(second_client.calls, 0)
            self.assertEqual(second["reviews"][0]["status"], "cached")
            self.assertEqual(second["issue_count"], 1)

    def test_invalid_json_retries(self):
        reviews = [review("r1", "尺码偏大，不跟脚")]
        good = json.dumps(
            {
                "results": [
                    {
                        "review_id": "r1",
                        "issues": [
                            {
                                "object": "尺码",
                                "aspect": "尺码与鞋型",
                                "manifestation": "尺码偏大",
                                "direction": "too_large",
                                "severity": "medium",
                                "evidence": "尺码偏大",
                                "basis": "explicit",
                                "confidence": 0.85,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(["这不是 JSON", good])
            result = IssueExtractor(client, "fake-model", Path(tmp)).run(reviews)
            self.assertEqual(client.calls, 2)
            self.assertEqual(result["issue_count"], 1)


if __name__ == "__main__":
    unittest.main()
