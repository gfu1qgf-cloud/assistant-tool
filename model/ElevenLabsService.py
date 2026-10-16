"""Host-owned ElevenLabs credentials; workers carry opaque session IDs only."""
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
from uuid import uuid4
from weakref import WeakValueDictionary
from contextvars import ContextVar
from contextlib import contextmanager

from model.ApiKeyHelper import (normalize_api_keys, api_key_id, select_api_keys_for_attempt,
    API_KEY_STATUSES_CONFIG_KEY, record_api_key_status)
from model.ConfigTransactions import read_api_statuses
from model.ElevenLabsTimings import (field, fingerprint, load_timings, matching_words, character_words,
    save_timings, validate_words)

ALIGNMENT_KEY = 'elevenlabs_alignment_key_id'
_SERVICES = WeakValueDictionary()
_REGISTRY_LOCK = threading.RLock()
_CURRENT_ALIGNMENT = ContextVar('elevenlabs_alignment_session', default=None)


@contextmanager
def alignment_session(binding):
    token = _CURRENT_ALIGNMENT.set(binding)
    try: yield
    finally: _CURRENT_ALIGNMENT.reset(token)


def current_alignment_session(): return _CURRENT_ALIGNMENT.get()


def resolve_session(binding):
    with _REGISTRY_LOCK: service = _SERVICES.get(binding)
    if service is None: raise ValueError('ELE 会话已失效，请从主程序重新提交；密钥未写入插件任务。')
    return service


def credentials_for_tts(binding):
    return resolve_session(binding).tts_credentials(binding)


def forced_align(binding, *args, **kwargs):
    return resolve_session(binding).forced_align(binding, *args, **kwargs)


