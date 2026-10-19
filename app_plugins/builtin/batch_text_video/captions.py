"""Word-timed caption component. Local alignment, deterministic Qt glyphs, ASS motion.

No per-frame Python rendering, extra model or transparent video. FFmpeg/libass
animates vector glyphs made with the same Qt fonts as the design preview.
"""
from model.AppDataStorage import data_directory, data_file, data_io
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
from itertools import groupby

from qt_compat import QtCore, QtGui
from model.WhisperInference import whisper_inference
from .word_spacing import glyph_rows

MAX_CAPTIONS = 4
MAX_WORDS = 6000
DEFAULTS = {"caption_preset": "pop", "caption_source": "body", "caption_words": 2,
            "caption_per_line": 1, "caption_pop_ms": 160, "caption_hold_ms": 150,
            "caption_seed": 1, "caption_size_contrast": 1., "caption_language": "sk", "caption_timing": "auto", "caption_file": "",
            "caption_effects": {}}
DEFAULTS['caption_cloud_consent'] = False
DEFAULTS['caption_random_size'] = False
DEFAULTS['caption_effects_version'] = 0
from .caption_appearance import DEFAULTS as APPEARANCE_DEFAULTS
DEFAULTS.update(copy.deepcopy(APPEARANCE_DEFAULTS))
from .caption_display import DEFAULTS as DISPLAY_DEFAULTS
DEFAULTS.update(copy.deepcopy(DISPLAY_DEFAULTS))


def new_caption():
    from .components import new_text_component
    layer = new_text_component()
    layer.update(kind="caption", name="动态字幕 · 逐词跃动", source="voice",
                 x=50, y=52, width=86, height=32, font="Poppins",
                 font_min=48, font_max=72, font_weight=600, **copy.deepcopy(DEFAULTS))
    return layer


def normalize_caption(layer):
    from .caption_appearance import normalize_appearance
    for key, value in DEFAULTS.items():
        if key not in DISPLAY_DEFAULTS:
            layer.setdefault(key, copy.deepcopy(value))
    from .caption_display import normalize_display
    normalize_display(layer)
    normalize_appearance(layer)
    from .caption_random import contrast_unit
    contrast_unit(.5, layer['caption_size_contrast'])
    if type(layer['caption_random_size']) is not bool:
        raise ValueError('随机字号开关无效。')
    from .caption_styles import is_catalog, effects
    from .caption_custom_styles import LZX_WORD_POP
    if layer['caption_preset'] == LZX_WORD_POP and layer.get('word_spacing_version') != 2:
        if layer.get('word_spacing') == 20:
            layer['word_spacing'] = 0
        layer['word_spacing_version'] = 2
    if (layer["caption_preset"] not in {"pop", "appear"} and not is_catalog(layer)
            or layer["caption_source"] not in {"body", "full", "asr"}):
        raise ValueError("动态字幕预设或文案来源无效。")
    effects(layer)
    from .caption_styles import freeze_effect_parameters
    freeze_effect_parameters(layer)
    if not isinstance(layer["caption_language"], str) or not re.fullmatch(r"[a-z]{2,3}", layer["caption_language"]):
        raise ValueError("字幕语言请填写语言代码，例如 sk / en / it。")
    if layer["caption_timing"] not in {"auto", "task", "file", "model", "uniform", "elevenlabs"}:
        raise ValueError("字幕时间戳来源无效。")
    if type(layer['caption_cloud_consent']) is not bool:
        raise ValueError('云端字幕授权开关无效。')
    if not isinstance(layer["caption_file"], str) or len(layer["caption_file"]) > 2000:
        raise ValueError("字幕文件路径无效。")
    for key, low, high in (("caption_words", 1, 32), ("caption_per_line", 1, 12),
                          ("caption_pop_ms", 0, 600), ("caption_hold_ms", 0, 1000),
                          ("caption_seed", 0, 999999)):
        if type(layer[key]) is not int or not low <= layer[key] <= high:
            raise ValueError(f"动态字幕的 {key} 超出允许范围。")


def active_captions(layers):
    return [layer for layer in layers if layer["kind"] == "caption" and layer["enabled"] and layer["opacity"] > 0]


def caption_text(layer, title, body):
    if layer["caption_source"] == "asr":
        return ""
    return str(body).strip() if layer["caption_source"] == "body" else "\n".join(
        part.strip() for part in (str(title), str(body)) if part.strip())


