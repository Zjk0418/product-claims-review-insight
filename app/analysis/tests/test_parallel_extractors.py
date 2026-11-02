import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from analysis.claim_extractor import ClaimConfig, ClaimExtractor
from analysis.gap_analyzer import GapAnalyzer
from analysis.issue_extractor import ExtractConfig, IssueExtractor


def review(rid):
    return {"review_id": rid, "text": "鞋底太硬，走路脚疼", "content": "鞋底太硬，走路脚疼",
            "star": 1, "date": "2026-01-01", "sku_id": "test", "valid_for_analysis": True}


def issue():
    return {"object": "鞋底", "aspect": "穿着舒适性", "manifestation": "鞋底过硬", "direction": "too_hard",
            "severity": "high", "evidence": "鞋底太硬", "basis": "explicit", "confidence": 0.9}


def fake_response(payload):
    return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False),
                           usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, latency_ms=1)


class IssueClient:
    def __init__(self, partial=False):
        self.calls, self.partial = 0, partial
        self.lock = threading.Lock()

    def chat_json(self, _system, user):
        rows = json.loads(user[user.index("{"):])["reviews"]
        with self.lock:
            self.calls += 1
        if rows[0]["review_id"].endswith("1"):
            time.sleep(0.02)
        issues = [issue()]
        if self.partial:
            issues.append({**issue(), "evidence": "这段文字并不存在于评论中"})
        return fake_response({"results": [{"review_id": row["review_id"], "issues": issues} for row in rows]})


class ClaimClient:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def chat_json(self, _system, user):
        rows = json.loads(user[user.index("{"):])["texts"]
        with self.lock:
            self.calls += 1
        return fake_response({"claims": [{
            "text_id": row["text_id"], "aspect": "功能与性能", "claim_type": "functional",
            "normalized_claim": "商品具有" + row["text"][:2] + "性能", "original_text": row["text"][:2],
            "confidence": 0.9} for row in rows]})


