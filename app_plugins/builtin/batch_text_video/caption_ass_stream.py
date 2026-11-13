"""Stream long scripts; resource guards measure concurrent work, never duration."""
from collections import defaultdict
from pathlib import Path
import json
from bisect import bisect_left,bisect_right
from contextlib import contextmanager
from contextvars import ContextVar
from model.AppDataStorage import data_file
_CHECK=ContextVar('caption_generation_check',default=None)


@contextmanager
def checking(cancel):
    def check():
        if callable(cancel):cancel()
        elif cancel is not None and cancel.is_set():
            from .engine import Cancelled
            raise Cancelled('已取消字幕生成。')
    token=_CHECK.set(check)
    try:yield
    finally:_CHECK.reset(token)


def current_check():return _CHECK.get()


class FollowWindows:
    """Bisect monotonic reading tracks; retain a conservative motion margin."""
    def __init__(self):self.tracks={}
    def indices(self,p):
        track=p.get('follow_track',())
        if not track:return 0,0
        identifier=id(track)
        if identifier not in self.tracks:
            ys=[-v[2] for v in track]
            self.tracks[identifier]=ys if all(b>=a for a,b in zip(ys,ys[1:])) else None
        ys=self.tracks[identifier];clip=p.get('screen_clip')
        if ys is None or not clip or p.get('cursor_track'):return 0,len(track)
        scale=max(1.,abs(p.get('pulse',1.15)),abs(p.get('jump',1.5)),abs(p.get('pop_peak',1.15)),abs(p.get('lzx_peak',1.28)))
        margin=(abs(p['width'])+abs(p['height'])+abs(p.get('origin_y',0)))*scale*4+160
        margin+=abs(p.get('floating_amplitude',8))*4+abs(p.get('bounce',20))*4+abs(p.get('blur',0))*32
        a=bisect_left(ys,p['y']-clip[3]-margin);b=bisect_right(ys,p['y']-clip[1]+p['height']+margin)
        return max(0,a-2),min(len(track),b+2)

    def times(self,p):
        a,b=self.indices(p);track=p.get('follow_track',())
        return (track[i][0] for i in range(a,b))

    def span(self,p):
        a,b=self.indices(p);track=p.get('follow_track',())
        if b<=a:return p['start'],p['end']
        return max(p['start'],track[a][0]),min(p['end'],track[b-1][0])


class ActiveBudget:
    def __init__(self,max_rows,max_bytes,*,ticks_per_second=100):
        self.max_rows=max_rows;self.max_bytes=max_bytes;self.deltas=defaultdict(lambda:[0,0]);self.rows=0;self.bytes=0
        self.peak_rows=self.peak_bytes=0
        self.ticks=ticks_per_second

    def add(self,start,end,cost):
        a,b=round(start*self.ticks),round(end*self.ticks)
        if a>=b:return
        self.deltas[a][0]+=1;self.deltas[b][0]-=1;self.deltas[a][1]+=cost;self.deltas[b][1]-=cost
        self.rows+=1;self.bytes+=cost

    def validate(self):
        rows=cost=0
        for time in sorted(self.deltas):
            a,b=self.deltas[time];rows+=a;cost+=b
            self.peak_rows=max(rows,self.peak_rows);self.peak_bytes=max(cost,self.peak_bytes)
        if self.peak_rows>self.max_rows:
            raise ValueError(f'同一时刻字幕绘制指令超过安全上限（峰值{self.peak_rows}条）；未省略文字或更改效果。')
        if self.peak_bytes>self.max_bytes:
            raise ValueError(f'同一时刻字幕矢量脚本超过安全容量（峰值{self.peak_bytes/1024/1024:.2f} MiB）；未省略文字或更改效果。')


def write_metadata(path,budget,resources,layer,width,height,candidates=0):
    from .caption_clip import geometry
    cursor=Path(path).with_suffix('.cursor.json');cursor_memory=0
    if cursor.is_file():
        details=json.loads(data_file(cursor).read_text('utf8'))
        cursor_memory=Path(details['commands']).stat().st_size*3+details.get('rows',0)*256
    data_file(Path(path).with_suffix('.caption-data.json')).write_text(json.dumps(dict(version=1,
        rows=budget.rows,bytes=Path(path).stat().st_size,peak_rows=budget.peak_rows,peak_bytes=budget.peak_bytes,
        candidate_segments=candidates,
        cursor_memory=cursor_memory,
        resource_glyphs=resources.glyphs,resource_bytes=resources.bytes,fonts=resources.files,
        renderers=2 if geometry(layer,width,height) is not None else 1)),encoding='utf8')