def design_layers(layers, title, body):
    """First-word sample only; never run a model from a paint/UI callback."""
    values = copy.deepcopy(layers)
    for layer in values:
        if layer["kind"] == "caption":
            text = caption_text(layer, title, body) or "字幕 预览"
            from .caption_display import display_settings
            tokens = re.findall(r"\S+", text)
            from .caption_follow import enabled as follow_enabled
            if not display_settings(layer)['caption_accumulate'] and not follow_enabled(layer):
                tokens = tokens[:layer["caption_words"]]
            per_line = layer["caption_per_line"]
            text = "\n".join(" ".join(tokens[i:i+per_line]) for i in range(0, len(tokens), per_line))
            layer.update(kind="text", source="fixed", text=text)
            layer['_caption_style_design'] = True
    return values


def validate_words(words, duration):
    if not isinstance(words, list) or not 0 < len(words) <= MAX_WORDS:
        raise ValueError("字幕未得到有效的时间戳或文字，请核对字幕来源。")
    result, previous = [], -1.0
    for word in words:
        try:
            text = str(word["text"]).strip()
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("字幕时间戳格式错误，请重新对齐。") from None
        if (not text or len(text) > 2000 or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end <= start or start < previous or end > duration + .25):
            raise ValueError(f"字幕“{text[:40]}”的时间戳无效（{start:.3f}–{end:.3f}秒，人声{duration:.3f}秒）；请检查字幕来源或对齐结果。")
        result.append({"text": text, "start": start, "end": min(duration, end)})
        if "cue" in word:
            if type(word["cue"]) is not int or word["cue"] < 0:
                raise ValueError("已有字幕的块编号无效。")
            result[-1]["cue"] = word["cue"]
        previous = start
    if any(word["end"] <= word["start"] for word in result):
        raise ValueError("有字幕超出人声时长，请核对文案。")
    return result


def caption_subtitle_path(job, voice, layer):
    """Public adapter: job subtitle_path/srt_path or actual voice's sibling SRT."""
    mode = layer["caption_timing"]
    if mode in {"model", "elevenlabs"}:
        return None
    if mode == "file" or mode == "uniform" and layer["caption_file"].strip():
        path = Path(layer["caption_file"]) if layer["caption_file"].strip() else None
        if path is None or not path.is_file():
            raise FileNotFoundError("请在字幕设置中指定有效的本地 SRT 文件。")
        return path
    frozen = job.get("caption_subtitles",{}).get(layer["id"])
    candidates = ([Path(frozen)] if frozen else []) + [Path(job[key]) for key in ("subtitle_path", "srt_path") if job.get(key)]
    task_voice = job.get("task_voice_path", job.get("voice_path", ""))
    from .store import file_identity
    same_task_voice = not voice or bool(task_voice and file_identity(voice) == file_identity(task_voice))
    if job.get("task_subtitle_path") and same_task_voice:
        candidates.append(Path(job["task_subtitle_path"]))
    if voice:
        candidates.append(Path(voice).with_suffix(".srt"))
    if mode == "task" and job.get("task_dir") and same_task_voice:
        candidates.append(Path(job["task_dir"])/"task_audio.srt")
    found = next((path for path in candidates if path.is_file()), None)
    if found is None and mode == "task":
        expected = Path(voice).with_suffix(".srt") if voice else Path(job.get("task_dir") or ".")/"task_audio.srt"
        raise FileNotFoundError("未找到任务字幕，请放到：" + str(expected) + "；也可手动指定 SRT 或切换本地对齐。")
    return found


