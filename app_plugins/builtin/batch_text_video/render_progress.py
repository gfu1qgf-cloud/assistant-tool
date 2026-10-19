"""Per-item phases are separate from actual FFmpeg time/percent progress."""
from collections import Counter


def stage(progress, text):
    callback = getattr(progress, 'stage', None)
    if callable(callback):
        callback(text)


def bind(progress, job_id):
    def update(value):
        progress(job_id, value)
    callback = getattr(progress, 'stage', None)
    if callable(callback):
        update.stage = lambda text: callback(job_id, text)
    return update


def observe_report(report, progress):
    def message(text):
        for prefix, label in (
                ('等待字幕模型空闲', '等待字幕模型'),
                ('字幕模型已就绪', '字幕对齐中'),
                ('动态字幕：使用', '字幕对齐中'),
                ('动态字幕：复用', '生成字幕'),
                ('准备可复用的叠化循环', '准备背景循环'),
                ('等待全局渲染名额', '等待编码名额')):
            if str(text).startswith(prefix):
                stage(progress, label)
                break
        report(text)
    return message


def summary(stages):
    counts = Counter(value for value in stages.values() if value not in {'已完成', '需处理', '排队中', '已取消'})
    if not counts:
        return '准备中'
    return ' · '.join(f'{name} {count}' if count > 1 else name for name, count in counts.items())
