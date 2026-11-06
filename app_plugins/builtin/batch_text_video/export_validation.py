"""Cheap actionable source checks before starting explicit media work."""
from .audio_sources import resolve_voice

VOICE_REQUIRED='动态字幕需要人声。请启用人声并选择任务音频或人声池；不会使用背景音乐充当朗读。'


def missing_voice(layers,job):
    from .captions import active_captions
    names='、'.join(layer.get('name','动态字幕') for layer in active_captions(layers))
    name=job.get('name') or job.get('id') or '当前视频'
    return ValueError(f'{VOICE_REQUIRED}\n任务：{name}；字幕：{names}')


def validate_caption_voice(state,jobs):
    from .captions import active_captions
    layers=state['settings']['layers']
    if not active_captions(layers):return
    settings=state['settings']
    for job in jobs:
        voice=resolve_voice(job,state,discover=False)
        if voice:continue
        # Task-directory discovery remains in the worker. Never scan hundreds
        # of folders on the GUI thread just to prove the absence of a file.
        unknown=(settings.get('use_voice') and settings.get('voice_pool_mode','task')=='task'
                 and not job.get('voice_override') and not job.get('task_voice_optional')
                 and (job.get('task_dir') or job.get('path')))
        if not unknown:raise missing_voice(layers,job)