def read_task_subtitle(path, duration):
    """SRT cue timing is authoritative: multi-word cues appear as a whole.

    No interpolation, equal word-duration guesses, file rewrite or model call.
    A one-word-per-cue SRT naturally gives exact word-timed animation.
    """
    path = Path(path)
    if path.suffix.casefold() != ".srt" or not path.is_file() or path.stat().st_size > 2*1024*1024:
        raise ValueError("任务字幕必须是本地 SRT 文件，大小不能超过2MB。")
    raw = data_file(path).read_bytes()
    try:
        text = raw.decode("utf16" if raw.startswith((b"\xff\xfe",b"\xfe\xff")) else "utf-8-sig").replace("\r\n","\n").replace("\r","\n").strip()
    except UnicodeError:
        raise ValueError("任务字幕编码无法识别，请保存为 UTF-8 或 UTF-16 SRT。") from None
    clock = r"(\d+:\d{2}:\d{2}[,.]\d{3})"
    pattern = re.compile(r"^\s*(?:\d+\s*\n)?"+clock+r"\s*-->\s*"+clock+r"[^\n]*\n(.*)$",re.S)
    def seconds(value):
        hour,minute,last = value.replace(",",".").split(":")
        if int(minute) >= 60 or float(last) >= 60:
            raise ValueError("SRT 时间格式无效。")
        return int(hour)*3600+int(minute)*60+float(last)
    words = []
    for cue, block in enumerate(re.split(r"\n[ \t]*\n",text)):
        match = pattern.fullmatch(block)
        if not match:
            raise ValueError(f"SRT 第{cue+1}个字幕块格式不正确，未忽略任何字幕。")
        start,end,content = match.groups()
        content = re.sub(r"</?(?:i|b|u|font)(?:\s[^>]*)?>","",content,flags=re.I)
        tokens = re.findall(r"\S+",content)
        if not tokens:
            raise ValueError(f"SRT 第{cue+1}个字幕块没有内容。")
        words.extend({"text": token,"start":seconds(start),"end":seconds(end),"cue":cue} for token in tokens)
    return validate_words(words,duration)


def task_word_timings(job, voice, layer, title, body, duration, directory, cancel=None, report=None):
    if cancel and cancel.is_set():
        from .engine import Cancelled
        raise Cancelled("已取消字幕处理。")
    if report:
        mode = {'auto':'自动优先逐词记录','task':'仅任务已有字幕','file':'手动 SRT',
                'model':'本地模型逐词对齐','uniform':'匀速动画','elevenlabs':'ELE 逐词对齐'}[layer['caption_timing']]
        grouping = (f"每组最多{layer['caption_words']}词，每行最多{layer['caption_per_line']}词。"
                    if 'caption_words' in layer and 'caption_per_line' in layer else '分组与样式由下游字幕节点决定。')
        report(f"动态字幕设置：{mode}；{grouping}")
    if layer['caption_timing'] in {'auto', 'task', 'elevenlabs'} and layer.get('source', 'voice') == 'voice' and voice:
        from model.ElevenLabsTimings import load_timings
        text = caption_text(layer, title, body)
        cached = load_timings(voice, text, duration, cancel, report=report)
        if cached is not None:
            if report: report('动态字幕：复用 ELE 逐词字幕，不加载本地模型、不上传音频；原 SRT 不变。')
            return cached
    if layer['caption_timing'] == 'elevenlabs':
        if not layer.get('caption_cloud_consent'):
            raise ValueError('请在字幕设置中确认允许 ELE 上传音频与文案并消耗对齐额度；原 SRT 不会修改。')
        from model.ElevenLabsService import current_alignment_session, forced_align
        binding = current_alignment_session()
        if not binding: raise ValueError('ELE 对齐缺少主程序会话，请从主程序重新提交。')
        return forced_align(binding, voice, caption_text(layer,title,body), duration, cancel=cancel, report=report)
    path = caption_subtitle_path(job, voice, layer)
    if path:
        words = read_task_subtitle(path,duration)
        if layer["caption_timing"] == "uniform":
            if report:
                report(f"动态字幕：匀速动画，按 {path.name} 每个字幕块均分词时间；不调用模型，时刻为近似值。")
            return uniform_word_timings(words,duration)
        from .caption_follow import requires_word_times, has_word_times, check_word_times
        if requires_word_times(layer) and not has_word_times(words):
            if layer['caption_timing'] == 'auto':
                if report:
                    report('朗读跟随：现有 SRT 是整句时间，自动继续精确逐词对齐；不修改原 SRT，不估算词时刻。')
                return load_word_timings(voice,caption_text(layer,title,body),duration,directory,cancel,report,layer['caption_language'])
            check_word_times(words, layer)
        if report:
            count = len({word["cue"] for word in words})
            report(f"动态字幕：直接使用任务字幕 {path.name}（{count}块）；不加载模型、不修改原字幕。")
            if count < len(words):
                if layer.get('caption_display_mode') == 'char':
                    report('逐字显示：在每个 SRT 字幕块内按阅读顺序连续打字；字时刻是显示动画，不是逐字语音对齐，原 SRT 不变。')
                else:
                    report("已有 SRT 含多词字幕块，按原块时间一起出现；如需精确逐词弹入，请改用本地对齐。")
        return word_srt_timings(words)
    text = caption_text(layer,title,body)
    if layer["caption_timing"] == "uniform":
        tokens = re.findall(r"\S+",text)
        if not tokens:
            raise ValueError("匀速动画需要已有 SRT 或任务文案，请填写文案；不会为了估算时间加载模型。")
        if report:
            report("动态字幕：匀速动画，无任务 SRT；按文案与人声总时长均分词时间（不识别停顿）。")
        return uniform_word_timings([{"text":token,"start":0,"end":duration,"cue":0} for token in tokens],duration)
    if layer["caption_source"] != "asr" and not text:
        raise ValueError(layer["name"]+"：任务文案为空；请填写文案或选择人声识别。")
    return load_word_timings(voice,text,duration,directory,cancel,report,layer["caption_language"])


