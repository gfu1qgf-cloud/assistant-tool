"""Groq adapter tests that also run without a Qt installation."""
import json
import os
import unittest
from unittest.mock import patch

from model.AIProviderClient import AIProviderClient, AIProviderError
from model.AIProviderKeys import read_provider_key


class Context:
    def check_canceled(self):
        pass

    def report(self, _text, _percent):
        pass


class Response:
    def __init__(self, payload, *, status=200, headers=None, stream=False):
        self.body = (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status_code = status
        self.headers = headers or {}
        self.stream = stream
        self.closed = False

    def iter_content(self, chunk_size):
        for byte in self.body if self.stream else [self.body]:
            yield bytes([byte]) if self.stream else byte

    def close(self):
        self.closed = True


class GroqTests(unittest.TestCase):
    def setUp(self):
        self.client = AIProviderClient()
        self.context = Context()
        self.key_patch = patch.object(self.client, "_key", return_value="synthetic-groq-key")
        self.online_patch = patch("model.AIProviderClient.require_online")
        self.key_patch.start()
        self.online_patch.start()

    def tearDown(self):
        self.online_patch.stop()
        self.key_patch.stop()

    def test_environment_key(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "synthetic-environment-key"}):
            self.assertEqual(read_provider_key({}, "groq"), "synthetic-environment-key")

    def test_catalog_omits_non_chat_and_identifies_vision(self):
        response = Response({"data": [{"id": "openai/gpt-oss-120b", "active": True},
                                      {"id": "qwen/qwen3.8-27b", "active": True},
                                      {"id": "whisper-large-v3"},
                                      {"id": "canopylabs/orpheus-v1-english"},
                                      {"id": "openai/old", "active": False}]})
        with patch("model.AIProviderClient.requests.request", return_value=response) as request:
            rows = self.client.list_models("groq", {}, self.context)
        self.assertEqual(request.call_args.args[1], "https://api.groq.com/openai/v1/models")
        self.assertEqual([row["id"] for row in rows], ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"])
        self.assertTrue(rows[1]["vision"])
        self.assertEqual([row["id"] for row in self.client.filter_models("groq", rows)],
                         ["openai/gpt-oss-120b"])
        self.assertTrue(response.closed)

    def test_chat_multiturn_and_image_endpoint(self):
        model = "qwen/qwen3.8-27b"
        image = {"mime": "image/png", "data": "YWJj"}
        messages = [{"role": "user", "text": "看", "images": [image]},
                    {"role": "assistant", "text": "是图片", "images": []}]
        response = Response({"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}]})
        rows = [{"id": model, "vision": True, "free": None}]
        with patch.object(self.client, "list_models", return_value=rows), patch(
                "model.AIProviderClient.requests.request", return_value=response) as request:
            result = self.client.chat("groq", model, messages, {"foreign_only": False}, {}, self.context)
        self.assertEqual(result["text"], "好的")
        self.assertEqual(request.call_args.args[1], "https://api.groq.com/openai/v1/chat/completions")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][0]["content"][1]["image_url"],
                         {"url": "data:image/png;base64,YWJj"})
        self.assertEqual(payload["messages"][1]["content"], "是图片")
        self.assertFalse(request.call_args.kwargs["allow_redirects"])

    def test_streaming(self):
        events = (b'data: {"choices":[{"index":0,"delta":{"content":"A"}}]}\n\n'
                  b'data: {"choices":[{"index":0,"delta":{"content":"B"},"finish_reason":"stop"}]}\n\n'
                  b'data: [DONE]\n\n')
        response = Response(events, headers={"Content-Type": "text/event-stream"}, stream=True)
        pieces = []
        rows = [{"id": "openai/gpt-oss-120b", "vision": False, "free": None}]
        with patch.object(self.client, "list_models", return_value=rows), patch(
                "model.AIProviderClient.requests.request", return_value=response):
            result = self.client.chat("groq", rows[0]["id"],
                [{"role": "user", "text": "hi", "images": []}], {"stream": True}, {}, self.context,
                on_delta=pieces.append)
        self.assertEqual((result["text"], pieces), ("AB", ["A", "B"]))
        self.assertTrue(response.closed)

    def test_unsupported_image_and_wrong_origin_block_before_send(self):
        with patch.object(self.client, "list_models", return_value=[]), patch(
                "model.AIProviderClient.requests.request") as request:
            with self.assertRaises(AIProviderError):
                self.client.chat("groq", "openai/gpt-oss-120b",
                                 [{"role": "user", "text": "", "images": [{"mime": "image/png", "data": "YWJj"}]}],
                                 {}, {}, self.context)
            with self.assertRaises(ValueError):
                self.client._request("groq", "synthetic-groq-key", "", "https://evil.invalid/v1/models", self.context)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
