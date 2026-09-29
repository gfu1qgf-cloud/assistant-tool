"""Optional one-shot image classification through the app's existing Gemini keys."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from .templates import clean_scene


SYSTEM_PROMPT = """只分析这张图片，不创作视频提示词。返回一个 JSON 对象，不要 Markdown：
{"scene":"person|group|poster|statue|landscape|object|unknown",
 "subject":"画面主体的简短中文名称",
 "has_text":false,
 "expression":"smile|tears|solemn|neutral|unknown",
 "full_body":false,
 "observation":"一句话说出你确实看见的内容"}
有明显文字、标语、海报时优先选择 poster。不要猜测人物身份、宗教或故事；不确定时用「画面中的人物／主体」。不要抄写图中可能存在的私人文字。"""


def parse_scene(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("图片分析没有返回对象")
    expression = str(result.get("expression") or "unknown")
    if expression not in {"smile", "tears", "solemn", "neutral", "unknown"}:
        expression = "unknown"
    return {
        "scene": clean_scene(str(result.get("scene") or "unknown")),
        "subject": " ".join(str(result.get("subject") or "").split())[:40],
        "has_text": result.get("has_text") is True,
        "expression": expression,
        "full_body": result.get("full_body") is True,
        "observation": " ".join(str(result.get("observation") or "").split())[:160],
    }


def analyze_image(jpeg_bytes, api_keys, model="gemini-2.5-flash"):
    if not api_keys:
        raise ValueError("未配置 Gemini Key；可先在程序设置中添加，或直接使用通用方案")
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": SYSTEM_PROMPT},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg_bytes).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    errors = []
    for number, key in enumerate(api_keys, 1):
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               + urllib.parse.quote(model, safe="") + ":generateContent?key="
               + urllib.parse.quote(key, safe=""))
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8"))
            parts = result["candidates"][0]["content"]["parts"]
            content = "\n".join(part.get("text", "") for part in parts if part.get("text"))
            return parse_scene(content)
        except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError) as error:
            # Never log the URL: it contains the API key.
            code = getattr(error, "code", None)
            errors.append(f"Key {number}: HTTP {code}" if code else f"Key {number}: {type(error).__name__}")
    raise RuntimeError("图片分析失败（" + "、".join(errors) + "）。仍可手动选择类型并使用通用方案。")