def word_srt_timings(words):
    """One word per cue is already aligned; grouping changes display, not times."""
    if words and all('cue' in w for w in words) and len({w['cue'] for w in words}) == len(words):
        return [{key:value for key,value in w.items() if key != 'cue'} for w in words]
    return words


def uniform_word_timings(words,duration):
    """Explicit user-selected approximation, never used by precise modes."""
    words = validate_words(words,duration)
    result = []
    for _cue,items in groupby(words,key=lambda word:word["cue"]):
        group = list(items)
        start,end = group[0]["start"],group[0]["end"]
        step = (end-start)/len(group)
        result.extend({"text":word["text"],"start":start+index*step,
                       "end":end if index == len(group)-1 else start+(index+1)*step}
                      for index,word in enumerate(group))
    return validate_words(result,duration)


def _audio_hash(path, check):
    digest = hashlib.sha256()
    with data_file(Path(path)).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check()
            digest.update(chunk)
    return digest.hexdigest()


def load_word_timings(voice, text, duration, directory, cancel=None, report=None, language="sk", *, cache_only=False):
    """Cache word timings independent of component geometry/animation/profile.

    A changed audio or task text cannot reuse stale alignment. The cached model
    name is the host's selected model, never an implicit fallback to base.
    """
    from .engine import Cancelled
    from globalValue import globalValue
    report = report or (lambda _message: None)

    def check():
        if cancel and cancel.is_set():
            raise Cancelled("已取消字幕对齐。")

    check()
    name = globalValue.whisper_model_name()
    signature = {"version": 1, "audio": _audio_hash(voice, check), "text": text,
                 "model": name, "duration": round(duration, 6), "language": language}
    key = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cache = Path(directory) / "caption-cache"
    if not cache_only: data_directory(cache).ensure(parents=True, exist_ok=True)
    path = cache / (key + ".json")
    try:
        value = json.loads(data_file(path).read_text("utf8"))
        if value["signature"] == signature:
            words = validate_words(value["words"], duration)
            if text:
                from .caption_alignment import original_words
                words = validate_words(original_words(words, text, language), duration)
            report("动态字幕：复用词级对齐缓存，不重新识别。")
            notes = value.get('timing_notes', [])
            if isinstance(notes, list):
                for note in notes[:4]:
                    if isinstance(note, str) and note.startswith(('动态字幕：局部修整', '动态字幕：短窗口逐词重对齐完成')) and len(note) <= 500:
                        report(note)
            return words
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if cache_only: return None  # Animation scrubbing must never launch a model.
    report(f"动态字幕：使用 {name} 本地模型{'对齐任务文案' if text else '识别人声'}，首次需要等待。")
    timing_notes = []
    def timing_report(message):
        if message.startswith(('动态字幕：局部修整', '动态字幕：短窗口逐词重对齐完成')):
            timing_notes.append(message)
        report(message)
    def coarse_loader():
        check()
        source = Path(voice).with_suffix('.srt')
        before = source.stat()
        words = read_task_subtitle(source, duration)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('读取期间同名SRT发生变化，未借用不稳定时间范围。')
        check(); return words
    try:
        with whisper_inference(progress=report, cancelled=lambda: bool(cancel and cancel.is_set())):
            model = globalValue.get_whisper_model(name)
            if text:
                from .caption_alignment import align_verbatim
                words = align_verbatim(model, voice, text, language, duration, check, timing_report, validate_words, coarse_loader)
            else:
                segments, _info = model.transcribe(str(voice), language=language, word_timestamps=True, vad_filter=True, beam_size=5)
                words = []
                try:
                    for segment in segments:
                        check()
                        words.extend({"text": word.word.strip(), "start": word.start, "end": word.end}
                                     for word in segment.words or [] if word.word.strip())
                finally:
                    close = getattr(segments, "close", None)
                    if callable(close):
                        close()
    except InterruptedError as error:
        raise Cancelled(str(error)) from error
    words = validate_words(words, duration)
    check()
    # Unique temporary name: simultaneous previews cannot corrupt a cache.
    handle, temporary = data_io.mkstemp(prefix=key + "-", suffix=".tmp", dir=cache)
    try:
        with os.fdopen(handle, "w", encoding="utf8") as stream:
            json.dump({"signature": signature, "words": words, "alignment_version": 2,
                       "timing_notes": timing_notes}, stream, ensure_ascii=False)
        check()
        data_io.replace(temporary, path)
    finally:
        data_file(Path(temporary)).remove(missing_ok=True)
    return words


