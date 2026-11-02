import json
import tempfile
import types
import unittest
from pathlib import Path

from analysis.claim_extractor import ClaimConfig, ClaimExtractor, build_raw_texts


class FakeClient:
    def __init__(self, payload, broken_first=False):
        self.payload = payload
        self.calls = 0
        self.broken_first = broken_first

    def chat_json(self, system, user):
        self.calls += 1
        if self.broken_first and self.calls == 1:
            content = "not json"
        else:
            raw = user[user.find("{") :]
            text_ids = [item["text_id"] for item in json.loads(raw)["texts"]]
            payload = json.loads(json.dumps(self.payload))
            payload["claims"] = [
                item for item in payload["claims"] if item["text_id"] in text_ids
            ]
            content = json.dumps(payload, ensure_ascii=False)
        return types.SimpleNamespace(content=content, usage={}, latency_ms=10)


class ClaimExtractorTest(unittest.TestCase):
    def test_build_raw_texts_consolidates_sizes_and_colors(self):
        export = {
            "product": {
                "title": "透气网面运动鞋",
                "price_text": "169-179",
                "attributes": {
                    "尺码": ["39 【标准运动码】现货秒发", "40 【标准运动码】现货秒发"],
                    "颜色分类": ["米色.", "白色.", "米色.【旗舰款】"],
                },
            },
            "promotional_claims": [
                {"text": "透气网面运动鞋", "source": "title", "confidence": 1.0},
                {"text": "防滑耐磨", "source": "image_ocr", "confidence": 0.99, "image": "a.jpg"},
            ],
        }
        texts = build_raw_texts(export)
        joined = "\n".join(item["text"] for item in texts)
        self.assertIn("尺码：39、40；标准运动码", joined)
        self.assertIn("颜色分类：米色、白色", joined)
        self.assertEqual(sum(1 for item in texts if "透气网面运动鞋" in item["text"]), 1)
        self.assertTrue(all(item["text_id"].startswith("T") for item in texts))

    def test_run_and_cache(self):
        export = {
            "product": {"title": "防滑耐磨减震透气"},
            "promotional_claims": [
                {"text": "防滑耐磨减震透气", "source": "title", "confidence": 1.0}
            ],
        }
        texts = build_raw_texts(export)
        text_id = texts[0]["text_id"]
        payload = {
            "claims": [
                {
                    "text_id": text_id,
                    "aspect": "功能与性能",
                    "claim_type": "functional",
                    "normalized_claim": "商品具有防滑性能",
                    "original_text": "防滑",
                    "confidence": 0.95,
                },
                {
                    "text_id": text_id,
                    "aspect": "功能与性能",
                    "claim_type": "functional",
                    "normalized_claim": "商品具有减震性能",
                    "original_text": "减震",
                    "confidence": 0.9,
                },
            ]
        }
        client = FakeClient(payload)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = ClaimExtractor(client, "fake-model", out, ClaimConfig(batch_size=1)).run(texts)
            self.assertEqual(result["claim_count"], 2)
            self.assertEqual(client.calls, 1)
            # 第二轮应命中缓存，不再调用
            result2 = ClaimExtractor(client, "fake-model", out, ClaimConfig(batch_size=1)).run(texts)
            self.assertEqual(result2["claim_count"], 2)
            self.assertEqual(client.calls, 1)

    def test_invalid_original_is_rejected(self):
        export = {
            "product": {"title": "防滑"},
            "promotional_claims": [{"text": "防滑", "source": "title", "confidence": 1.0}],
        }
        texts = build_raw_texts(export)
        text_id = texts[0]["text_id"]
        payload = {
            "claims": [
                {
                    "text_id": text_id,
                    "aspect": "功能与性能",
                    "claim_type": "functional",
                    "normalized_claim": "商品具有防水性能",
                    "original_text": "防水",
                }
            ]
        }
        client = FakeClient(payload)
        with tempfile.TemporaryDirectory() as tmp:
            result = ClaimExtractor(
                client, "fake-model", Path(tmp), ClaimConfig(batch_size=1, repair_retries=0)
            ).run(texts)
            self.assertEqual(result["claim_count"], 0)
            self.assertTrue(any("无法在输入文本中定位" in error for error in result["errors"]))

    def test_json_retry(self):
        export = {
            "product": {"title": "防滑"},
            "promotional_claims": [{"text": "防滑", "source": "title", "confidence": 1.0}],
        }
        texts = build_raw_texts(export)
        text_id = texts[0]["text_id"]
        payload = {
            "claims": [
                {
                    "text_id": text_id,
                    "aspect": "功能与性能",
                    "claim_type": "functional",
                    "normalized_claim": "商品具有防滑性能",
                    "original_text": "防滑",
                }
            ]
        }
        client = FakeClient(payload, broken_first=True)
        with tempfile.TemporaryDirectory() as tmp:
            result = ClaimExtractor(
                client, "fake-model", Path(tmp), ClaimConfig(batch_size=1, repair_retries=1)
            ).run(texts)
            self.assertEqual(result["claim_count"], 1)
            self.assertEqual(client.calls, 2)

    def test_empty_promo_texts(self):
        export = {"product": {"title": "", "attributes": {}}, "promotional_claims": []}
        texts = build_raw_texts(export)
        self.assertEqual(texts, [])
        client = FakeClient({"claims": []})
        with tempfile.TemporaryDirectory() as tmp:
            result = ClaimExtractor(client, "fake-model", Path(tmp)).run(texts)
            self.assertEqual(result["claim_count"], 0)
            self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()


