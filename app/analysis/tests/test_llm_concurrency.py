import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import requests

from analysis.llm_client import ChatClient, LLMConfig


def response(status=200):
    result = Mock(status_code=status, text="format unsupported", headers={})
    result.json.return_value = {
        "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}], "usage": {}}
    return result


class LLMConcurrencyTest(unittest.TestCase):
    def client(self, retries=1):
        return ChatClient(LLMConfig(api_key="offline-test-only", model="offline-test", max_retries=retries))

    def test_each_inflight_json_payload_performs_its_own_fallback(self):
        client = self.client()
        both_started, fallback_started = threading.Barrier(2), threading.Event()
        seen, lock = [], threading.Lock()

        def post(payload):
            name = payload["messages"][1]["content"]
            has_format = "response_format" in payload
            with lock:
                seen.append((name, has_format))
            if has_format:
                both_started.wait(timeout=3)
                if name == "B":
                    self.assertTrue(fallback_started.wait(timeout=3))
                return response(400), 1
            fallback_started.set()
            return response(), 1

        with patch.object(client, "_post", side_effect=post):
            with ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(lambda name: client.chat_json("test", name).content, ["A", "B"]))
            self.assertEqual(values, ['{"ok":true}', '{"ok":true}'])
            self.assertEqual(client.chat_json("test", "later").content, '{"ok":true}')
        self.assertEqual(set(seen), {("A", True), ("A", False), ("B", True), ("B", False), ("later", False)})

    def test_network_error_during_format_fallback_uses_normal_retry(self):
        client = self.client(retries=2)
        formats = []

        def post(payload):
            formats.append("response_format" in payload)
            if len(formats) == 1:
                return response(400), 1
            if len(formats) == 2:
                raise requests.Timeout("offline simulated timeout")
            return response(), 1

        with patch.object(client, "_post", side_effect=post), patch("analysis.llm_client.time.sleep"):
            result = client.chat_json("test", "input")
        self.assertEqual(result.content, '{"ok":true}')
        self.assertEqual(result.attempts, 2)
        self.assertEqual(formats, [True, False, False])

    def test_concurrent_workers_have_independent_reusable_sessions(self):
        with patch("analysis.llm_client.requests.Session", side_effect=lambda: Mock()) as factory:
            client = self.client()
            ready = threading.Barrier(3)

            def work(_value):
                first = client._thread_session()
                ready.wait(timeout=3)
                return first, client._thread_session()

            with ThreadPoolExecutor(max_workers=3) as pool:
                sessions = list(pool.map(work, range(3)))
        self.assertEqual(len({id(first) for first, _ in sessions}), 3)
        self.assertTrue(all(first is second for first, second in sessions))
        self.assertEqual(factory.call_count, 4)  # compatibility session plus three workers


if __name__ == "__main__":
    unittest.main()
