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
    for job in jobs:
        view=state
        if job.get('render_from_snapshot'):
            from .task_render_config import apply as apply_config
            job,view=apply_config(job,{**state,'_live_settings':state['settings']})
        if job.get('render_assignment'):
            # Voice-only preflight: no project/pool copies or media reads.
            from model.PrivateDataPaths import remap_path_fields
            from .task_associations import explicit_voice
            assignment=job['render_assignment']
            pending=(not assignment.get('voice') and assignment.get('voice_origin','task')=='task'
                and not explicit_voice(job) and state['settings'].get('use_voice')
                and state['settings'].get('voice_pool_mode','task')=='task')
            if pending:
                job={**job,'voice_override':False}
                view={**view,'settings':{**view['settings'],'use_voice':True}}
            else:
                voice={'voice_path':assignment.get('voice','')}
                remap_path_fields(voice,{'voice_path'})
                job={**job,**voice,'voice_override':True}
                view={**view,'settings':{**view['settings'],'use_voice':bool(voice['voice_path'])}}
        settings=view['settings'];layers=settings['layers']
        if not active_captions(layers):continue
        voice=resolve_voice(job,view,discover=False)
        if voice:continue
        # Task-directory discovery remains in the worker. Never scan hundreds
        # of folders on the GUI thread just to prove the absence of a file.
        unknown=(settings.get('use_voice') and settings.get('voice_pool_mode','task')=='task'
                 and not job.get('voice_override')
                 and (job.get('task_dir') or job.get('path')))
        if not unknown:raise missing_voice(layers,job)
