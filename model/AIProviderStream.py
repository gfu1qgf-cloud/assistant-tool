"""Bounded SSE parsing and text-only chat accumulation; no Qt objects."""
import time

from model.OpenAIResponses import normalize_response


def sse_events(response, context, deadline, maximum):
    fields, pending, size = [], bytearray(), 0
    for chunk in response.iter_content(chunk_size=1):
        context.check_canceled()
        if time.monotonic() > deadline:
            raise ValueError("流式回答超过3分钟，已停止读取；不会自动重发。")
        size += len(chunk)
        if size > maximum:
            raise ValueError("流式响应过大，已停止读取。")
        pending.extend(chunk)
        if b"\n" not in chunk:
            continue
        while b"\n" in pending:
            end = pending.index(10)
            line = bytes(pending[:end]).rstrip(b"\r").decode("utf-8-sig", errors="strict")
            del pending[:end + 1]
            if not line:
                if fields:
                    yield "\n".join(fields)
                    fields = []
            elif line.startswith("data:"):
                fields.append(line[5:].removeprefix(" "))
    if pending:
        line = bytes(pending).rstrip(b"\r").decode("utf-8-sig", errors="strict")
        if line.startswith("data:"):
            fields.append(line[5:].removeprefix(" "))
    if fields:
        yield "\n".join(fields)


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)) and not isinstance(content, bool):
        return str(content)
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


class ChatStream:
    def __init__(self, provider, model, on_delta=None):
        self.provider, self.model, self.on_delta = provider, model, on_delta
        self.parts, self.usage, self.reason = [], {}, ""
        self.terminal = False
        self.reported_model = ''
        self.metadata = {}
        self.grounding = {}
        self.annotations = []

    def feed(self, data):
        if not isinstance(data, dict):
            raise ValueError("流式事件不是有效的模型数据。")
        if isinstance(data.get("result"), dict):
            data = data["result"]
        self.model = str(data.get("model") or self.model)
        text = ""
        if self.provider == "openai":
            kind = data.get('type')
            if kind == 'response.output_text.delta':
                text = str(data.get('delta') or '')
            elif kind in {'response.completed', 'response.incomplete'}:
                result = normalize_response(data.get('response') or {})
                self.model = str(result.get('model') or self.model)
                self.reported_model = result.get('reported_model') or ''
                self.usage = result['usage']
                self.metadata = {key:result[key] for key in ('sources','search_calls')}
                self.reason = result['choices'][0]['finish_reason']
                if not self.parts:
                    text = result['choices'][0]['message']['content']
            elif kind in {'response.failed', 'error', 'response.refusal.done'}:
                raise ValueError('OpenAI 流式请求失败或被拒绝；未自动重发，已收到的文字保留供查看。')
        elif self.provider == "gemini":
            candidate = next((row for row in data.get("candidates", []) if row.get("index", 0) == 0), {})
            text = "".join(str(part.get("text") or "") for part in
                           (candidate.get("content") or {}).get("parts", [])
                           if isinstance(part, dict) and not part.get("thought"))
            self.reason = candidate.get("finishReason") or self.reason
            self.grounding.update(candidate.get('groundingMetadata') or {})
            self.usage.update(data.get("usageMetadata") or {})
        elif "response" in data:
            text = text_content(data.get("response"))
            self.usage.update(data.get("usage") or {})
            self.reason = data.get("finish_reason") or self.reason
        else:
            choice = next((row for row in data.get("choices", []) if row.get("index", 0) == 0), {})
            text = text_content((choice.get("delta") or {}).get("content"))
            self.annotations.extend(((choice.get('delta') or {}).get('annotations') or [])[:200])
            self.reason = choice.get("finish_reason") or self.reason
            self.usage.update(data.get("usage") or {})
        if self.reason == "error":
            raise ValueError("服务端在流式回答中报告错误，回答尚未完成。")
        self.terminal = self.terminal or bool(self.reason)
        if text:
            self.parts.append(text)
            if self.on_delta:
                self.on_delta(text)

    def result(self, done):
        if self.provider == "openai" and not self.terminal:
            raise ValueError("OpenAI 流式回答未收到完成确认；已收到的文字保留供查看，不自动重发。")
        if not done and not self.terminal:
            raise ValueError("流式连接提前结束，回答未完成；已收到的文字保留供查看，不作为后续上下文。")
        text = "".join(self.parts)
        if self.provider == "gemini":
            return {"model": self.model, "candidates": [{"content": {"parts": [{"text": text}]},
                    "finishReason": self.reason, 'groundingMetadata':self.grounding}], "usageMetadata": self.usage}
        return {"model": self.model, "reported_model": self.reported_model if self.provider == 'openai' else self.model,
                "choices": [{"message": {"content": text,'annotations':self.annotations},
                "finish_reason": self.reason}], "usage": self.usage, **self.metadata}