class ClaimFilterAndParallelTest(unittest.TestCase):
    def test_non_actionable_claims_are_filtered_out(self):
        export = {
            "product": {"title": "持久留香30小时 美丽自信"},
            "promotional_claims": [
                {"text": "持久留香30小时 美丽自信", "source": "title", "confidence": 1.0}
            ],
        }
        texts = build_raw_texts(export)
        text_id = texts[0]["text_id"]
        payload = {
            "claims": [
                {
                    "text_id": text_id,
                    "aspect": "功效与效果",
                    "claim_type": "functional",
                    "normalized_claim": "持久留香30小时",
                    "original_text": "持久留香30小时",
                },
                {
                    "text_id": text_id,
                    "aspect": "其他",
                    "claim_type": "marketing_slogan",
                    "normalized_claim": "美丽自信",
                    "original_text": "美丽自信",
                },
            ]
        }
        client = FakeClient(payload)
        with tempfile.TemporaryDirectory() as tmp:
            result = ClaimExtractor(client, "fake-model", Path(tmp), ClaimConfig(batch_size=1)).run(texts)
        self.assertEqual(result["claim_count"], 1)
        self.assertEqual(result["claims"][0]["normalized_claim"], "持久留香30小时")
        self.assertEqual(result["filtered"]["count"], 1)
        self.assertEqual(sum(result["filtered"]["reasons"].values()), 1)

    def test_parallel_batches_keep_every_claim_and_order(self):
        export = {
            "product": {"title": "标题"},
            "promotional_claims": [
                {"text": f"卖点{i}", "source": "title", "confidence": 1.0} for i in range(1, 9)
            ],
        }
        texts = build_raw_texts(export)

        class PerTextClient:
            def __init__(self):
                self.calls = 0

            def chat_json(self, system, user):
                self.calls += 1
                raw = user[user.find("{"):]
                items = json.loads(raw)["texts"]
                claims = [
                    {
                        "text_id": item["text_id"],
                        "aspect": "功能与性能",
                        "claim_type": "functional",
                        "normalized_claim": f"具备{item['text']}效果",
                        "original_text": item["text"],
                    }
                    for item in items
                ]
                return types.SimpleNamespace(
                    content=json.dumps({"claims": claims}, ensure_ascii=False),
                    usage={},
                    latency_ms=10,
                )

        client = PerTextClient()
        with tempfile.TemporaryDirectory() as tmp:
            result = ClaimExtractor(
                client, "fake-model", Path(tmp), ClaimConfig(batch_size=1)
            ).run(texts)
        self.assertEqual(result["claim_count"], len(texts))
        self.assertEqual(client.calls, len(texts))
        self.assertEqual(
            sorted(item["original_text"] for item in result["claims"]),
            sorted(item["text"] for item in texts),
            "并发不能丢失任何主张",
        )
        # 并发批次的结果在合并时按输入顺序拼接，重复运行顺序一致。
        with tempfile.TemporaryDirectory() as tmp2:
            again = ClaimExtractor(
                PerTextClient(), "fake-model", Path(tmp2), ClaimConfig(batch_size=1)
            ).run(texts)
        self.assertEqual(
            [item["original_text"] for item in again["claims"]],
            [item["original_text"] for item in result["claims"]],
        )

class ClaimProgressTest(unittest.TestCase):
    def _payload(self, texts):
        return {
            "claims": [
                {
                    "text_id": item["text_id"],
                    "aspect": "功效与效果",
                    "claim_type": "functional",
                    "normalized_claim": item["text"],
                    "original_text": item["text"],
                }
                for item in texts
            ]
        }

    def test_on_progress_counts_each_batch(self):
        export = {
            "product": {"title": "标题"},
            "promotional_claims": [
                {"text": f"持久留香{i}小时", "source": "title", "confidence": 1.0}
                for i in range(1, 4)
            ],
        }
        texts = build_raw_texts(export)
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            ClaimExtractor(FakeClient(self._payload(texts)), "fake", Path(tmp),
                           ClaimConfig(batch_size=1)).run(
                texts, on_progress=lambda done, total: seen.append((done, total))
            )
        expected = len(texts)  # 标题 + 3 条卖点，batch_size=1 时每条一批
        self.assertEqual(sorted(done for done, _ in seen), list(range(1, expected + 1)))
        self.assertTrue(all(total == expected for _, total in seen))

    def test_progress_callback_errors_do_not_break_extraction(self):
        export = {"product": {"title": "持久留香30小时"}}
        texts = build_raw_texts(export)

        def boom(_done, _total):
            raise RuntimeError("progress failed")

        with tempfile.TemporaryDirectory() as tmp:
            result = ClaimExtractor(FakeClient(self._payload(texts)), "fake", Path(tmp),
                                    ClaimConfig(batch_size=1)).run(texts, on_progress=boom)
        self.assertEqual(result["claim_count"], 1)
