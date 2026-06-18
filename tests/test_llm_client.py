from __future__ import annotations

import json
import urllib.error
import unittest

from agent_engine import telemetry
from agent_engine.llm_client import ChatClient


class FlakyChatClient(ChatClient):
    def __init__(self) -> None:
        super().__init__("http://model.test/v1", "test-model")
        self.max_retries = 1
        self.backoff_seconds = 0
        self.calls = 0

    def _request_raw(self, _req):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise urllib.error.URLError("temporary network failure")
        return json.dumps(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            }
        )


class CircuitChatClient(ChatClient):
    def __init__(self) -> None:
        super().__init__("http://circuit.test/v1", "test-model")
        self.max_retries = 0
        self.circuit_threshold = 1
        self.circuit_cooldown_seconds = 60

    def _request_raw(self, _req):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("down")


class ChatClientTests(unittest.TestCase):
    def setUp(self) -> None:
        ChatClient._circuit_state.clear()
        telemetry.reset_token_usage()

    def test_chat_retries_transient_errors_and_records_usage(self) -> None:
        client = FlakyChatClient()

        text = client.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(text, "ok")
        self.assertEqual(client.calls, 2)
        self.assertEqual(telemetry.get_token_usage(), 5)

    def test_circuit_opens_after_repeated_failures(self) -> None:
        client = CircuitChatClient()

        with self.assertRaises(urllib.error.URLError):
            client.chat([{"role": "user", "content": "hello"}])
        with self.assertRaises(RuntimeError):
            client.chat([{"role": "user", "content": "hello"}])


if __name__ == "__main__":
    unittest.main()
