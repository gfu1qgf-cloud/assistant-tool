import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from qt_compat import QtCore, QtGui, QtTest, QtWidgets
from model.AIProviderClient import AIProviderClient, AIProviderError, CF_ALLOWANCE_MODELS, is_free_model, validate_model
from model.AIProviderKeys import KEYS_CONFIG_KEY, cloudflare_account, protect_key, read_provider_key
from model.BackgroundTasks import BackgroundTaskCanceled, BackgroundTaskManager
from model.OfflineMode import OfflineModeError
from app_plugins.builtin.ai_model_client.plugin import AIModelClientPlugin
from app_plugins.builtin.ai_model_client.settings import ClientSettingsPage, default_options
from app_plugins.builtin.ai_model_client.store import ChatStore, MAX_SOURCE_BYTES
from app_plugins.builtin.ai_model_client.ui import AIClientDialog, ChatInput, run_chat


class Job:
    def __init__(self):
        self.canceled = False
        self.reports = []
    def check_canceled(self):
        if self.canceled:
            raise BackgroundTaskCanceled("操作已取消。")
    def is_canceled(self):
        return self.canceled
    def report(self, text, percent=None):
        self.check_canceled()
        self.reports.append((text, percent))


class Response:
    def __init__(self, data, code=200, headers=None, raw=None, hook=None):
        self.body = raw if raw is not None else json.dumps(data).encode()
        self.status_code, self.headers = code, headers or {}
        self.closed = False
        self.hook = hook
    def iter_content(self, size):
        if self.hook:
            self.hook()
        yield self.body
    def close(self):
        self.closed = True


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.client, self.job = AIProviderClient(), Job()
        self.config = {"ai_model_client": {"cloudflare_account_id": "a" * 32}}
        self.key_patch = patch.object(self.client, "_key", return_value="synthetic-not-a-real-key")
        self.key_patch.start()
        self.online = patch("model.AIProviderClient.require_online")
        self.online.start()
    def tearDown(self):
        self.key_patch.stop()
        self.online.stop()
    def reply(self, provider, model, images=None, options=None, response=None):
        row = {"id": model, "name": model, "vision": True, "free": True}
        messages = [{"role": "user", "text": "你好", "images": images or []}, {"role": "assistant", "text": "Hello", "images": []}, {"role": "user", "text": "继续", "images": []}]
        data = response or ({"candidates": [{"content": {"parts": [{"text": "回答"}]}}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}} if provider == "gemini" else {"choices": [{"message": {"content": "回答"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        resp = Response(data)
        with patch.object(self.client, "list_models", return_value=[row]), patch("model.AIProviderClient.requests.request", return_value=resp) as request:
            result = self.client.chat(provider, model, messages, {**default_options(), **(options or {})}, self.config, self.job)
        self.assertTrue(resp.closed)
        return result, request.call_args
    def test_gemini_multiturn_image_payload_and_header(self):
        result, call = self.reply("gemini", "models/gemini-test", [{"mime": "image/jpeg", "data": "YWJj"}], {"system": "中文", "temperature": .4})
        payload = call.kwargs["json"]
        self.assertEqual([item["role"] for item in payload["contents"]], ["user", "model", "user"])
        self.assertEqual(payload["contents"][0]["parts"][1]["inlineData"]["data"], "YWJj")
        self.assertIn("x-goog-api-key", call.kwargs["headers"])
        self.assertNotIn("key=", call.args[1])
        self.assertEqual(payload["generationConfig"]["temperature"], .4)
        self.assertEqual(result["usage"], {"input": 3, "output": 2})
    def test_mistral_image_url_is_string(self):
        _, call = self.reply("mistral", "mistral-small-latest", [{"mime": "image/png", "data": "YWJj"}])
        self.assertEqual(call.kwargs["json"]["messages"][0]["content"][1]["image_url"], "data:image/png;base64,YWJj")
        self.assertEqual(call.args[1], "https://api.mistral.ai/v1/chat/completions")
    def test_openrouter_fixed_model_and_no_fallback(self):
        _, call = self.reply("openrouter", "mistralai/test:free", [{"mime": "image/png", "data": "YWJj"}])
        self.assertEqual(call.kwargs["json"]["provider"], {"allow_fallbacks": False})
        self.assertEqual(call.kwargs["json"]["messages"][0]["content"][1]["image_url"], {"url": "data:image/png;base64,YWJj"})
        self.assertFalse(call.kwargs["allow_redirects"])
    def test_groq_chat_and_vision_use_fixed_official_endpoint(self):
        result, call = self.reply("groq", "openai/gpt-oss-120b")
        self.assertEqual(result["text"], "回答")
        self.assertEqual(call.args[1], "https://api.groq.com/openai/v1/chat/completions")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["json"]["model"], "openai/gpt-oss-120b")
        _, call = self.reply("groq", "qwen/qwen3.8-27b", [{"mime": "image/png", "data": "YWJj"}],
                             {"foreign_only": False})
        self.assertEqual(call.kwargs["json"]["messages"][0]["content"][1]["image_url"],
                         {"url": "data:image/png;base64,YWJj"})
    def test_groq_unsupported_images_and_more_than_three_blocked_before_post(self):
        image = {"mime": "image/png", "data": "YWJj"}
        messages = [{"role": "user", "text": "看图", "images": [image]}]
        with patch.object(self.client, "list_models", return_value=[]), patch("model.AIProviderClient.requests.request") as request:
            with self.assertRaisesRegex(AIProviderError, "未核实支持图片"):
                self.client.chat("groq", "openai/gpt-oss-120b", messages, default_options(), self.config, self.job)
            with self.assertRaisesRegex(AIProviderError, "最多三张"):
                self.client.chat("groq", "qwen/qwen3.8-27b", [{**messages[0], "images": [image] * 4}],
                                 {**default_options(), "foreign_only": False}, self.config, self.job)
            request.assert_not_called()
    def test_cloudflare_chat_account_endpoint(self):
        _, call = self.reply("cloudflare", "@cf/mistralai/mistral-small-3.1-24b-instruct")
        self.assertEqual(call.args[1], f"https://api.cloudflare.com/client/v4/accounts/{'a'*32}/ai/v1/chat/completions")
        self.assertEqual(call.kwargs["json"]["model"], "@cf/mistralai/mistral-small-3.1-24b-instruct")
    def test_cloudflare_vision_documented_raw_endpoint(self):
        result, call = self.reply("cloudflare", "@cf/meta/llama-3.2-11b-vision-instruct", [{"mime": "image/png", "data": "YWJj"}], response={"success": True, "result": {"response": "图像回答"}})
        self.assertIn("/ai/run/@cf/meta/", call.args[1])
        self.assertEqual(call.kwargs["json"]["image"], "data:image/png;base64,YWJj")
        self.assertTrue(all(isinstance(m["content"], str) for m in call.kwargs["json"]["messages"]))
        self.assertEqual(result["text"], "图像回答")
    def test_cloudflare_vision_rejects_multiple_without_post(self):
        with patch.object(self.client, "list_models", return_value=[]), patch("model.AIProviderClient.requests.request") as request:
            with self.assertRaisesRegex(AIProviderError, "一张"):
                self.client.chat("cloudflare", "@cf/meta/llama-3.2-11b-vision-instruct", [{"role": "user", "text": "图", "images": [{"mime": "image/png", "data": "abc"}] * 2}], default_options(), self.config, self.job)
            request.assert_not_called()
    def test_cloudflare_unverified_model_blocked(self):
        with patch.object(self.client, "list_models", return_value=[]), patch("model.AIProviderClient.requests.request") as request:
            with self.assertRaisesRegex(AIProviderError, "免费额度"):
                self.client.chat("cloudflare", "@cf/meta/new-model", [], default_options(), self.config, self.job)
            request.assert_not_called()
    def test_openrouter_paid_model_blocked(self):
        with patch.object(self.client, "list_models", return_value=[{"id": "google/test", "free": False}]), patch("model.AIProviderClient.requests.request") as request:
            with self.assertRaisesRegex(AIProviderError, "免费"):
                self.client.chat("openrouter", "google/test", [], default_options(), self.config, self.job)
            request.assert_not_called()
    def test_foreign_filter_applies_to_manually_typed_models(self):
        for provider, model in (("openrouter", "qwen/test:free"), ("mistral", "glm-test"),
                                ("cloudflare", "@cf/deepseek-ai/test"), ("groq", "qwen/qwen3.8-27b")):
            with self.subTest(provider=provider), patch.object(self.client, "list_models", return_value=[]), patch("model.AIProviderClient.requests.request") as request:
                with self.assertRaisesRegex(AIProviderError, "国外厂商"):
                    self.client.chat(provider, model, [], {**default_options(), "free_only": False, "cf_free_only": False}, self.config, self.job)
                request.assert_not_called()
    def test_text_only_model_does_not_upload_image(self):
        with patch.object(self.client, "list_models", return_value=[{"id": "mistral-test", "vision": False}]), patch("model.AIProviderClient.requests.request") as request:
            with self.assertRaisesRegex(AIProviderError, "不支持图片"):
                self.client.chat("mistral", "mistral-test", [{"images": [{}]}], default_options(), self.config, self.job)
            request.assert_not_called()
    def test_model_ids_reject_routes_and_injection(self):
        for provider, model in (("openrouter", "openrouter/free"), ("gemini", "../../secret"),
                                ("mistral", "test?key=x"), ("cloudflare", "https://evil.test"),
                                ("cloudflare", "@cf/meta/a/b"), ("groq", "https://evil.test"),
                                ("other", "test")):
            with self.subTest(model=model), self.assertRaises(ValueError):
                validate_model(provider, model)
    def test_network_origin_cannot_be_overridden(self):
        for url in ("http://api.mistral.ai/v1/models", "https://evil.test", "https://api.mistral.ai:123/v1/models", "https://x@api.mistral.ai/v1/models"):
            with self.subTest(url=url), patch("model.AIProviderClient.requests.request") as request:
                with self.assertRaises(ValueError):
                    self.client._request("mistral", "test", "", url, self.job)
                request.assert_not_called()
        with patch("model.AIProviderClient.requests.request") as request:
            with self.assertRaises(ValueError):
                self.client._request("groq", "test", "", "https://api.mistral.ai/v1/models", self.job)
            request.assert_not_called()
    def test_offline_blocks_even_catalog_cache(self):
        with patch("model.AIProviderClient.require_online", side_effect=OfflineModeError("离线")), patch("model.AIProviderClient.requests.request") as request:
            with self.assertRaises(OfflineModeError):
                self.client.list_models("gemini", {}, self.job)
            with self.assertRaises(OfflineModeError):
                self.client.chat("gemini", "gemini-test", [], {}, {}, self.job)
            request.assert_not_called()
    def test_errors_close_response_and_never_blindly_retry(self):
        for code in (401, 403, 429, 503, 302):
            self.client._cooldowns.clear()
            response = Response({"error": {"message": "test error"}}, code)
            with self.subTest(code=code), patch("model.AIProviderClient.requests.request", return_value=response) as request:
                with self.assertRaises(AIProviderError):
                    self.client._request("mistral", "test", "model", "https://api.mistral.ai/v1/chat/completions", self.job, payload={})
                self.assertEqual(request.call_count, 1)
                self.assertTrue(response.closed)
    def test_429_has_local_cooldown(self):
        response = Response({}, 429, {"Retry-After": "30"})
        with patch("model.AIProviderClient.requests.request", return_value=response) as request:
            for _ in range(2):
                with self.assertRaises(AIProviderError):
                    self.client._request("mistral", "test", "", "https://api.mistral.ai/v1/models", self.job)
            self.assertEqual(request.call_count, 1)
    def test_http200_error_envelopes_not_success(self):
        for provider, data, url in (("cloudflare", {"success": False, "errors": [{"message": "quota"}]}, "https://api.cloudflare.com/client/v4/test"), ("openrouter", {"error": {"message": "provider error"}}, "https://openrouter.ai/api/v1/models")):
            response = Response(data)
            with self.subTest(provider=provider), patch("model.AIProviderClient.requests.request", return_value=response):
                with self.assertRaises(AIProviderError):
                    self.client._request(provider, "test", "", url, self.job)
                self.assertTrue(response.closed)
    def test_invalid_json_empty_and_oversize_responses(self):
        for raw in (b"<html>bad</html>", b"{}", b"[]", b"x" * (8*1024*1024+1)):
            response = Response(None, raw=raw)
            with self.subTest(length=len(raw)), patch("model.AIProviderClient.requests.request", return_value=response):
                with self.assertRaises(AIProviderError):
                    self.client._request("mistral", "test", "", "https://api.mistral.ai/v1/models", self.job)
                self.assertTrue(response.closed)
    def test_cancel_mid_response_closes_socket(self):
        response = Response({"data": []}, hook=lambda: setattr(self.job, "canceled", True))
        with patch("model.AIProviderClient.requests.request", return_value=response):
            with self.assertRaises(BackgroundTaskCanceled):
                self.client._request("mistral", "test", "", "https://api.mistral.ai/v1/models", self.job)
        self.assertTrue(response.closed)
    def test_request_payload_limit_precedes_http(self):
        with patch("model.AIProviderClient.requests.request") as request:
            with self.assertRaises(ValueError):
                self.client._request("mistral", "test", "", "https://api.mistral.ai/v1/models", self.job, payload={"text": "x" * (15*1024*1024)})
            request.assert_not_called()
    def test_empty_answer_and_truncation(self):
        with self.assertRaisesRegex(AIProviderError, "未返回"):
            self.reply("mistral", "mistral-test", response={"choices": [{"message": {"content": None}}]})
        result, _ = self.reply("gemini", "gemini-test", response={"candidates": [{"content": {"parts": [{"text": "private", "thought": True}, {"text": "public"}]}, "finishReason": "MAX_TOKENS"}]})
        self.assertEqual(result["text"], "public")
        self.assertTrue(result["warning"])
    def test_gemini_paginated_model_catalog_cache(self):
        responses = [Response({"models": [{"name": "models/gemini-test", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "page2"}), Response({"models": [{"name": "models/embedding-test", "supportedGenerationMethods": ["embedContent"]}]})]
        with patch("model.AIProviderClient.requests.request", side_effect=responses) as request:
            rows = self.client.list_models("gemini", {}, self.job)
            self.assertEqual([r["id"] for r in rows], ["gemini-test"])
            self.client.list_models("gemini", {}, self.job)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args.kwargs["params"]["pageToken"], "page2")
    def test_mistral_catalog_capabilities(self):
        response = Response({"data": [{"id": "mistral-small-latest", "capabilities": {"vision": True}}, {"id": "embedding", "capabilities": {"completion_chat": False}}, {"id": "archived", "archived": True}]})
        with patch("model.AIProviderClient.requests.request", return_value=response):
            rows = self.client.list_models("mistral", {}, self.job)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["vision"])
    def test_groq_catalog_excludes_non_chat_and_respects_foreign_filter(self):
        response = Response({"data": [
            {"id": "openai/gpt-oss-120b", "active": True, "owned_by": "openai"},
            {"id": "qwen/qwen3.8-27b", "active": True, "owned_by": "qwen"},
            {"id": "whisper-large-v3", "active": True},
            {"id": "canopylabs/orpheus-v1-english", "active": True},
            {"id": "meta-llama/llama-prompt-guard-2-22m", "active": True},
            {"id": "openai/old", "active": False},
        ]})
        with patch("model.AIProviderClient.requests.request", return_value=response) as request:
            rows = self.client.list_models("groq", {}, self.job)
            self.assertEqual(request.call_args.args[1], "https://api.groq.com/openai/v1/models")
        self.assertEqual([r["id"] for r in rows], ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"])
        self.assertEqual([r["id"] for r in self.client.filter_models("groq", rows)], ["openai/gpt-oss-120b"])
        self.assertTrue(rows[1]["vision"])
        self.assertIsNone(rows[0]["free"])
    def test_cloudflare_catalog_and_account_isolation(self):
        response = Response({"data": [{"id": "@cf/meta/llama-3.1-8b-instruct", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}}, {"id": "@cf/meta/new", "architecture": {"input_modalities": ["image"]}}]})
        with patch("model.AIProviderClient.requests.request", return_value=response) as request:
            rows = self.client.list_models("cloudflare", self.config, self.job)
            visible = self.client.filter_models("cloudflare", rows)
            self.assertEqual([r["id"] for r in visible], ["@cf/meta/llama-3.1-8b-instruct"])
            self.assertIsNone(visible[0]["free"])
            self.assertIn("/ai/models/search", request.call_args.args[1])
            other = {"ai_model_client": {"cloudflare_account_id": "b"*32}}
            self.client.list_models("cloudflare", other, self.job)
            self.assertEqual(request.call_count, 2)
    def test_free_price_requires_all_relevant_zero_prices(self):
        self.assertTrue(is_free_model({"pricing": {"prompt": "0", "completion": "0", "image": "0"}}))
        for pricing in ({}, {"prompt": "0"}, {"prompt": "0", "completion": "0", "image": ".01"}, {"prompt": "not-a-price", "completion": "0"}):
            self.assertFalse(is_free_model({"pricing": pricing}))

    def test_network_error_does_not_retry_or_expose_request(self):
        import requests
        with patch("model.AIProviderClient.requests.request", side_effect=requests.ReadTimeout("sensitive-detail")) as request:
            with self.assertRaisesRegex(AIProviderError, "未自动重发") as caught:
                self.client._request("mistral", "test", "", "https://api.mistral.ai/v1/models", self.job)
            self.assertNotIn("sensitive-detail", str(caught.exception))
            self.assertEqual(request.call_count, 1)

    def test_server_echoed_key_is_redacted(self):
        from model.SensitiveData import register_sensitive_values
        secret = "server-echo-secret-for-test"
        register_sensitive_values([secret])
        response = Response({"error": {"message": secret}}, 403)
        with patch("model.AIProviderClient.requests.request", return_value=response):
            with self.assertRaises(AIProviderError) as caught:
                self.client._request("mistral", secret, "", "https://api.mistral.ai/v1/models", self.job)
            self.assertNotIn(secret, str(caught.exception))

    def test_existing_gemini_broker_reports_without_erasing_keys(self):
        from model.GeminiKeyManager import GeminiKeyManager
        broker = GeminiKeyManager({"gemini_api_keys": ["synthetic-gemini-broker-key"]})
        client = AIProviderClient(broker)
        key = client._key("gemini", "gemini-test", {})
        response = Response({"error": {"message": "denied"}}, 403)
        with patch("model.AIProviderClient.requests.request", return_value=response):
            with self.assertRaises(AIProviderError):
                client._request("gemini", key, "gemini-test", "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent", self.job, payload={})
        self.assertEqual(broker.snapshot()["gemini_api_keys"], ["synthetic-gemini-broker-key"])
        self.assertIsNone(broker.request_key("gemini-test"))
        self.assertEqual(broker.request_key("other-model"), key)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ChatStore(Path(self.temp.name) / "history")
        self.identifier = self.store.create(options=default_options())
    def tearDown(self):
        self.temp.cleanup()
    def finish(self, text="hello"):
        prompt = self.store.begin_request(self.identifier, text, [], "gemini", "test")
        result = {"text": "answer", "provider": "gemini", "model": "test", "usage": {"input": 1, "output": 1}, "elapsed": .1, "warning": ""}
        self.store.complete_request(self.identifier, prompt, result)
        return prompt, result
    def image(self, size=(320, 180), alpha=False):
        buffer = io.BytesIO()
        Image.new("RGBA" if alpha else "RGB", size, "red").save(buffer, "PNG")
        return self.store.import_image(buffer.getvalue())
    def test_persist_restart_drafts_and_completed_turns(self):
        self.finish()
        self.store.update(self.identifier, draft="draft", options={"rounds": 7})
        restored = ChatStore(self.store.root)
        self.assertEqual(restored.get(self.identifier)["draft"], "draft")
        self.assertEqual(len(restored.messages(self.identifier)), 2)
        self.assertEqual(restored.get(self.identifier)["options"], {"rounds": 7})
    def test_restart_marks_pending_interrupted(self):
        self.store.begin_request(self.identifier, "pending", [], "gemini", "test")
        restored = ChatStore(self.store.root)
        self.assertEqual(restored.messages(self.identifier)[0]["status"], "interrupted")
    def test_retry_reuses_user_message_and_excludes_failed_history(self):
        self.finish()
        failed = self.store.begin_request(self.identifier, "failed", [], "gemini", "test")
        self.store.fail_request(self.identifier, failed, "error")
        prompt = self.store.begin_request(self.identifier, "", [], "mistral", "new", retry_id=failed)
        self.assertEqual(prompt, failed)
        history = self.store.request_messages(self.identifier, prompt, 2)
        self.assertEqual([r["text"] for r in history], ["hello", "answer", "failed"])
        self.assertEqual(len(self.store.messages(self.identifier)), 3)
    def test_failed_turn_not_replayed_on_new_send(self):
        failed = self.store.begin_request(self.identifier, "failed", [], "gemini", "test")
        self.store.fail_request(self.identifier, failed, "error")
        prompt = self.store.begin_request(self.identifier, "new", [], "gemini", "test")
        self.assertEqual([r["text"] for r in self.store.request_messages(self.identifier, prompt, 20)], ["new"])
    def test_duplicate_completion_is_idempotent(self):
        prompt, result = self.finish()
        self.assertFalse(self.store.complete_request(self.identifier, prompt, result))
        self.store.fail_request(self.identifier, prompt, "late cancel", True)
        self.assertEqual(len(self.store.messages(self.identifier)), 2)
        self.assertEqual(self.store.messages(self.identifier)[0]["status"], "done")
    def test_retry_only_last_failed_user(self):
        prompt, _ = self.finish()
        with self.assertRaises(ValueError):
            self.store.begin_request(self.identifier, "", [], "gemini", "test", retry_id=prompt)
    def test_context_round_limit_is_in_pairs(self):
        for i in range(4):
            self.finish(str(i))
        prompt = self.store.begin_request(self.identifier, "new", [], "gemini", "test")
        rows = self.store.request_messages(self.identifier, prompt, 1)
        self.assertEqual([r["text"] for r in rows], ["3", "answer", "new"])
    def test_sql_names_are_not_injectable(self):
        self.store.update(self.identifier, title="'; DROP TABLE messages; --")
        self.assertTrue(self.store.get(self.identifier))
        with self.assertRaises(ValueError):
            self.store.update(self.identifier, **{"title='x';--": "x"})
    def test_images_preserve_proportions_and_dedup(self):
        first, second = self.image((3000, 1500)), self.image((3000, 1500))
        self.assertEqual(first["file"], second["file"])
        self.assertEqual((first["width"], first["height"]), (2048, 1024))
        self.assertEqual(self.store.encoded_image(first)["mime"], "image/jpeg")
        self.assertEqual(len(list(self.store.images.iterdir())), 1)
    def test_alpha_image_remains_png(self):
        image = self.image(alpha=True)
        self.assertEqual(self.store.encoded_image(image)["mime"], "image/png")
    def test_source_file_unchanged(self):
        path = Path(self.temp.name) / "source.png"
        Image.new("RGB", (70, 30), "blue").save(path)
        original = path.read_bytes()
        self.store.import_image(str(path))
        self.assertEqual(path.read_bytes(), original)
    def test_invalid_image_and_oversize_fail_safely(self):
        for source in (b"not-an-image", b"x" * (MAX_SOURCE_BYTES + 1), str(Path(self.temp.name)/"missing.png")):
            with self.subTest(type=type(source)), self.assertRaises(Exception):
                self.store.import_image(source)
    def test_attachment_paths_reject_traversal(self):
        for name in ("../secret.txt", "images/../../config.json", "C:/secret.jpg", "images/not-a-hash.png"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.asset_path({"file": name})
    def test_deleted_conversation_cleans_only_unshared_managed_copies(self):
        image = self.image()
        self.store.update(self.identifier, images=[image])
        second = self.store.create()
        self.store.update(second, images=[image])
        path = self.store.asset_path(image)
        self.store.delete(self.identifier)
        self.assertTrue(path.exists())
        self.store.delete(second)
        self.assertFalse(path.exists())
    def test_worker_failure_retains_user_text_for_retry(self):
        prompt = self.store.begin_request(self.identifier, "please keep", [], "mistral", "test")
        client = AIProviderClient()
        with patch.object(client, "chat", side_effect=AIProviderError("error")), self.assertRaises(AIProviderError):
            run_chat({"conversation": self.identifier, "prompt_id": prompt, "provider": "mistral", "model": "test", "options": default_options(), "config": {}}, Job(), store=self.store, client=client)
        row = self.store.messages(self.identifier)[0]
        self.assertEqual((row["text"], row["status"]), ("please keep", "failed"))
    def test_store_concurrent_draft_and_completion(self):
        prompt = self.store.begin_request(self.identifier, "hello", [], "gemini", "test")
        result = {"text": "answer", "provider": "gemini", "model": "test", "usage": {}, "elapsed": 1, "warning": ""}
        threads = [threading.Thread(target=lambda: self.store.update(self.identifier, draft="next")), threading.Thread(target=lambda: self.store.complete_request(self.identifier, prompt, result))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(self.store.get(self.identifier)["draft"], "next")
        self.assertEqual(len(self.store.messages(self.identifier)), 2)


class FakeContext:
    def __init__(self, manager, client=None):
        self.manager = manager
        self.ai_client = client or AIProviderClient()
        self.parent_widget = None
        self.config = {"gemini_api_keys": ["synthetic-legacy-keep"], "other_setting": 42}
        self.logs = []
        self.commands, self.pages = [], []
        self.nodes = []
    @property
    def ai_workflows(self):
        from model.AIWorkflowService import AIWorkflowService
        from model.AppDataStorage import application_storage
        if not hasattr(self, '_ai_workflows'):
            self._ai_workflows = AIWorkflowService(self.ai_client,
                application_storage().for_plugin('ai_model_client').directory('workflow_cache'))
        return self._ai_workflows
    def register_node(self, definition): self.nodes.append(definition)
    def load_config(self): return copy.deepcopy(self.config)
    def log(self, text, level=None): self.logs.append((text, level))
    def patch_config(self, updates, expected): self.config.update(updates); return True
    def open_gemini_key_manager(self): return None
    def submit_background_task(self, **kwargs): return self.manager.submit(owner="ai_model_client", **kwargs)
    def register_command(self, item): self.commands.append(item)
    def register_settings_page(self, item): self.pages.append(item)


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = BackgroundTaskManager()
        self.context = FakeContext(self.manager)
        self.store = ChatStore(Path(self.temp.name) / "history")
        self.dialog = AIClientDialog(self.context, self.store)
        self.dialog.consent = True
        self.dialog.show()
        self.app.processEvents()
    def tearDown(self):
        if self.dialog.handle:
            self.dialog.handle.requestInterruption()
            self.wait(lambda: self.dialog.handle is None)
        self.manager.cancel_all()
        self.wait(lambda: not self.manager.is_busy())
        self.dialog.close()
        self.dialog.deleteLater()
        self.manager.shutdown()
        self.manager.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()
    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate(), "test timed out")
    def select_model(self, provider="mistral", model="mistral-small-latest"):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData(provider))
        self.dialog.model.setCurrentText(model)
    def result(self):
        return {"text": "<script>not HTML</script>\n测试回答", "provider": "mistral", "model": "mistral-small-latest", "usage": {"input": 1, "output": 2}, "elapsed": .05, "warning": ""}
    def test_connection_snapshot_never_copies_other_secrets(self):
        self.assertNotIn("gemini_api_keys", self.dialog.connection_snapshot())
        self.assertNotIn("other_setting", self.dialog.connection_snapshot())
    def test_model_labels_not_sent_as_model_ids(self):
        self.select_model()
        self.dialog.catalogs["mistral"] = [{"id": "mistral-small-latest", "name": "test", "vision": True, "free": None}]
        self.dialog.fill_models("mistral-small-latest")
        self.assertIn("[图片]", self.dialog.model.currentText())
        self.assertEqual(self.dialog.model_id(), "mistral-small-latest")
        self.dialog.save_draft()
        self.assertEqual(self.store.get(self.dialog.current_id)["model"], "mistral-small-latest")
        self.dialog.model.setCurrentText("manual-model")
        self.assertEqual(self.dialog.model_id(), "manual-model")
    def test_conversations_have_independent_options_and_drafts(self):
        first = self.dialog.current_id
        self.dialog.input.setPlainText("first draft")
        self.dialog.rounds.setValue(9)
        self.dialog.new_conversation()
        second = self.dialog.current_id
        self.assertNotEqual(first, second)
        self.dialog.input.setPlainText("second draft")
        self.dialog.reload_history(first)
        self.assertEqual(self.dialog.input.toPlainText(), "first draft")
        self.assertEqual(self.dialog.rounds.value(), 9)
        self.assertEqual(self.store.get(second)["draft"], "second draft")
    def test_chat_runs_worker_and_callbacks_gui_hidden_window_retains_result(self):
        self.select_model()
        release, started = threading.Event(), threading.Event()
        thread_ids = []
        def chat(*args, **kwargs):
            thread_ids.append(threading.get_ident())
            started.set()
            release.wait(2)
            return self.result()
        original = self.dialog.completed
        self.dialog.completed = lambda *args: (thread_ids.append(threading.get_ident()), original(*args))
        with patch.object(self.context.ai_client, "chat", side_effect=chat):
            self.dialog.input.setPlainText("hello")
            identifier = self.dialog.current_id
            self.dialog.send()
            self.wait(started.is_set)
            self.dialog.close()
            release.set()
            self.wait(lambda: self.dialog.handle is None)
        self.assertNotEqual(thread_ids[0], threading.get_ident())
        self.assertEqual(thread_ids[1], threading.get_ident())
        self.assertEqual(len(self.store.messages(identifier)), 2)
        self.assertIn("<script>not HTML</script>", self.dialog.transcript.toPlainText())
        self.assertFalse(self.dialog.isVisible())
    def test_send_error_retry_does_not_duplicate_prompt(self):
        self.select_model()
        self.dialog.input.setPlainText("hello")
        with patch.object(self.context.ai_client, "chat", side_effect=AIProviderError("403 权限不足")):
            self.dialog.send()
            self.wait(lambda: self.dialog.handle is None)
        self.assertIn("403", self.dialog.status.text())
        self.assertEqual(len(self.store.messages(self.dialog.current_id)), 1)
        with patch.object(self.context.ai_client, "chat", return_value=self.result()):
            self.dialog.retry_request()
            self.wait(lambda: self.dialog.handle is None)
        self.assertEqual(len(self.store.messages(self.dialog.current_id)), 2)
    def test_cancel_is_cooperative_and_preserves_prompt(self):
        self.select_model()
        started = threading.Event()
        def chat(*args, **kwargs):
            job = args[-1]
            started.set()
            while not job.is_canceled(): time.sleep(.005)
            job.check_canceled()
        with patch.object(self.context.ai_client, "chat", side_effect=chat):
            self.dialog.input.setPlainText("cancel me")
            self.dialog.send()
            self.wait(started.is_set)
            self.dialog.stop_request()
            self.wait(lambda: self.dialog.handle is None)
        self.assertEqual(self.store.messages(self.dialog.current_id)[0]["status"], "canceled")
        self.assertTrue(self.dialog.send_button.isEnabled())
    def test_switch_conversation_while_job_running_updates_correct_history(self):
        self.select_model()
        release, started = threading.Event(), threading.Event()
        def chat(*args, **kwargs):
            started.set(); release.wait(2); return self.result()
        with patch.object(self.context.ai_client, "chat", side_effect=chat):
            self.dialog.input.setPlainText("old")
            old = self.dialog.current_id
            self.dialog.send()
            self.wait(started.is_set)
            self.dialog.new_conversation()
            new = self.dialog.current_id
            self.dialog.input.setPlainText("new draft")
            release.set()
            self.wait(lambda: self.dialog.handle is None)
        self.assertEqual(len(self.store.messages(old)), 2)
        self.assertEqual(self.store.messages(new), [])
        self.assertEqual(self.dialog.input.toPlainText(), "new draft")
    def test_paste_image_bytes_processed_in_host_queue(self):
        mime = QtCore.QMimeData()
        image = QtGui.QImage(40, 80, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor("red"))
        mime.setImageData(image)
        self.assertTrue(self.dialog.input.canInsertFromMimeData(mime))
        self.dialog.input.insertFromMimeData(mime)
        self.wait(lambda: self.dialog.handle is None)
        self.assertEqual(len(self.dialog.pending_images), 1)
        self.assertEqual((self.dialog.pending_images[0]["width"], self.dialog.pending_images[0]["height"]), (40, 80))
    def test_drag_local_urls_and_remote_urls_not_downloaded(self):
        mime = QtCore.QMimeData()
        path = Path(self.temp.name)/"image.png"
        Image.new("RGB", (20, 20), "blue").save(path)
        mime.setUrls([QtCore.QUrl.fromLocalFile(str(path))])
        self.assertTrue(self.dialog.input.canInsertFromMimeData(mime))
        self.dialog.input.insertFromMimeData(mime)
        self.wait(lambda: self.dialog.handle is None)
        self.assertEqual(len(self.dialog.pending_images), 1)
        remote = QtCore.QMimeData()
        remote.setText("https://example.test/image.png")
        remote.setUrls([QtCore.QUrl("https://example.test/image.png")])
        self.dialog.input.insertFromMimeData(remote)
        self.assertIsNone(self.dialog.handle)
    def test_ctrl_enter_sends_and_enter_newline(self):
        calls = []
        self.dialog.input.sendRequested.connect(lambda: calls.append(True))
        QtTest.QTest.keyClick(self.dialog.input, QtCore.Qt.Key.Key_Return)
        self.assertEqual(self.dialog.input.toPlainText(), "\n")
        QtTest.QTest.keyClick(self.dialog.input, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(calls, [True])
    def test_settings_blank_keys_preserved_and_explicit_removal(self):
        self.context.config[KEYS_CONFIG_KEY] = {"mistral": "dpapi:stored", "openrouter": "dpapi:other",
                                                 "groq": "dpapi:groq"}
        page = ClientSettingsPage(self.context)
        config = self.context.load_config()
        page.update_config(config)
        self.assertEqual(config[KEYS_CONFIG_KEY], self.context.config[KEYS_CONFIG_KEY])
        self.assertEqual(config["gemini_api_keys"], ["synthetic-legacy-keep"])
        page.clear_checks["mistral"].setChecked(True)
        page.update_config(config)
        self.assertNotIn("mistral", config[KEYS_CONFIG_KEY])
        self.assertIn("openrouter", config[KEYS_CONFIG_KEY])
        self.assertIn("groq", config[KEYS_CONFIG_KEY])
        page.widget.deleteLater()
    def test_settings_new_key_dpapi_roundtrip_and_legacy_preserved(self):
        page = ClientSettingsPage(self.context)
        page.edits["cloudflare"].setText("synthetic-cloudflare-test")
        page.account.setText("A"*32)
        config = self.context.load_config()
        page.update_config(config)
        self.assertTrue(config[KEYS_CONFIG_KEY]["cloudflare"].startswith("dpapi:"))
        self.assertNotIn("synthetic-cloudflare-test", config[KEYS_CONFIG_KEY]["cloudflare"])
        self.assertEqual(read_provider_key(config, "cloudflare"), "synthetic-cloudflare-test")
        self.assertEqual(cloudflare_account(config), "a"*32)
        self.assertEqual(config["gemini_api_keys"], self.context.config["gemini_api_keys"])
        page.widget.deleteLater()
    def test_groq_key_settings_roundtrip(self):
        page = ClientSettingsPage(self.context)
        page.edits["groq"].setText("synthetic-groq-test-key")
        config = self.context.load_config()
        page.update_config(config)
        self.assertEqual(read_provider_key(config, "groq"), "synthetic-groq-test-key")
        self.assertNotIn("synthetic-groq-test-key", str(config))
        page.widget.deleteLater()
    def test_invalid_account_settings_are_rejected(self):
        page = ClientSettingsPage(self.context)
        page.account.setText("not-token-or-account")
        with self.assertRaises(ValueError): page.validate()
        page.widget.deleteLater()
    def test_plugin_register_is_lazy_and_can_be_disabled(self):
        plugin = AIModelClientPlugin()
        plugin.register(self.context)
        self.assertIsNone(plugin.store)
        self.assertEqual(len(self.context.commands), 1)
        self.assertEqual(len(self.context.pages), 1)
        self.assertEqual(plugin.can_close(), (True, ""))
        self.assertTrue(plugin.stop())

    def test_real_host_menu_settings_shared_keys_and_cached_dialog(self):
        from tests.test_plugin_host import FakeMainWindow
        from app_plugins.host import PluginHost
        from model.GeminiKeyManager import GeminiKeyManager
        window = FakeMainWindow()
        window.gemini_keys = GeminiKeyManager({"gemini_api_keys": ["synthetic-host-key"]}, window)
        host = PluginHost(window)
        plugin = host.install(AIModelClientPlugin())
        menu = host.attach_main_menu(QtWidgets.QMenuBar(window))
        self.assertIn("AI 模型试用台…", [action.text() for action in menu.actions()])
        self.assertIsNone(plugin.store)
        self.assertIs(host.ai_client.gemini_keys, window.gemini_keys)
        self.assertIs(plugin.context.ai_client, host.ai_client)
        tabs = QtWidgets.QTabWidget(window)
        host.create_settings_pages(window, tabs)
        self.assertIn("AI 模型试用台", [tabs.tabText(i) for i in range(tabs.count())])
        with patch("app_plugins.builtin.ai_model_client.plugin.APP_ROOT", Path(self.temp.name)):
            first = host.invoke("ai_model_client.open")
            self.assertIsNotNone(first)
            first.close()
            self.assertIs(host.invoke("ai_model_client.open"), first)
            self.assertTrue(first.isVisible())
            first.close()
        self.assertTrue(host.stop_all())
        window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    def test_small_window_and_parameter_layout(self):
        self.dialog.resize(860, 650)
        self.dialog.options_panel.show()
        self.app.processEvents()
        self.assertGreater(self.dialog.transcript.height(), 60)
        self.assertGreater(self.dialog.input.width(), 360)
        self.assertLessEqual(self.dialog.options_panel.mapTo(self.dialog, self.dialog.options_panel.rect().bottomRight()).x(), self.dialog.width())


class CredentialTests(unittest.TestCase):
    def test_current_windows_user_key_encryption(self):
        encrypted = protect_key("synthetic-roundtrip-only")
        self.assertEqual(read_provider_key({KEYS_CONFIG_KEY: {"mistral": encrypted}}, "mistral"), "synthetic-roundtrip-only")
    def test_plaintext_saved_key_is_not_used(self):
        with self.assertRaisesRegex(ValueError, "未加密"):
            read_provider_key({KEYS_CONFIG_KEY: {"mistral": "plaintext"}}, "mistral")
    def test_corrupt_encrypted_key_never_falls_back_silently(self):
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "environment-test"}), self.assertRaises(Exception):
            read_provider_key({KEYS_CONFIG_KEY: {"mistral": "dpapi:broken"}}, "mistral")
    def test_environment_token_and_account(self):
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "environment-test", "CLOUDFLARE_ACCOUNT_ID": "c"*32}):
            self.assertEqual(read_provider_key({}, "cloudflare"), "environment-test")
            self.assertEqual(cloudflare_account({}), "c"*32)
    def test_account_id_cannot_inject_path(self):
        for account in ("../abc", "a"*31, "z"*32, "https://other.test", "a"*33):
            with self.subTest(account=account), self.assertRaises(ValueError):
                cloudflare_account({"ai_model_client": {"cloudflare_account_id": account}})


if __name__ == "__main__":
    unittest.main()
