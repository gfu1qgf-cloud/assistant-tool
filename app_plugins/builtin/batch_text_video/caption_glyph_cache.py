"""Bounded shaped-word reuse across previews, styles and native subtitles."""
from collections import OrderedDict
import threading
from qt_compat import QtGui

_CACHE=OrderedDict()
_LOCK=threading.RLock()
_BYTES=0
BYTE_LIMIT=8*1024*1024
ENTRY_LIMIT=1024
FIELDS=('font','font_weight','italic','capitalization','letter_spacing','font_max','underline','strikeout')


def snapshot(value):
    result=dict(value)
    if '_native_path' in result:result['_native_path']=QtGui.QPainterPath(result['_native_path'])
    if '_cluster_edges' in result:result['_cluster_edges']=list(result['_cluster_edges'])
    return result


def get(text,layer,size,native,build):
    global _BYTES
    from .text_outline import is_preview
    key=(text,tuple(layer[field] for field in FIELDS),size,native,is_preview())
    with _LOCK:
        found=_CACHE.get(key)
        if found is not None:
            _CACHE.move_to_end(key);return snapshot(found[0])
    value=build()
    cost=len(value['path'])*2+len(text)*2+512
    if native:cost+=value['_native_path'].elementCount()*32+len(value['_cluster_edges'])*8
    if cost<=BYTE_LIMIT//4:
        with _LOCK:
            if key not in _CACHE:
                _CACHE[key]=(snapshot(value),cost);_BYTES+=cost
                while len(_CACHE)>ENTRY_LIMIT or _BYTES>BYTE_LIMIT:
                    _,(_,old)=_CACHE.popitem(last=False);_BYTES-=old
    return value