def _path_commands(path):
    parts, index = [], 0
    def point(element):
        return f"{element.x:.3f} {element.y:.3f}"
    while index < path.elementCount():
        element = path.elementAt(index)
        if element.type == QtGui.QPainterPath.ElementType.MoveToElement:
            parts.append("m " + point(element))
        elif element.type == QtGui.QPainterPath.ElementType.LineToElement:
            parts.append("l " + point(element))
        elif element.type == QtGui.QPainterPath.ElementType.CurveToElement:
            parts.append("b " + " ".join(point(path.elementAt(index+i)) for i in range(3)))
            index += 2
        else:
            raise ValueError("字幕字体轮廓包含不支持的路径。")
        index += 1
    return " ".join(parts)


def _glyph(text, layer, size, native=False):
    from .fonts import weight_font
    font = weight_font(layer["font"], layer["font_weight"], layer["italic"])
    font.setPixelSize(max(1, round(size)))
    if layer["capitalization"] == "smallcaps":
        font.setCapitalization(QtGui.QFont.Capitalization.SmallCaps)
    if layer["capitalization"] == "uppercase":
        text = text.upper()
    elif layer["capitalization"] == "lowercase":
        text = text.lower()
    if layer["letter_spacing"]:
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, layer["letter_spacing"] * size / layer["font_max"])
    metrics = QtGui.QFontMetricsF(font)
    path = QtGui.QPainterPath()
    path.addText(0, metrics.ascent(), font, text)
    advance = metrics.horizontalAdvance(text)
    decorations = QtGui.QPainterPath()
    decorations.setFillRule(QtCore.Qt.FillRule.WindingFill)
    if layer["underline"]:
        decorations.addRect(0, metrics.ascent() + metrics.underlinePos(), advance, max(1, metrics.lineWidth()))
    if layer["strikeout"]:
        decorations.addRect(0, metrics.ascent() - metrics.strikeOutPos(), advance, max(1, metrics.lineWidth()))
    bounds = path.boundingRect()
    if not decorations.isEmpty():
        bounds = bounds.united(decorations.boundingRect())
    # Some fonts (e.g. Fredoka) build a letter from overlapping contours.
    # Stroking each contour exposes internal construction seams. Merge the
    # filled glyph region first, keeping real counters such as B/o intact.
    # Keep the original metrics/bounds: union flattening must not shift layout.
    path = path.simplified()
    if not decorations.isEmpty():
        path = path.united(decorations)
    if path.isEmpty() or bounds.isEmpty():
        raise ValueError("字幕字体无法绘制：" + text[:40])
    # Keep the font's ascent/baseline, not each word's ink bounding-box top.
    # Trimming the top separately makes "som" and accented "dôvodom" bounce
    # vertically even with identical font sizes and no animated movement.
    top = min(0., bounds.top())
    path.translate(-bounds.left(), -top)
    result = {"path": _path_commands(path), "width": max(advance, bounds.width()),
              "height": max(metrics.height(), bounds.bottom()) - top,
              "_baseline": metrics.ascent() - top,
              "_space_advance": metrics.horizontalAdvance(' ')}
    if native:
        result['_native_path'] = path
        finder = QtCore.QTextBoundaryFinder(QtCore.QTextBoundaryFinder.BoundaryType.Grapheme,text)
        encoded = text.encode('utf-16-le')
        edges = [0.]
        while (boundary := finder.toNextBoundary()) >= 0:
            prefix = encoded[:boundary*2].decode('utf-16-le')
            edges.append(min(result['width'],max(0,metrics.horizontalAdvance(prefix)-bounds.left())))
        edges[-1] = result['width']
        result['_cluster_edges'] = edges
    return result


