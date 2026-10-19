"""Read-only, bounded reuse of timings while scrubbing; no model/API starts."""
from collections import OrderedDict
import copy
import json
from pathlib import Path
import threading

from .caption_follow import enabled, requires_word_times, has_word_times, check_word_times

_CACHE = OrderedDict()
_LOCK = threading.RLock()


def stamp(path):
    try:
        path=Path(path);value=path.stat()
        return str(path.resolve()),value.st_mtime_ns,value.st_size
    except (OSError,ValueError,TypeError): return str(path),None,None


def cached_words(layer, job, voice, text, duration, directory, cancel):
    from .captions import (caption_subtitle_path,read_task_subtitle,word_srt_timings,
                           uniform_word_timings,load_word_timings)
    mode=layer['caption_timing']
    path=caption_subtitle_path(job,voice,layer)
    from globalValue import globalValue
    model=globalValue.whisper_model_name() if mode in {'auto','model'} else ''
    identities=[stamp(path)] if path else []
    if voice:
        identities.extend((stamp(voice),stamp(Path(voice).parent)))
        from model.ElevenLabsTimings import artifact_files
        identities.extend(stamp(file) for file in artifact_files(voice))
        identities.extend(stamp(file.with_suffix('.srt')) for file in artifact_files(voice))
    if directory: identities.append(stamp(Path(directory)/'caption-cache'))
    key=json.dumps([mode,model,text,duration,layer['caption_language'],
                    layer.get('caption_follow_mode','none'),identities],ensure_ascii=False)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key);return copy.deepcopy(_CACHE[key])
    words=None
    if voice and mode in {'auto','task','elevenlabs'} and layer.get('source','voice')=='voice':
        from model.ElevenLabsTimings import load_timings
        words=load_timings(voice,text,duration,cancel)
        if words is not None and requires_word_times(layer) and not has_word_times(words):
            if mode == 'auto': words = None
            else: check_word_times(words, layer)
    if words is None and path:
        words=read_task_subtitle(path,duration)
        words=uniform_word_timings(words,duration) if mode=='uniform' else word_srt_timings(words)
        if requires_word_times(layer) and not has_word_times(words):
            if mode == 'auto':
                words = None  # Same fallback as export, but preview may only read a cache.
            else:
                check_word_times(words, layer)
    if words is None and mode=='uniform':
        words=uniform_word_timings([dict(text=token,start=0.,end=duration,cue=0)
                                    for token in text.split()],duration)
    if words is None and voice and mode in {'auto','model'}:
        words=load_word_timings(voice,text,duration,directory,cancel,language=layer['caption_language'],cache_only=True)
    if words is not None:
        check_word_times(words, layer)
        with _LOCK:
            _CACHE[key]=copy.deepcopy(words);_CACHE.move_to_end(key)
            while len(_CACHE)>8: _CACHE.popitem(last=False)
    return words


def prepare(settings,job,directory,cancel=None):
    """Return transient preview-only words, never store them in project JSON."""
    timing=settings.get('_editor_timing')
    if not timing: return settings,[]
    from .captions import caption_text
    words_by_id={};notices=[]
    for layer in settings.get('layers',[]):
        if layer.get('kind')!='caption' or not layer.get('enabled') or not enabled(layer): continue
        try:
            words=cached_words(layer,job or {},settings.get('_preview_voice'),
                caption_text(layer,(job or {}).get('title',''),(job or {}).get('body','')),
                timing['duration'],directory,cancel)
            if words is not None:
                words_by_id[layer['id']]=words
                notices.append(layer['name']+'：动画快览复用现有时间戳，未启动对齐。')
            else:
                notices.append(layer['name']+'：尚无可复用时间戳，快览仅为排版／运动示意；点击播放预览可按所选来源准备真实字幕。')
        except (OSError,ValueError) as error:
            notices.append(layer['name']+'：时间戳快览未读取：'+str(error)+' 快览仅作排版／运动示意，不代表真实朗读时刻。')
    return {**settings,'_caption_preview_words':words_by_id},notices