class ElevenLabsService:
    def __init__(self):
        self._lock=threading.RLock(); self._bindings={}; self._next=0
        self._alignment_slots=threading.BoundedSemaphore(2)
        self._input_locks={}

    def bind(self, config, config_path):
        raw=config.get('elevenlabs_api_keys')
        keys=normalize_api_keys(config.get('elevenlabs_api_key','') if raw is None else raw)
        selected=str(config.get(ALIGNMENT_KEY) or '')
        statuses=config.get(API_KEY_STATUSES_CONFIG_KEY)
        snapshot={'keys':keys,'alignment':selected,'path':str(config_path or ''),
            'statuses':copy.deepcopy(statuses) if isinstance(statuses,dict) else {}}
        signature=hashlib.sha256(json.dumps({k:v for k,v in snapshot.items() if k!='statuses'},
            sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self._lock:
            for token,row in self._bindings.items():
                if row['signature']==signature:
                    row['statuses']=snapshot['statuses']
                    return token
            if len(self._bindings)>=256: raise ValueError('ELE 待执行会话过多，请完成现有任务后再提交。')
            token=uuid4().hex; self._bindings[token]={**snapshot,'signature':signature}
            with _REGISTRY_LOCK: _SERVICES[token]=self
            return token

    def _snapshot(self,binding):
        with self._lock:
            if binding not in self._bindings: raise ValueError('ELE 会话不属于当前主程序。')
            row=copy.deepcopy(self._bindings[binding])
        if row['path']:
            row['statuses'].update(read_api_statuses(row['path']))
        return row

    def tts_credentials(self,binding):
        row=self._snapshot(binding)
        with self._lock:
            keys,next_index,state=select_api_keys_for_attempt(row['keys'],row['statuses'],self._next)
            self._next=next_index
        if not keys:
            raise ValueError('ELE 没有可用的语音生成密钥，请在“程序设置 → ElevenLabs 密钥”查看额度或冷却状态。')
        return keys,row['path']

    @staticmethod
    def _check(cancel):
        if cancel and cancel.is_set(): raise InterruptedError('已取消 ELE 对齐。')
        from model.AppDataStorage import application_storage
        storage=application_storage()
        if storage.protected: storage.check()

    @staticmethod
    def _record(row,key,status,*,retry=0):
        if row['path']:
            record_api_key_status(row['path'],key,forced_alignment_status=status,
                forced_alignment_retry_after=retry or None,forced_alignment_checked_at=int(time.time()))

    def forced_align(self,binding,audio,text,duration,*,cancel=None,report=None):
        # Serialize identical inputs only; unrelated files may upload concurrently.
        self._check(cancel)
        audio=Path(audio)
        if not audio.is_file() or not 0<audio.stat().st_size<100*1024*1024:
            raise ValueError('本程序云端对齐接受小于100MB的本地音频，请拆分过长音频。')
        stamp=fingerprint(audio,cancel)
        identity=(stamp['sha256'],str(Path(audio).resolve()),str(text),duration)
        with self._lock:
            entry=self._input_locks.setdefault(identity,[threading.Lock(),0])
            entry[1]+=1
        acquired=False
        try:
            while not acquired:
                self._check(cancel)
                acquired=entry[0].acquire(timeout=.25)
            return self._forced_align(binding,audio,text,duration,cancel=cancel,report=report)
        finally:
            if acquired: entry[0].release()
            with self._lock:
                entry[1]-=1
                if not entry[1]: self._input_locks.pop(identity,None)

    def _forced_align(self,binding,audio,text,duration,*,cancel=None,report=None):
        report=report or (lambda _m:None)
        self._check(cancel)
        cached=load_timings(audio,text,duration,cancel)
        if cached is not None:
            report('动态字幕：复用 ELE 逐词记录，不上传、不扣对齐额度。'); return cached
        if not str(text or '').strip(): raise ValueError('ELE 强制对齐需要明确文案，不用于自动识别人声。')
        if len(text)>40000: raise ValueError('ELE 对齐文案过长，请先拆分；未上传。')
        row=self._snapshot(binding)
        key=next((key for key in row['keys'] if api_key_id(key)==row['alignment']),None)
        if key is None: raise ValueError('请先在“程序设置 → ElevenLabs 密钥”指定云端对齐用的 Key；不会遍历其他 Key。')
        state=row['statuses'].get(api_key_id(key),{})
        if not isinstance(state,dict): state={}
        status=state.get('forced_alignment_status')
        if status=='permission_denied':
            raise ValueError('所选 ELE Key 未开启强制对齐权限；语音生成不受此标记影响。开启权限后，请在主程序密钥页重置该 Key 状态。')
        retry=state.get('forced_alignment_retry_after')
        if type(retry) in (int,float) and retry>time.time():
            raise ValueError('所选 ELE 对齐 Key 尚在冷却；未换 Key 或重复上传。')
        audio=Path(audio)
        if not audio.is_file() or not 0<audio.stat().st_size<100*1024*1024:
            raise ValueError('本程序云端对齐接受小于100MB的本地音频，请拆分过长音频。')
        stamp=fingerprint(audio,cancel)
        from model.OfflineMode import require_online
        require_online('ElevenLabs 云端逐词对齐（上传音频与文案）')
        while not self._alignment_slots.acquire(timeout=.25): self._check(cancel)
        try:
            self._check(cancel)
            # Another concurrent request may have completed the identical input.
            cached=load_timings(audio,text,duration,cancel)
            if cached is not None: return cached
            report('ELE 云端对齐：上传当前人声与文案；仅使用你指定的 Key，原 SRT 保持不变。')
            from elevenlabs.client import ElevenLabs
            from model.AppDataStorage import data_file
            try:
                with data_file(audio).open('rb') as stream:
                    response=ElevenLabs(api_key=key).forced_alignment.create(file=stream,text=text,
                        request_options={'max_retries':0,'timeout_in_seconds':120})
            except Exception as error:
                from model.AudioHelper import _api_error_details
                detail=_api_error_details(error)
                permission=detail['status_code']==403 or any('permission' in detail[k] for k in ('code','status','type'))
                if permission:
                    self._record(row,key,'permission_denied')
                    raise ValueError('ELE Key 缺少强制对齐权限；请在 ElevenLabs 开启 Forced Alignment 权限或改选 Key。没有尝试其他 Key，语音生成仍可用。') from None
                code=detail['status_code']
                self._record(row,key,'quota' if code==402 else 'cooldown' if code==429 else 'error',
                    retry=int(time.time())+60 if code in {402,429} else 0)
                raise ValueError(f'ELE 云端对齐失败（HTTP {code if isinstance(code,int) else "未知"}）；未覆盖字幕、未自动换 Key 或重复扣费重试。') from None
            self._check(cancel)
            candidates=field(response,'words')
            try:
                words=[{'text':field(w,'text'),'start':field(w,'start'),'end':field(w,'end')} for w in candidates]
                words=matching_words(validate_words(words,duration),text)
            except (TypeError,ValueError,KeyError):
                chars=field(response,'characters') or []
                words=character_words({'characters':[field(c,'text') for c in chars],
                    'character_start_times_seconds':[field(c,'start') for c in chars],
                    'character_end_times_seconds':[field(c,'end') for c in chars]})
                words=matching_words(validate_words(words,duration),text)
            words=validate_words(words,duration)
            path=save_timings(audio,words,duration,source='elevenlabs_forced_alignment',cancel=cancel,expected=stamp)
            self._record(row,key,'available')
            report('ELE 逐词字幕已保存：'+path.name+'；原 SRT 未修改。')
            return words
        finally:
            self._alignment_slots.release()
