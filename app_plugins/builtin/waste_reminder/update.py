"""Read public official pages, archive sources, and parse calendars off the GUI thread."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler

from qt_compat import QtCore

from .parser import parse_pdf, ParseError


OFFICIAL_HOSTS = {"www.teknoserviceitalia.com", "teknoserviceitalia.com", "teknoserviceitalia.b-cdn.net",
                  "www.comune.vallelomellina.pv.it", "comune.vallelomellina.pv.it"}
DEFAULT_PAGE = "https://www.teknoserviceitalia.com/lombardia/pavia/valle-lomellina/"
NOTICE_PAGE = "https://www.teknoserviceitalia.com/avvisi-e-aggiornamenti/"


def allowed_url(url):
    value = urlparse(url)
    return value.scheme == "https" and value.hostname in OFFICIAL_HOSTS and not value.username


class OfficialRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not allowed_url(newurl):
            raise ParseError("官网重定向目标不在允许列表，未访问该目标。")
        return super().redirect_request(request, fp, code, message, headers, newurl)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def discover_calendars(html, page_url=DEFAULT_PAGE):
    if not allowed_url(page_url):
        raise ParseError("自动下载只允许 Valle Lomellina / TeknoService 官方域名。")
    parser = Links()
    parser.feed(html)
    found = {}
    for raw in parser.links:
        url = urljoin(page_url, raw)
        name = unquote(urlparse(url).path).lower()
        compact = re.sub(r"[^a-z0-9]", "", name)
        if allowed_url(url) and name.endswith(".pdf") and "calendario" in name and "vallelomellina" in compact:
            years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", name.rsplit("/", 1)[-1])
            if len(years) == 1:
                found[int(years[0])] = url
    return found


class NoticeCards(HTMLParser):
    """Read the publisher's dated cards; require an exact Comune link."""
    def __init__(self):
        super().__init__()
        self.cards, self.card, self.depth, self.fields = [], None, 0, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag == "div" and "box-avviso" in classes:
            self.card = {"date": attrs.get("data-post-date", attrs.get("data-date", "")),
                         "title": "", "alert": False, "local": False, "url": ""}
            self.depth, self.fields = 0, []
        if self.card is None:
            return
        if tag == "div":
            self.depth += 1
            self.fields.append("title" if "title" in classes else "")
            if "alert" in classes:
                self.card["alert"] = True
        if tag == "a":
            href = urljoin(NOTICE_PAGE, attrs.get("href", ""))
            if href.rstrip("/") == DEFAULT_PAGE.rstrip("/"):
                self.card["local"] = True
            if self.fields and "title" in self.fields and allowed_url(href):
                self.card["url"] = href

    def handle_data(self, data):
        if self.card is not None and "title" in self.fields:
            self.card["title"] += data

    def handle_endtag(self, tag):
        if self.card is None or tag != "div":
            return
        self.depth -= 1
        if self.fields:
            self.fields.pop()
        if self.depth == 0:
            card, self.card = self.card, None
            if card["local"] and card["url"] and card["title"].strip():
                card["title"] = " ".join(card["title"].split())
                try:
                    datetime.fromisoformat(card["date"])
                except ValueError:
                    return
                card["id"] = hashlib.sha256((card["url"]+card["date"]+card["title"]).encode()).hexdigest()[:24]
                title = card["title"].casefold()
                card["zh_subject"] = ("黑色垃圾袋使用规定" if "sacchi neri" in title else
                    "收运日历/日期调整" if "calendario" in title or "raccolta" in title else
                    "垃圾回收中心开放/关闭信息" if "ecocentro" in title else "新的当地官方通知")
                self.cards.append(card)


def parse_notices(html):
    parser = NoticeCards()
    parser.feed(html)
    if not parser.cards:
        raise ParseError("官方公告页没有可识别的 Valle Lomellina 公告；可能页面结构变化，保留此前消息。")
    return sorted({card["id"]: card for card in parser.cards}.values(), key=lambda x: x["date"], reverse=True)


def fetch_with_origin_fallback(url, maximum, cancelled=lambda: False):
    try:
        return fetch(url, maximum, cancelled)
    except (OSError, TimeoutError) as error:
        if urlparse(url).hostname != "teknoserviceitalia.b-cdn.net" or cancelled():
            raise
        value = urlparse(url)
        return fetch("https://www.teknoserviceitalia.com"+value.path, maximum, cancelled)


def fetch(url, maximum, cancelled=lambda: False):
    if not allowed_url(url):
        raise ParseError("下载地址不属于配置支持的官方域名。")
    with build_opener(OfficialRedirectHandler()).open(Request(url, headers={"User-Agent": "LZX-WasteReminder/1.0 (calendar reader)"}), timeout=20) as response:
        if not allowed_url(response.url):
            raise ParseError("网页重定向到了非官方域名，停止下载。")
        parts, size = [], 0
        while True:
            if cancelled():
                raise InterruptedError("已取消官网检查")
            part = response.read(65536)
            if not part:
                break
            size += len(part)
            if size > maximum:
                raise ParseError("官网文件超过安全大小限制。")
            parts.append(part)
    return b"".join(parts)


class OfficialUpdateWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(str)
    result = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, folder, settings, years, parent=None, translation_only=False):
        super().__init__(parent)
        self.folder = Path(folder)
        self.settings = dict(settings)
        self.years = list(years)
        self.translation_only = translation_only

    def _translate_notices(self, items):
        if not self.settings.get("translate_news", True):
            return items
        from .translation import NoticeTranslator, needs_translation
        translator = NoticeTranslator(self.folder, self.isInterruptionRequested)
        output = []
        for item in items[:20]:
            if self.isInterruptionRequested():
                raise InterruptedError("已取消公告翻译")
            if needs_translation(item):
                self.progress.emit("正在后台翻译官方公告："+item.get("title", ""))
            output.append(translator.translate_card(item))
        return output+items[20:]

    def run(self):
        try:
            if self.translation_only:
                path = self.folder/"notices.json"
                old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                self.result.emit({"documents": [], "available_years": [], "translation_only": True,
                                  "notices": self._translate_notices(old.get("items", [])),
                                  "notices_checked_at": old.get("checked_at"), "errors": []})
                return
            self.progress.emit("正在读取官网年度日历链接…")
            page = self.settings["official_page"]
            html = fetch(page, 3*1024*1024, self.isInterruptionRequested)
            links = discover_calendars(html.decode("utf-8", errors="replace"), page)
            archive = self.folder / "official_archive"
            documents, errors = [], []
            for year in self.years:
                if year not in links:
                    continue
                self.progress.emit(f"正在下载并解析 {year} 年官方 PDF…")
                pdf = fetch_with_origin_fallback(links[year], 25*1024*1024, self.isInterruptionRequested)
                # Preserve the original even if a new layout cannot be parsed.
                archive.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(pdf).hexdigest()
                original = archive / f"official_calendar_{year}_{digest[:16]}.pdf"
                if not original.exists():
                    original.write_bytes(pdf)
                (archive / "official_page.html").write_bytes(html)
                try:
                    document = parse_pdf(pdf, year)
                except Exception as error:
                    errors.append(f"{year} 年 PDF 解析失败：{error}；原件已保存：{original}")
                    continue
                document["verified_on"] = datetime.now(timezone.utc).date().isoformat()
                document["sources"] = [{"title": f"官网自动下载：{year} 年原始日历 PDF",
                                        "file": str(original.relative_to(self.folder)).replace("\\", "/"),
                                        "url": links[year], "sha256": digest}]
                if self.isInterruptionRequested():
                    raise InterruptedError("已取消官网检查")
                documents.append(document)
            notices, checked = None, None
            try:
                self.progress.emit("正在检查 Valle Lomellina 官方新公告与注意事项…")
                notice_html = fetch(NOTICE_PAGE, 5*1024*1024, self.isInterruptionRequested)
                notices = parse_notices(notice_html.decode("utf-8", errors="replace"))
                archive.mkdir(parents=True, exist_ok=True)
                (archive/"notices_latest.html").write_bytes(notice_html)
                cache_path = self.folder/"notices.json"
                old = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
                known = {x["id"]: x for x in old.get("items", [])}
                for item in notices[:20]:
                    prior = known.get(item["id"], {})
                    if prior.get("file") and (self.folder/prior["file"]).is_file():
                        item.update({key: prior[key] for key in ("file", "text", "sha256", "title_zh", "text_zh",
                                    "translation_hash", "translation_provider", "translated_at") if key in prior})
                        continue
                    if urlparse(item["url"]).path.lower().endswith(".pdf"):
                        try:
                            pdf = fetch_with_origin_fallback(item["url"], 15*1024*1024, self.isInterruptionRequested)
                            if not pdf.startswith(b"%PDF"):
                                raise ParseError("公告附件不是 PDF")
                            digest = hashlib.sha256(pdf).hexdigest()
                            original = archive/f"notice_{item['date']}_{digest[:16]}.pdf"
                            if not original.exists():
                                original.write_bytes(pdf)
                            item["file"] = str(original.relative_to(self.folder)).replace("\\", "/")
                            item["sha256"] = digest
                            from pypdf import PdfReader
                            import io
                            reader = PdfReader(io.BytesIO(pdf))
                            item["text"] = "\n".join((page.extract_text() or "") for page in reader.pages[:8])[:30000]
                        except Exception as error:
                            errors.append(f"公告原件暂未下载：{item['title']}：{error}")
                checked = datetime.now(timezone.utc).isoformat()
                merged = dict(known)
                merged.update({x["id"]: x for x in notices})
                notices = self._translate_notices(sorted(merged.values(), key=lambda x: x["date"], reverse=True))
            except Exception as error:
                errors.append(f"最新公告检查未完成，保留上次消息：{error}")
            self.result.emit({"documents": documents, "available_years": sorted(links),
                              "archive": str(archive) if archive.exists() else "",
                              "notices": notices, "notices_checked_at": checked, "errors": errors})
        except Exception as error:
            import traceback
            self.failed.emit(f"{type(error).__name__}: {error}\n{traceback.format_exc()}")
