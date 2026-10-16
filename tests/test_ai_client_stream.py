import json
import threading
import unittest
from unittest.mock import patch

from tests.test_ai_model_client import ProviderTests, UITests, Response
from model.AIProviderClient import AIProviderError
from model.BackgroundTasks import BackgroundTaskCanceled
from app_plugins.builtin.ai_model_client.settings import default_options


class SSEResponse(Response):
    def __init__(self, events, before=None):
        super().__init__({}, headers={"Content-Type": "text/event-stream; charset=utf-8"})
        self.events, self.before = events, before

    def iter_content(self, chunk_size):
        assert chunk_size == 1
        yield b": keep-alive\r\n\r\n"
        for event in self.events:
            if self.before:
                self.before()
            if isinstance(event, Exception):
                raise event
            data = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
            for value in ("data: " + data + "\n\n").encode():
                yield bytes([value])


def chunk(text="", finish=None, **fields):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}], **fields}


class StreamingProviderTests(ProviderTests):
    def streaming(self, provider, events, model="mistral-small-latest", callback=None):
        row = {"id": model, "name": model, "vision": True, "free": True}
        response = SSEResponse(events)
        with patch.object(self.client, "list_models", return_value=[row]), patch(
                "model.AIProviderClient.requests.request", return_value=response) as request:
            result = self.client.chat(provider, model, [{"role": "user", "text": "Hi", "images": []}],
                {**default_options(), "stream": True}, self.config, self.job, on_delta=callback)
        self.assertTrue(response.closed)
        return result, request.call_args

    def test_mistral_incremental_and_final_usage(self):
        pieces = []
        result, call = self.streaming("mistral", [chunk("你"), chunk("好"),
            chunk(finish="stop", usage={"prompt_tokens": 3, "completion_tokens": 2}), "[DONE]"], callback=pieces.append)
        self.assertEqual(pieces, ["你", "好"])
        self.assertEqual(result["text"], "你好")
        self.assertEqual(result["usage"], {"input": 3, "output": 2})
        self.assertTrue(call.kwargs["json"]["stream"])

    def test_openrouter_comments_reasoning_and_zero_text_events(self):
        pieces = []
        event = {"choices": [{"delta": {"reasoning": "private thoughts"}}]}
        result, call = self.streaming("openrouter", [event, chunk("Hello"), chunk(finish="length"), "[DONE]"],
            model="meta-llama/test:free", callback=pieces.append)
        self.assertEqual(result["text"], "Hello")
        self.assertEqual(pieces, ["Hello"])
        self.assertTrue(result["warning"])
        self.assertFalse(call.kwargs["json"]["provider"]["allow_fallbacks"])

    def test_groq_streaming_chat(self):
        pieces = []
        result, call = self.streaming("groq", [chunk("你"), chunk("好"),
            chunk(finish="stop", usage={"prompt_tokens": 3, "completion_tokens": 2}), "[DONE]"],
            model="openai/gpt-oss-120b", callback=pieces.append)
        self.assertEqual(result["text"], "你好")
        self.assertEqual(result["usage"], {"input": 3, "output": 2})
        self.assertEqual(pieces, ["你", "好"])
        self.assertEqual(call.args[1], "https://api.groq.com/openai/v1/chat/completions")

    def test_gemini_stream_endpoint_and_thought_ignored(self):
        pieces = []
        result, call = self.streaming("gemini", [
            {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": "A"}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "B"}]}, "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}}], model="gemini-test", callback=pieces.append)
        self.assertTrue(call.args[1].endswith(":streamGenerateContent"))
        self.assertEqual(call.kwargs["params"], {"alt": "sse"})
        self.assertEqual(result["text"], "AB")
        self.assertEqual(pieces, ["A", "B"])

    def test_cloudflare_compat_and_raw_vision_stream(self):
        result, _ = self.streaming("cloudflare", [chunk(6), chunk(" apples"), "[DONE]"],
                                    model="@cf/openai/gpt-oss-120b")
        self.assertEqual(result["text"], "6 apples")
        result, _ = self.streaming("cloudflare", [{"response": "image"}, {"response": " description"}, "[DONE]"],
                                    model="@cf/meta/llama-3.2-11b-vision-instruct")
        self.assertEqual(result["text"], "image description")

    def test_missing_terminal_is_not_success(self):
        pieces = []
        with self.assertRaisesRegex(AIProviderError, "提前结束"):
            self.streaming("mistral", [chunk("partial")], callback=pieces.append)
        self.assertEqual(pieces, ["partial"])

    def test_error_mid_stream_has_upstream_detail_and_no_retry(self):
        pieces = []
        error = {"error": {"code": 429, "message": "Provider returned error",
            "metadata": {"provider_name": "test host", "raw": "temporarily rate limited"}}}
        with self.assertRaisesRegex(AIProviderError, "temporarily rate limited"):
            self.streaming("openrouter", [chunk("partial"), error], model="meta-llama/test:free", callback=pieces.append)
        self.assertEqual(pieces, ["partial"])

    def test_mistral_top_level_error_message_is_not_lost(self):
        response = Response({"message": "Rate limit exceeded", "code": "1300"}, 429)
        with patch("model.AIProviderClient.requests.request", return_value=response):
            with self.assertRaisesRegex(AIProviderError, "Rate limit exceeded"):
                self.client._request("mistral", "test", "model", "https://api.mistral.ai/v1/chat/completions", self.job, payload={})

    def test_non_sse_json_does_not_send_second_request(self):
        pieces = []
        row = {"id": "mistral-small-latest", "vision": True, "free": True}
        response = Response({"choices": [{"message": {"content": "JSON fallback"}}]})
        with patch.object(self.client, "list_models", return_value=[row]), patch(
                "model.AIProviderClient.requests.request", return_value=response) as request:
            result = self.client.chat("mistral", row["id"], [], {**default_options(), "stream": True},
                                      self.config, self.job, on_delta=pieces.append)
        self.assertEqual(result["text"], "JSON fallback")
        self.assertEqual(pieces, ["JSON fallback"])
        self.assertEqual(request.call_count, 1)

    def test_cancel_mid_stream_closes_socket(self):
        response = SSEResponse([chunk("hello")], before=lambda: setattr(self.job, "canceled", True))
        from model.AIProviderStream import ChatStream
        with patch("model.AIProviderClient.requests.request", return_value=response):
            with self.assertRaises(BackgroundTaskCanceled):
                self.client._request("mistral", "test", "model", "https://api.mistral.ai/v1/chat/completions",
                                     self.job, payload={}, chat_stream=ChatStream("mistral", "model"))
        self.assertTrue(response.closed)


