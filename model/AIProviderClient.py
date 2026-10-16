"""Small fixed-origin chat adapters, shared by plugins without live UI access."""
import copy
import hashlib
import json
import re
import threading
import time
from urllib.parse import quote, urlsplit

import requests

from model.AIProviderKeys import read_provider_key, cloudflare_account
from model.OfflineMode import require_online
from model.SensitiveData import redact_sensitive_text, register_sensitive_values
from model.AIProviderStream import ChatStream, sse_events

PROVIDERS = {"gemini": "Gemini", "mistral": "Mistral", "openrouter": "OpenRouter",
             "cloudflare": "Cloudflare Workers AI", "groq": "Groq"}
OR_PUBLISHERS = {"google", "mistralai", "openai", "anthropic", "meta-llama", "microsoft",
                 "nvidia", "arcee-ai", "allenai", "cohere", "perplexity", "x-ai", "amazon",
                 "ai21labs", "ibm-granite"}
MODEL_ID = re.compile(r"^(?:@cf/)?[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}$")
# Verified against the official Neurons pricing table on 2026-10-08.
# Not a guarantee of zero billing: Paid accounts can bill after the daily allowance.
# Unknown/new models require explicit opt-in, never silent paid fallback.
CF_ALLOWANCE_MODELS = {
    "@cf/meta/llama-3.1-8b-instruct", "@cf/meta/llama-3.1-8b-instruct-fp8-fast",
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/meta/llama-4-scout-17b-16e-instruct",
    "@cf/meta/llama-3.2-11b-vision-instruct", "@cf/mistralai/mistral-small-3.1-24b-instruct",
    "@cf/google/gemma-3-12b-it", "@cf/openai/gpt-oss-120b", "@cf/openai/gpt-oss-20b",
}
CF_PUBLISHERS = {"meta", "mistralai", "google", "openai", "microsoft", "nvidia", "ibm", "tiiuae"}
GROQ_FOREIGN_PUBLISHERS = {"openai", "meta-llama"}
GROQ_VISION_MODELS = {"qwen/qwen3.8-27b"}
GROQ_NON_CHAT_PARTS = ("whisper", "orpheus", "prompt-guard", "safeguard")
MAX_REQUEST_BYTES = 15 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class AIProviderError(RuntimeError):
    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def validate_model(provider, model):
    model = str(model).strip()
    if provider == "gemini" and model.startswith("models/"):
        model = model[7:]
    if provider not in PROVIDERS or not MODEL_ID.fullmatch(model):
        raise ValueError("请选择或填写一个具体的模型 ID。")
    if provider == "openrouter" and model.startswith("openrouter/"):
        raise ValueError("不使用自动/随机路由，请选择具体厂商的模型。")
    if provider == "cloudflare" and (not model.startswith("@cf/") or len(model.split("/")) != 3):
        raise ValueError("请选择具体的 Cloudflare 托管模型（@cf/厂商/模型），不使用随机路由。")
    if ".." in model or "//" in model:
        raise ValueError("模型 ID 格式不正确。")
    return model


def is_free_model(row):
    pricing = row.get("pricing") or {}
    try:
        return ("prompt" in pricing and "completion" in pricing
                and all(float(pricing.get(key, 0) or 0) == 0
                        for key in ("prompt", "completion", "image", "request", "input_audio", "output_audio")))
    except (ValueError, TypeError):
        return False


def is_groq_chat_model(row):
    """The Models API also lists audio and moderation models, not just chat models."""
    model = row.get("id", "")
    return (isinstance(model, str) and bool(MODEL_ID.fullmatch(model))
            and row.get("active") is not False
            and not any(part in model.lower() for part in GROQ_NON_CHAT_PARTS))