class ParallelExtractorsTest(unittest.TestCase):
    def test_mock_waiting_batches_have_identical_results_at_one_and_six_workers(self):
        class WaitingClient(IssueClient):
            def chat_json(self, system, user):
                time.sleep(0.03)  # offline simulated network wait; never contacts a provider
                return super().chat_json(system, user)

        reviews = [review(f"r{n}") for n in range(1, 13)]
        payloads, timings, calls = {}, {}, {}
        with tempfile.TemporaryDirectory() as tmp:
            for workers in (1, 6):
                folder = Path(tmp) / str(workers)
                folder.mkdir()
                client = WaitingClient()
                with patch.dict(os.environ, {"LLM_CONCURRENCY": str(workers)}):
                    started = time.perf_counter()
                    payloads[workers] = IssueExtractor(client, "offline", folder, ExtractConfig(batch_size=1)).run(reviews)
                    timings[workers] = round(time.perf_counter() - started, 4)
                calls[workers] = client.calls
        self.assertEqual(payloads[1]["reviews"], payloads[6]["reviews"])
        self.assertEqual(payloads[1]["usage"], payloads[6]["usage"])
        self.assertEqual(calls, {1: 12, 6: 12})
        # Timings are a diagnostic, never a flaky correctness threshold or a
        # claimed speedup for a real provider's rate limits and latency.
        print("OFFLINE_MOCK_BENCHMARK " + json.dumps({"batch_count": 12, "mock_wait_ms": 30,
              "one_worker_seconds": timings[1], "six_workers_seconds": timings[6],
              "contents_and_order_identical": True, "real_api_calls": 0}))

    def test_gap_progress_remains_monotonic_with_a_slow_callback(self):
        claims = [{"claim_id": f"PC{n}", "normalized_claim": "商品具有防水性能", "original_text": "防水",
                   "aspect": "功能与性能", "claim_type": "functional", "source": "title"} for n in range(8)]
        candidate = {"cluster_id": "C1", "aspect": "功能与性能", "review_count": 1, "review_ids": ["r1"]}
        result = {"gap_type": "insufficient_evidence", "related_cluster_ids": [], "evidence_review_ids": [],
                  "finding": "离线测试没有足够依据", "reasoning": "offline"}
        progress = []

        def completed(done, total):
            if done == 1:
                time.sleep(0.025)
            progress.append((done, total))

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LLM_CONCURRENCY": "6"}):
            analyzer = GapAnalyzer(Mock(), "offline", Path(tmp))
            with patch.object(analyzer, "_rule_only_gap", return_value=None), patch.object(
                analyzer, "_call_llm", return_value=result
            ), patch("analysis.gap_analyzer.claim_candidates", return_value=[candidate]):
                payload = analyzer.analyze(claims, [candidate], {"valid_review_count": 1}, on_progress=completed)
        self.assertEqual(progress, [(n, 8) for n in range(1, 9)])
        self.assertEqual([gap["claim_id"] for gap in payload["gaps"]], [claim["claim_id"] for claim in claims])

    def test_issue_parallel_output_matches_serial_and_progress_follows_durable_cache(self):
        reviews = [review(f"r{n}") for n in range(1, 9)]
        with tempfile.TemporaryDirectory() as tmp:
            serial_dir, parallel_dir = Path(tmp) / "serial", Path(tmp) / "parallel"
            serial_dir.mkdir()
            parallel_dir.mkdir()
            serial_client, parallel_client = IssueClient(), IssueClient()
            with patch.dict(os.environ, {"LLM_CONCURRENCY": "1"}):
                serial = IssueExtractor(serial_client, "offline", serial_dir, ExtractConfig(batch_size=1)).run(reviews)
            extractor = IssueExtractor(parallel_client, "offline", parallel_dir, ExtractConfig(batch_size=1))
            progress = []

            def completed(done, total):
                with extractor._io_lock:
                    rows = [json.loads(line) for line in extractor.cache_path.read_text(encoding="utf-8").splitlines()]
                progress.append((done, total, len(rows)))

            with patch.dict(os.environ, {"LLM_CONCURRENCY": "6"}):
                parallel = extractor.run(reviews, on_progress=completed)
            self.assertEqual(parallel["reviews"], serial["reviews"])
            self.assertEqual(parallel["usage"], serial["usage"])
            self.assertEqual(serial_client.calls, parallel_client.calls)
            self.assertEqual([done for done, _, _ in progress], list(range(1, 9)))
            self.assertTrue(all(total == 8 and durable >= done for done, total, durable in progress))

    def test_completed_issue_batch_survives_a_later_worker_exception(self):
        done = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            extractor = IssueExtractor(Mock(), "offline", Path(tmp), ExtractConfig(batch_size=1))

            def batch(rows):
                rid = rows[0]["review_id"]
                if rid == "r2":
                    if not done.wait(timeout=3):
                        raise TimeoutError("first batch did not finish")
                    raise RuntimeError("later worker failed")
                return {rid: []}, {}, [], {}

            with patch.object(extractor, "_call_batch", side_effect=batch), patch.dict(os.environ, {"LLM_CONCURRENCY": "2"}):
                with self.assertRaisesRegex(RuntimeError, "later worker failed"):
                    extractor.run([review("r1"), review("r2")], on_progress=lambda *_args: done.set())
            cached = [json.loads(line) for line in extractor.cache_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(row["review_id"], row["status"]) for row in cached], [("r1", "empty")])
            never_called = Mock()
            resumed = IssueExtractor(never_called, "offline", Path(tmp), ExtractConfig(batch_size=1)).run([review("r1")])
            self.assertEqual(resumed["reviews"][0]["status"], "cached")
            never_called.chat_json.assert_not_called()

    def test_partial_issue_validation_is_not_treated_as_a_complete_cache_hit(self):
        client = IssueClient(partial=True)
        with tempfile.TemporaryDirectory() as tmp:
            first = IssueExtractor(client, "offline", Path(tmp)).run([review("r1")])
            self.assertEqual(first["reviews"][0]["status"], "partial_validation")
            path = Path(tmp) / "llm_cache.jsonl"
            legacy = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            legacy["status"] = "ok"  # old versions mislabeled partially valid rows
            path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")
            second = IssueExtractor(client, "offline", Path(tmp)).run([review("r1")])
            self.assertEqual(second["reviews"][0]["status"], "partial_validation")
            self.assertEqual(client.calls, 2)

    def test_invalid_cached_evidence_is_reprocessed_instead_of_silently_becoming_empty(self):
        client = IssueClient()
        with tempfile.TemporaryDirectory() as tmp:
            IssueExtractor(client, "offline", Path(tmp)).run([review("r1")])
            path = Path(tmp) / "llm_cache.jsonl"
            cached = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            cached["issues"][0]["evidence"] = "并不存在的原文"
            path.write_text(json.dumps(cached, ensure_ascii=False) + "\n", encoding="utf-8")
            result = IssueExtractor(client, "offline", Path(tmp)).run([review("r1")])
            self.assertEqual(result["reviews"][0]["status"], "ok")
            self.assertEqual(result["issue_count"], 1)
            self.assertEqual(client.calls, 2)

    def test_mixed_cached_claim_batches_preserve_original_tie_order(self):
        texts = [{"text_id": "T1", "text": "防水耐用", "source": "title", "confidence": 1.0, "image": ""},
                 {"text_id": "T2", "text": "防滑轻盈", "source": "title", "confidence": 1.0, "image": ""}]
        with tempfile.TemporaryDirectory() as tmp:
            fresh_dir, mixed_dir = Path(tmp) / "fresh", Path(tmp) / "mixed"
            fresh_dir.mkdir()
            mixed_dir.mkdir()
            fresh = ClaimExtractor(ClaimClient(), "offline", fresh_dir, ClaimConfig(batch_size=1)).run(texts)
            client = ClaimClient()
            ClaimExtractor(client, "offline", mixed_dir, ClaimConfig(batch_size=1)).run([texts[1]])
            mixed = ClaimExtractor(client, "offline", mixed_dir, ClaimConfig(batch_size=1)).run(texts)
            self.assertEqual([row["text_id"] for row in fresh["claims"]], ["T1", "T2"])
            self.assertEqual(mixed["claims"], fresh["claims"])
            self.assertEqual(client.calls, 2)  # one cached T2 call, then only the missing T1


if __name__ == "__main__":
    unittest.main()
