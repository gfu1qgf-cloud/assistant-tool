"""Translate public official notices off the GUI thread; cache by content, not date."""
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler


class TranslationError(ValueError):
    pass


def readable_text(text):
    lines = []
    for line in str(text or "").splitlines():
        tokens = line.split()
        # This official PDF uses tracking spaces within words, two spaces between words.
        if len(tokens) > 12 and sum(len(x) == 1 for x in tokens)/len(tokens) > .6:
            line = " ".join(x.replace(" ", "") for x in re.split(r" {2,}", line.strip()))
        lines.append(line.strip())
    return "\n".join(lines).strip()


def source_digest(card):
    return hashlib.sha256(json.dumps(["waste-terms-v2", card.get("title", ""), readable_text(card.get("text", ""))],
                                     ensure_ascii=False).encode()).hexdigest()


def needs_translation(card):
    return card.get("translation_hash") != source_digest(card) or not card.get("title_zh")


def chunks(text, limit=450):
    """Stay below MyMemory's 500-byte input limit, including accented Italian."""
    remaining = text
    while remaining:
        end = min(len(remaining), limit)
        while len(remaining[:end].encode("utf-8")) > limit:
            end -= 1
        if end < len(remaining):
            sentence = max(remaining.rfind(". ", 0, end), remaining.rfind("! ", 0, end), remaining.rfind("? ", 0, end))
            boundary = sentence+1 if sentence > end//2 else max(remaining.rfind("\n", 0, end), remaining.rfind(" ", 0, end))
            if boundary > end//2:
                end = boundary+1
        yield remaining[:end]
        remaining = remaining[end:]


class TranslationRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https" or urlparse(newurl).hostname not in (
                "translate.googleapis.com", "api.mymemory.translated.net"):
            raise TranslationError("翻译服务重定向地址不受信任")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_json(url):
    with build_opener(TranslationRedirect()).open(Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/json"}), timeout=12) as response:
        data = response.read(1024*1024+1)
    if len(data) > 1024*1024:
        raise TranslationError("翻译返回内容过大")
    return json.loads(data.decode("utf-8"))


def translate_remote(text):
    try:
        data = request_json("https://translate.googleapis.com/translate_a/single?"+urlencode({
            "client": "gtx", "sl": "it", "tl": "zh-CN", "dt": "t", "q": text}))
        translated = "".join(x[0] for x in data[0] if x[0])
        if not translated.strip():
            raise TranslationError("谷歌翻译返回空内容")
        return translated, "Google Translate 网页接口"
    except Exception:
        # Free web endpoint isn't a supported Cloud API and can be rate-limited.
        try:
            data = request_json("https://api.mymemory.translated.net/get?"+urlencode({
                "langpair": "it|zh-CN", "q": text}))
            if int(data.get("responseStatus", 0)) != 200 or data.get("quotaFinished"):
                raise TranslationError("备用翻译暂不可用或已达到免费限额")
            translated = html.unescape(data["responseData"]["translatedText"])
            if not translated.strip():
                raise TranslationError("备用翻译返回空内容")
            return translated, "MyMemory"
        except Exception as error:
            raise TranslationError("在线翻译暂不可用；保留原文，可稍后重试") from error


class NoticeTranslator:
    def __init__(self, folder, cancelled=lambda: False):
        self.path = Path(folder)/"translations.json"
        self.cancelled = cancelled
        try:
            self.cache = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.cache, dict):
                self.cache = {}
        except (OSError, ValueError):
            self.cache = {}

    def _save(self):
        # Save each successful chunk: an interrupted multi-page notice resumes cheaply.
        self.cache = dict(list(self.cache.items())[-500:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(prefix="translations-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.cache, stream, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def translate(self, text):
        translated, providers = [], set()
        # Remove PDF soft line breaks so headings/clauses are translated in context.
        text = " ".join(text.split())
        # Resolve a genuine Italian ambiguity: mastello here means waste bin, not bathtub.
        text = re.sub(r"\bmastello\b", "contenitore per i rifiuti", text, flags=re.I)
        text = re.sub(r"\bmastelli\b", "contenitori per i rifiuti", text, flags=re.I)
        text = re.sub(r"\bsacchi neri\b", "sacchi neri per i rifiuti", text, flags=re.I)
        for chunk in chunks(text):
            if self.cancelled():
                raise InterruptedError("已取消公告翻译")
            key = hashlib.sha256(("it:zh-CN:v2:"+chunk).encode()).hexdigest()
            cached = self.cache.get(key)
            if not isinstance(cached, dict) or not cached.get("text"):
                output, provider = translate_remote(chunk)
                if self.cancelled():
                    raise InterruptedError("已取消公告翻译")
                cached = {"text": output, "provider": provider}
                self.cache[key] = cached
                self._save()
            translated.append(cached["text"])
            providers.add(cached.get("provider", "缓存译文"))
        return "\n".join(translated), sorted(providers)

    def translate_card(self, card):
        if not needs_translation(card):
            return card
        result = dict(card)
        try:
            title, first = self.translate(card.get("title", ""))
            body, second = self.translate(readable_text(card.get("text", "")))
            result.update(title_zh=title, text_zh=body, translation_hash=source_digest(card),
                          translation_provider=" + ".join(sorted(set(first+second))),
                          translated_at=datetime.now(timezone.utc).isoformat())
            result.pop("translation_error", None)
        except InterruptedError:
            raise
        except Exception as error:
            result["translation_error"] = str(error)
        return result