def glyph_line_metrics(glyphs):
    """Content height and common baseline for mixed sizes/accents in one row."""
    ascent = max(g['_baseline'] for g in glyphs)
    descent = max(g['height']-g['_baseline'] for g in glyphs)
    return ascent+descent, ascent


def caption_events(words, layer, width, height, duration, *, native=False):
    """Stable layout for each group; a future word is NEVER shown early."""
    from .caption_styles import uses_style_renderer
    if uses_style_renderer(layer):
        from .caption_style_render import groups_and_events
        return groups_and_events(words,layer,width,height,duration)[0]
    from .layout import TextDoesNotFit
    words = validate_words(words, duration)
    scale = width / 1080
    box = QtCore.QRectF(width * (layer["x"]-layer["width"]/2)/100,
                        height * (layer["y"]-layer["height"]/2)/100,
                        width*layer["width"]/100, height*layer["height"]/100).intersected(QtCore.QRectF(0,0,width,height))
    from .shadow_render import shadow_extent
    pad = max(layer["padding"], layer["outline_width"] + 3*layer.get('outline_blur',0)+2, shadow_extent(layer)) * scale
    box.adjust(pad, pad, -pad, -pad)
    seed = hashlib.sha256(f"{layer['caption_seed']}|".encode() + json.dumps(words, ensure_ascii=False).encode()).digest()
    rng = random.Random(seed)
    from .caption_random import size_between, size_contrast
    sizes = [size_between(rng, layer["font_min"], layer["font_max"], size_contrast(layer)) for _ in words]
    events = []
    count, per_line = layer["caption_words"], layer["caption_per_line"]
    # Leave room for the 108% pop peak; positions do not jump when another word enters.
    from .caption_styles import effects
    style = effects(layer)
    peak = max(1,style['pop_start'],style['pop_peak']) if style['anim_in_type'] == 'pop' else 1
    trusted_cues = all("cue" in word for word in words)
    from .caption_display import display_settings, layout_glyphs
    groups = ([words] if display_settings(layer)['caption_accumulate'] else
              [list(items) for _cue,items in groupby(words,key=lambda word:word["cue"])] if trusted_cues else
              [words[i:i+count] for i in range(0,len(words),count)])
    start_index = 0
    for group_index, group in enumerate(groups):
        next_start = groups[group_index+1][0]["start"] if group_index+1 < len(groups) else duration
        end = min(duration,max(word["end"] for word in group)) if trusted_cues else min(
            duration,next_start,max(word["end"] for word in group)+layer["caption_hold_ms"]/1000)
        def layout(fraction):
            glyphs, lines, gap = layout_glyphs(group, layer, sizes[start_index:start_index+len(group)],
                scale, fraction, available_width=box.width(), per_line=per_line,
                gap_for=lambda _glyphs: max(0, 8+layer['word_spacing'])*scale, peak=peak, native=native)
            widths = [sum(glyphs[i]['width']*peak for i in line) + gap*(len(line)-1) for line in lines]
            metrics = [glyph_line_metrics([glyphs[i] for i in line]) for line in lines]
            heights = [height*peak*layer["line_spacing"] for height,_baseline in metrics]
            return glyphs, widths, heights, gap, metrics, lines
        chosen = layout(1)
        fits = lambda value: max(value[1]) <= box.width() and sum(value[2]) <= box.height()
        if not fits(chosen):
            chosen = layout(0)
            if not fits(chosen):
                raise TextDoesNotFit(layer["name"]+"：“"+" ".join(word["text"] for word in group)+
                    f"”最小字号仍放不下（本组实际{len(group)}词；设置每组最多{count}词、每行最多{per_line}词）。请扩大字幕框或降低最小字号；未删字、未猜测时间。")
            low, high = 0., 1.
            for _ in range(10):
                middle = (low+high)/2
                candidate = layout(middle)
                if fits(candidate):
                    low, chosen = middle, candidate
                else:
                    high = middle
        glyphs, widths, heights, gap, metrics, lines = chosen
        total = sum(heights)
        y = box.top() + (box.height()-total)*{"top":0,"center":.5,"bottom":1}[layer["vertical"]]
        for line_index, line in enumerate(lines):
            x = box.left() + (box.width()-widths[line_index])*{"left":0,"center":.5,"right":1}[layer["align"]]
            line_height, ascent = metrics[line_index]
            baseline = y + (heights[line_index]-line_height)/2 + ascent
            for i in line:
                word, glyph = group[i], glyphs[i]
                word_end = end
                if word_end <= word["start"]:
                    raise ValueError("字幕组的时间顺序冲突，请核对人声对齐。")
                events.append({**word, **glyph, "end": word_end, "spoken_end":word['end'],
                               "group_start":min(w['start'] for w in group),'group_end':end,
                               "x": x+glyph['width']*(peak-1)/2,
                               "y": baseline-glyph['_baseline'], 'group':group_index, 'line':line_index,
                               'context':False})
                x += glyph["width"]*peak + gap
            y += heights[line_index]
        start_index += len(group)
    return events