class AIProviderClient:
    def __init__(self, gemini_keys=None):
        self.gemini_keys = gemini_keys
        self._lock = threading.RLock()
        self._catalogs = {}
        self._cooldowns = {}

    def _key(self, provider, model, config):
        if provider == "gemini":
            key = self.gemini_keys.request_key(model) if self.gemini_keys else None
            if not key:
                import os
                key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not key:
                message = self.gemini_keys.unavailable_message(model) if self.gemini_keys else "请先添加 Gemini Key。"
                raise AIProviderError(message)
            register_sensitive_values([key])
            return key
        return read_provider_key(config, provider)

    @staticmethod
    def _fingerprint(key):
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _raise_api_error(self, provider, key, model, code, data, headers):
        register_sensitive_values([key])
        error = data.get("error", {}) if isinstance(data, dict) else {}
        detail = error.get("message", "") if isinstance(error, dict) else str(error)
        metadata = error.get("metadata") or {} if isinstance(error, dict) else {}
        if isinstance(data, dict):
            detail = detail or str(data.get("message") or data.get("detail") or "")
            if provider == "cloudflare":
                detail = "; ".join(str(item.get("message", "")) for item in data.get("errors", [])
                                   if isinstance(item, dict)) or detail
        if isinstance(metadata, dict):
            extra = [str(metadata[field]) for field in ("provider_name", "raw", "remedy_hint", "limit_source")
                     if metadata.get(field)]
            detail = "; ".join([detail, *extra])
        detail = redact_sensitive_text(detail)[:800]
        retry = headers.get("Retry-After", "")
        if code == 429:
            try:
                delay = min(86400, max(1, float(retry)))
            except (ValueError, TypeError):
                delay = 60
            with self._lock:
                self._cooldowns[(provider, self._fingerprint(key))] = time.time() + delay
        if provider == "gemini" and self.gemini_keys:
            self.gemini_keys.report_failure(key, model, code, retry_after=retry or None, message=detail)
        labels = {401: "密钥无效", 402: "余额或密钥消费上限不足",
                  403: "账号/模型权限不足，换低级模型也未必能解决",
                  429: "平台或上游限流／额度不足，不是连接超时", 503: "服务暂不可用"}
        message = labels.get(code, "请求失败；模型或参数可能不支持")
        raise AIProviderError(redact_sensitive_text(f"{PROVIDERS[provider]} HTTP {code}：{message}。{detail}"),
                              status=code, retry_after=delay if code == 429 else None)

    def _request(self, provider, key, model, url, context, *, payload=None, params=None, chat_stream=None):
        require_online("AI 模型请求")
        endpoint = urlsplit(url)
        expected_host = {"gemini": "generativelanguage.googleapis.com", "mistral": "api.mistral.ai",
                         "openrouter": "openrouter.ai", "cloudflare": "api.cloudflare.com",
                         "groq": "api.groq.com"}.get(provider)
        if endpoint.scheme != "https" or endpoint.hostname != expected_host or endpoint.port not in (None, 443) or endpoint.username or endpoint.password:
            raise ValueError("拒绝把 AI 密钥发送到非官方接口。")
        context.check_canceled()
        marker = (provider, self._fingerprint(key))
        with self._lock:
            until = self._cooldowns.get(marker, 0)
        if until > time.time():
            raise AIProviderError(f"该服务仍在限流冷却，约 {int(until-time.time())+1} 秒后再试。",
                                  status=429, retry_after=until-time.time())
        headers = ({"x-goog-api-key": key} if provider == "gemini"
                   else {"Authorization": "Bearer " + key})
        headers["Content-Type"] = "application/json"
        if payload is not None and len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("请求图片/文字过多，请减少上下文轮数或新建对话；原记录未删减。")
        response = None
        deadline = time.monotonic() + 180
        try:
            response = requests.request("GET" if payload is None else "POST", url,
                headers=headers, json=payload, params=params, timeout=(10, 60),
                allow_redirects=False, stream=True)
            if chat_stream is not None and 200 <= response.status_code < 300 and "text/event-stream" in response.headers.get("Content-Type", "").lower():
                done = False
                try:
                    for event in sse_events(response, context, deadline, MAX_RESPONSE_BYTES):
                        if not event.strip():
                            continue
                        if event.strip() == "[DONE]":
                            done = True
                            break
                        data = json.loads(event)
                        if isinstance(data, dict) and (data.get("error") or data.get("success") is False):
                            error = data.get("error") or {}
                            code = error.get("code", 500) if isinstance(error, dict) else 500
                            self._raise_api_error(provider, key, model, code, data, response.headers)
                        chat_stream.feed(data)
                    data = chat_stream.result(done)
                except (ValueError, UnicodeError) as error:
                    raise AIProviderError(redact_sensitive_text(str(error))) from None
                context.check_canceled()
                if provider == "gemini" and self.gemini_keys:
                    self.gemini_keys.report_success(key, model)
                return data
            chunks, length = [], 0
            for chunk in response.iter_content(65536):
                context.check_canceled()
                if time.monotonic() > deadline:
                    raise AIProviderError("模型响应超过3分钟，已停止读取；未自动重发。")
                length += len(chunk)
                if length > MAX_RESPONSE_BYTES:
                    raise AIProviderError("服务响应过大，已停止读取。")
                chunks.append(chunk)
            context.check_canceled()
            try:
                data = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                data = {}
            envelope_error = isinstance(data, dict) and (data.get("success") is False or bool(data.get("error")))
            if not 200 <= response.status_code < 300 or envelope_error:
                self._raise_api_error(provider, key, model, response.status_code, data, response.headers)
            if not isinstance(data, (dict, list)) or not data:
                raise AIProviderError("服务返回了空数据或非 JSON 响应，请稍后重试。")
            if provider == "gemini" and self.gemini_keys:
                self.gemini_keys.report_success(key, model)
            return data
        except requests.RequestException as error:
            raise AIProviderError("网络请求失败（" + type(error).__name__ + "），未自动重发，避免重复计费。") from None
        finally:
            if response is not None:
                response.close()

    def list_models(self, provider, config, context, *, force=False):
        require_online("查询 AI 模型列表")
        key = self._key(provider, None, config)
        account = cloudflare_account(config) if provider == "cloudflare" else ""
        marker = (provider, self._fingerprint(key), account)
        with self._lock:
            cached = self._catalogs.get(marker)
        if cached and not force and time.time() - cached[0] < 300:
            return copy.deepcopy(cached[1])
        context.report("读取 " + PROVIDERS[provider] + " 模型列表…", 10)
        rows = []
        if provider == "gemini":
            token = None
            for _ in range(10):
                params = {"pageSize": 1000}
                if token:
                    params["pageToken"] = token
                data = self._request(provider, key, "", "https://generativelanguage.googleapis.com/v1beta/models", context, params=params)
                for row in data.get("models", []):
                    if "generateContent" in row.get("supportedGenerationMethods", []):
                        rows.append({"id": row["name"].removeprefix("models/"), "name": row.get("displayName", row["name"]), "vision": None, "free": None})
                token = data.get("nextPageToken")
                if not token:
                    break
        elif provider == "cloudflare":
            seen = set()
            for page in range(1, 11):
                data = self._request(provider, key, "", f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/models/search", context,
                                     params={"format": "openrouter", "task": "Text Generation", "per_page": 100, "page": page})
                batch = data.get("data", data.get("result", []))
                if isinstance(batch, dict):
                    batch = batch.get("data", [])
                fresh = 0
                for row in batch:
                    model = row.get("id", "")
                    if not str(model).startswith("@cf/"):
                        model = row.get("name", "")
                    if not MODEL_ID.fullmatch(str(model)) or not model.startswith("@cf/") or model in seen:
                        continue
                    seen.add(model)
                    fresh += 1
                    architecture = row.get("architecture") or {}
                    if architecture.get("output_modalities") and "text" not in architecture["output_modalities"]:
                        continue
                    rows.append({"id": model, "name": row.get("name", model),
                                 "vision": "image" in architecture.get("input_modalities", []) or model == "@cf/meta/llama-3.2-11b-vision-instruct",
                                 "free": None, "allowance": model in CF_ALLOWANCE_MODELS})
                if not fresh or len(batch) < 100:
                    break
        else:
            origin = {"mistral": "https://api.mistral.ai/v1/models",
                      "openrouter": "https://openrouter.ai/api/v1/models",
                      "groq": "https://api.groq.com/openai/v1/models"}[provider]
            data = self._request(provider, key, "", origin, context)
            for row in data if isinstance(data, list) else data.get("data", []):
                if provider == "mistral":
                    capabilities = row.get("capabilities") or {}
                    if capabilities.get("completion_chat") is False or row.get("archived"):
                        continue
                    vision, free = capabilities.get("vision"), None
                elif provider == "groq":
                    if not is_groq_chat_model(row):
                        continue
                    vision, free = row["id"] in GROQ_VISION_MODELS, None
                else:
                    architecture = row.get("architecture") or {}
                    if architecture.get("output_modalities") and "text" not in architecture["output_modalities"]:
                        continue
                    vision = "image" in architecture.get("input_modalities", [])
                    free = is_free_model(row)
                model = row.get("id", "")
                if MODEL_ID.fullmatch(model) and not model.startswith("openrouter/"):
                    rows.append({"id": model, "name": row.get("name", model), "vision": vision, "free": free})
        rows.sort(key=lambda row: row["id"])
        with self._lock:
            self._catalogs[marker] = (time.time(), copy.deepcopy(rows))
        return rows

    @staticmethod
    def filter_models(provider, rows, *, free_only=True, foreign_only=True, cf_free_only=True):
        return [row for row in rows
                if not (provider == "openrouter" and free_only and not row.get("free"))
                and not (foreign_only and provider == "openrouter" and row["id"].split("/")[0] not in OR_PUBLISHERS)
                and not (foreign_only and provider == "mistral" and row["id"].lower().startswith(("glm", "qwen", "deepseek")))
                and not (provider == "cloudflare" and cf_free_only and row["id"] not in CF_ALLOWANCE_MODELS)
                and not (foreign_only and provider == "cloudflare" and row["id"].split("/")[1] not in CF_PUBLISHERS)
                and not (foreign_only and provider == "groq" and row["id"].split("/")[0] not in GROQ_FOREIGN_PUBLISHERS)]

    def chat(self, provider, model, messages, options, config, context, *, on_delta=None):
        require_online("AI 聊天请求")
        model = validate_model(provider, model)
        key = self._key(provider, model, config)
        rows = self.list_models(provider, config, context)
        selected = next((row for row in rows if row["id"] == model), None)
        if provider == "openrouter" and options.get("free_only", True) and (not selected or not selected["free"]):
            raise AIProviderError("免费模式下只能调用已确认零价格的具体模型；请刷新模型列表后选择。")
        if provider == "cloudflare" and options.get("cf_free_only", True) and model not in CF_ALLOWANCE_MODELS:
            raise AIProviderError("此模型尚未核实可用每日免费额度，已阻止发送；可在参数中明确关闭此限制。付费账号超出额度仍可能计费。")
        if provider in {"openrouter", "mistral", "cloudflare", "groq"} and options.get("foreign_only", True):
            if not self.filter_models(provider, [{"id": model, "free": True}], foreign_only=True, cf_free_only=False):
                raise AIProviderError("当前开启了国外厂商筛选；此模型不在名单中。")
        if any(item.get("images") for item in messages) and selected and selected["vision"] is False:
            raise AIProviderError("该模型明确不支持图片，请换一个标有“图片”的模型；尚未发送聊天素材。")
        if provider == "groq" and any(item.get("images") for item in messages):
            if model not in GROQ_VISION_MODELS:
                raise AIProviderError("Groq 此模型未核实支持图片，请选择标有“图片”的模型；尚未上传素材。")
            if sum(len(item.get("images", [])) for item in messages) > 3:
                raise AIProviderError("Groq 当前图片模型每次请求最多三张图片，请减少上下文图片。")
        context.report("正在请求 " + PROVIDERS[provider] + " / " + model + "…", 25)
        maximum = max(128, min(32768, int(options.get("max_tokens", 4096))))
        system = str(options.get("system", ""))
        if len(system) > 16000:
            raise ValueError("系统提示超过16000字符，请缩短；没有截断或改写原文。")
        streaming = bool(options.get("stream", False))
        params = None
        if provider == "gemini":
            contents = []
            for item in messages:
                parts = [{"text": item["text"]}] if item.get("text") else []
                for image in item.get("images", []):
                    parts.append({"inlineData": {"mimeType": image["mime"], "data": image["data"]}})
                contents.append({"role": "model" if item["role"] == "assistant" else "user", "parts": parts})
            payload = {"contents": contents, "generationConfig": {"maxOutputTokens": maximum}}
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}
            url = "https://generativelanguage.googleapis.com/v1beta/models/" + quote(model, safe="") + (":streamGenerateContent" if streaming else ":generateContent")
            if streaming:
                params = {"alt": "sse"}
        else:
            history = [{"role": "system", "content": system}] if system else []
            for item in messages:
                content = [{"type": "text", "text": item["text"]}] if item.get("text") else []
                for image in item.get("images", []):
                    image_url = "data:" + image["mime"] + ";base64," + image["data"]
                    content.append({"type": "image_url", "image_url": image_url if provider == "mistral" else {"url": image_url}})
                history.append({"role": item["role"], "content": content if item.get("images") else item["text"]})
            payload = {"model": model, "messages": history, "max_tokens": maximum, "stream": streaming}
            if provider == "openrouter":
                payload["provider"] = {"allow_fallbacks": False}
            if provider == "cloudflare":
                account = cloudflare_account(config)
                url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1/chat/completions"
                # This model's documented vision endpoint has a separate image field.
                if model == "@cf/meta/llama-3.2-11b-vision-instruct":
                    images = [image for item in messages for image in item.get("images", [])]
                    if len(images) > 1:
                        raise AIProviderError("Cloudflare Llama 3.2 Vision 此接口一次只发一张图片；请新建对话分析另一张图片，或使用其他图片模型。")
                    payload.pop("model")
                    payload["messages"] = ([{"role": "system", "content": system}] if system else []) + [{"role": item["role"], "content": item.get("text") or "请描述这张图片。"} for item in messages]
                    if images:
                        payload["image"] = "data:" + images[0]["mime"] + ";base64," + images[0]["data"]
                    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/" + model
            else:
                url = {"mistral": "https://api.mistral.ai/v1/chat/completions",
                       "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                       "groq": "https://api.groq.com/openai/v1/chat/completions"}[provider]
        if options.get("temperature") is not None:
            target = payload["generationConfig"] if provider == "gemini" else payload
            target["temperature"] = max(0.0, min(2.0, float(options["temperature"])))
        started = time.monotonic()
        accumulator = ChatStream(provider, model, on_delta) if streaming else None
        data = self._request(provider, key, model, url, context, payload=payload, params=params, chat_stream=accumulator)
        if not isinstance(data, dict):
            raise AIProviderError("模型返回的数据结构无效，请换模型或稍后重试。")
        if provider == "cloudflare" and isinstance(data.get("result"), dict):
            data = data["result"]
        if provider == "gemini":
            candidate = (data.get("candidates") or [{}])[0]
            text = "".join(str(part.get("text") or "") for part in (candidate.get("content") or {}).get("parts", []) if isinstance(part, dict) and not part.get("thought"))
            usage = data.get("usageMetadata") or {}
            tokens = {"input": usage.get("promptTokenCount", 0), "output": usage.get("candidatesTokenCount", 0)}
            reason = candidate.get("finishReason", "")
        elif provider == "cloudflare" and "response" in data:
            text = str(data.get("response") or "")
            usage = data.get("usage") or {}
            tokens = {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}
            reason = ""
        else:
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            text = content if isinstance(content, str) else "".join(part.get("text", "") for part in content if part.get("type") == "text")
            usage = data.get("usage") or {}
            tokens = {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}
            reason = choice.get("finish_reason", "")
        if not text.strip():
            raise AIProviderError("模型未返回可显示的文字，可能被拦截、只返回了思考或不支持该输入；可换模型/增大输出上限重试。")
        if on_delta and (accumulator is None or not accumulator.parts):
            on_delta(text)
        return {"text": text, "provider": provider, "model": str(data.get("model") or model),
                "usage": tokens, "elapsed": round(time.monotonic() - started, 2),
                "warning": "回答达到输出上限，可能未完。" if reason in {"MAX_TOKENS", "length"} else ""}