class StreamingDialogTests(UITests):
    def test_text_visible_before_request_finishes_on_gui_thread(self):
        release, started = threading.Event(), threading.Event()
        self.select_model()
        threads = []
        original = self.dialog.show_stream
        def show():
            threads.append(threading.get_ident())
            original()
        self.dialog.stream_timer.timeout.disconnect()
        self.dialog.stream_timer.timeout.connect(show)
        def chat(*args, on_delta=None):
            on_delta("<script>not HTML</script>\nfirst part")
            started.set()
            release.wait(3)
            return self.result()
        try:
            with patch.object(self.context.ai_client, "chat", side_effect=chat):
                self.dialog.input.setPlainText("hello")
                self.dialog.send()
                self.wait(started.is_set)
                self.assertFalse(self.manager.is_busy())
                self.context.submit_background_task = lambda **kwargs: self.fail("AI must not submit a host task")
                self.wait(lambda: "first part" in self.dialog.transcript.toPlainText())
                self.assertIsNotNone(self.dialog.handle)
                self.assertTrue(all(value == threading.get_ident() for value in threads))
                self.assertEqual(len(self.store.messages(self.dialog.current_id)), 1)
                self.dialog.new_conversation()
                self.assertNotIn("first part", self.dialog.transcript.toPlainText())
                release.set()
                self.wait(lambda: self.dialog.handle is None)
        finally:
            release.set()

    def test_partial_answer_persisted_on_failure_but_never_used_as_context(self):
        self.select_model()
        def chat(*args, on_delta=None):
            on_delta("unfinished text")
            raise AIProviderError("stream disconnected")
        with patch.object(self.context.ai_client, "chat", side_effect=chat):
            self.dialog.input.setPlainText("hello")
            self.dialog.send()
            self.wait(lambda: self.dialog.handle is None)
        identifier = self.dialog.current_id
        row = self.store.messages(identifier)[0]
        self.assertEqual(row["metadata"]["partial"]["text"], "unfinished text")
        self.assertIn("unfinished text", self.dialog.transcript.toPlainText())
        self.dialog.show_stream()
        self.assertEqual(self.dialog.transcript.toPlainText().count("unfinished text"), 1)
        with patch("app_plugins.builtin.ai_model_client.ui.set_internal_clipboard_text") as copied:
            self.dialog.copy_answer()
            copied.assert_called_once_with("unfinished text")
        self.assertIn("unfinished text", self.store.export_text(identifier))
        self.assertEqual(row["status"], "failed")
        prompt = self.store.begin_request(identifier, "new question", [], "mistral", "mistral-small-latest")
        context = self.store.request_messages(identifier, prompt, 20)
        self.assertEqual([value["text"] for value in context], ["new question"])

    def test_plugin_exit_retains_running_thread_without_blocking_wait(self):
        from app_plugins.builtin.ai_model_client.plugin import AIModelClientPlugin
        self.select_model()
        started, release = threading.Event(), threading.Event()
        def chat(*args, **kwargs):
            started.set()
            release.wait(3)
            args[-1].check_canceled()
            return self.result()
        plugin = AIModelClientPlugin()
        plugin.dialog = self.dialog
        try:
            with patch.object(self.context.ai_client, "chat", side_effect=chat):
                self.dialog.input.setPlainText("hello")
                self.dialog.send()
                self.wait(started.is_set)
                worker = self.dialog.handle
                self.assertFalse(plugin.can_close()[0])
                self.assertFalse(plugin.stop())
                self.assertIs(self.dialog.handle, worker)
                release.set()
                self.wait(lambda: self.dialog.handle is None)
                self.assertTrue(plugin.can_close()[0])
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