def _ass_time(value):
    value = max(0, round(value*100))
    return f"{value//360000}:{value//6000%60:02}:{value//100%60:02}.{value%100:02}"


def _ass_color(value):
    color = QtGui.QColor(value)
    return f"&H{color.blue():02X}{color.green():02X}{color.red():02X}&"


def write_caption_ass(path, words, layer, width, height, duration):
    from .caption_styles import uses_style_renderer
    if uses_style_renderer(layer):
        from .caption_style_render import write_style_ass
        return write_style_ass(path,words,layer,width,height,duration)
    from .caption_basic_render import make_primitives
    from .caption_style_render import write_primitives_ass
    primitives,events=make_primitives(words,layer,width,height,duration)
    return write_primitives_ass(path,primitives,events,layer,width,height)


def filter_path(path):
    # Filter-option quoting, not shell quoting. Input always our generated file.
    value = str(Path(path).resolve()).replace("\\", "/")
    value = "".join("\\"+char if char in "\\':" else char for char in value)
    return "".join("\\"+char if char in "\\'[],;" else char for char in value)


def static_planes(title, body, width, height, settings, image_cache):
    """Split static runs at caption layers, preserving user-controlled Z order."""
    from .layers import componentize_layers, DEFAULT_LAYERS, compose_layers
    from .layout import render_overlay
    layers = componentize_layers(settings["layers"], settings)
    from .components import resolve_sources
    from .visual_media import needs_plane
    from .scrolling_text import is_scrolling
    layers, _ = resolve_sources(layers, title, body)
    from .text_colors import resolve_colors
    layers = resolve_colors(layers, settings.get("_color_scope", "design"))
    result, group, first = [], [], True
    def flush():
        nonlocal group, first
        if not group and not first:
            return
        if group:
            ids = {item["id"] for item in group}
            selected = group + [dict(default, enabled=False) for default in DEFAULT_LAYERS if default["id"] not in ids]
            image, _ = render_overlay(title, body, width, height,
                {**settings, "layers": selected, "darkness": settings["darkness"] if first else 0,
                 "background_tint_strength": settings.get("background_tint_strength",0) if first else 0},
                image_cache, allow_empty=True)
        else:
            image = compose_layers({}, [], width, height, settings["darkness"], image_cache,
                settings.get("background_tint_color", "#000000"), settings.get("background_tint_strength", 0))
        result.append(("image", image))
        group, first = [], False
    for layer in layers:
        if not layer["enabled"] or not layer["opacity"]:
            continue
        if layer["kind"] == "node_effect":
            flush()
            result.append(("node_effect", layer))
        elif layer["kind"] == "caption":
            flush()
            result.append(("caption", layer))
        elif is_scrolling(layer):
            flush()
            result.append(("scroll", layer))
        elif needs_plane(layer):
            flush()
            result.append(("visual", layer))
        else:
            group.append(layer)
    flush()
    return result
