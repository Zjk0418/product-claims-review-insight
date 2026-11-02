import time
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from analysis.parallel import MAX_CONCURRENCY, default_workers, ordered_map


class OrderedMapTest(unittest.TestCase):
    def test_preserves_input_order(self):
        def work(value):
            # 让后面的任务先完成，验证结果仍按输入顺序返回
            time.sleep(0.03 if value % 3 == 0 else 0.005)
            return value * 2

        self.assertEqual(ordered_map(work, range(12), workers=4), [value * 2 for value in range(12)])

    def test_single_worker_runs_sequentially(self):
        seen = []

        def work(value):
            seen.append(value)
            return value

        self.assertEqual(ordered_map(work, [3, 1, 2], workers=1), [3, 1, 2])
        self.assertEqual(seen, [3, 1, 2])

    def test_empty_input(self):
        self.assertEqual(ordered_map(lambda value: value, [], workers=4), [])

    def test_worker_configuration_is_bounded(self):
        for value, expected in (("999", MAX_CONCURRENCY), ("0", 1), ("invalid", 6)):
            with self.subTest(value=value), patch.dict(os.environ, {"LLM_CONCURRENCY": value}):
                self.assertEqual(default_workers(), expected)

    def test_explicit_worker_count_cannot_exceed_global_call_limit(self):
        release, ready, lock = threading.Event(), threading.Event(), threading.Lock()
        active, peak = 0, 0

        def work(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == MAX_CONCURRENCY:
                    ready.set()
            try:
                if not release.wait(timeout=3):
                    raise TimeoutError("offline concurrency test timed out")
                return value
            finally:
                with lock:
                    active -= 1

        with ThreadPoolExecutor(max_workers=1) as driver:
            result = driver.submit(ordered_map, work, range(24), MAX_CONCURRENCY * 3)
            try:
                self.assertTrue(ready.wait(timeout=3))
                time.sleep(0.03)
            finally:
                release.set()
            self.assertEqual(result.result(timeout=3), list(range(24)))
        self.assertLessEqual(peak, MAX_CONCURRENCY)
        self.assertGreater(peak, 1)

    def test_worker_error_is_not_silently_dropped(self):
        def work(value):
            if value == 2:
                raise RuntimeError("offline batch failure")
            return value
        with self.assertRaisesRegex(RuntimeError, "offline batch failure"):
            ordered_map(work, range(5), workers=3)
