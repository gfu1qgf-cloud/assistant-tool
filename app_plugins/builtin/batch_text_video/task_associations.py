"""Read-through task links; missing files are never a permanent negative cache."""
import copy
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
import threading

_SHEETS=OrderedDict();_LOCK=threading.RLock()
FIELDS=('path','voice_path','task_voice_path','task_subtitle_path','task_audio_name','task_background_path','match_error','association_signature')


def explicit_voice(job):
    return bool(job.get('_voice_override_before_assignment',job.get('voice_override',False)))


def for_state(job,state,**options):
    """Honor an explicitly selected saved composition before filling its links."""
    from .task_render_config import apply as apply_config
    value,view=apply_config(job,{**state,'_live_settings':state['settings']})
    return refresh(value,view['settings'],**options)


def task_folder(job,settings):
    if job.get('_associations_frozen'):return None
    if job.get('task_dir'):return Path(job['task_dir'])
    if settings.get('use_voice') and settings.get('voice_pool_mode','task')=='task' and job.get('path'):
        return Path(job['path']).parent
    return None


def sheet_audio_name(job):
    """Old sheet imports omitted audio_name; recover only association metadata."""
    source=job.get('source_sheet')
    if not source:return ''
    path=Path(source)
    try:
        stat=path.stat();key=(str(path.resolve()),stat.st_mtime_ns,stat.st_size)
        with _LOCK:records=_SHEETS.get(key)
        if records is None:
            from .task_texts import read_sheet_records
            values,_report=read_sheet_records(path)
            from .store import file_identity
            records={file_identity(v['target_dir']):str(v.get('audio_name') or v.get('task_name') or '') for v in values}
            with _LOCK:
                _SHEETS[key]=records;_SHEETS.move_to_end(key)
                while len(_SHEETS)>4:_SHEETS.popitem(last=False)
        from .store import file_identity
        return records.get(file_identity(job['task_dir']),'')
    except (OSError,ValueError,KeyError):return ''


def stamp(path):
    try:
        path=Path(path);value=path.stat()
        return str(path.resolve()),value.st_size,value.st_mtime_ns
    except (OSError,ValueError,TypeError):return str(path),None,None


def dependencies(job,settings,*,scan_artifacts=True):
    files=[]
    assigned=job.get('render_assignment') or {}
    automatic=(not explicit_voice(job) and settings.get('voice_pool_mode','task')=='task'
        and assigned.get('voice_origin','task')=='task' and settings.get('use_voice'))
    for field in ('path','subtitle_path','srt_path'):
        if job.get(field):files.append(str(job[field]))
    files.extend(str(p) for p in job.get('caption_subtitles',{}).values() if p)
    spoken=assigned.get('voice',job.get('voice_path',''))
    if spoken:files.append(spoken)
    if automatic:
        files.extend(job[k] for k in ('task_voice_path','task_subtitle_path') if job.get(k))
    if job.get('task_dir') and automatic:
        folder=Path(job['task_dir'])
        from .matching import AUDIO_EXTENSIONS
        files += [str(folder/('task_audio'+suffix)) for suffix in sorted(AUDIO_EXTENSIONS|{'.srt'})]
    from .components import walk_components
    for layer in walk_components(settings.get('layers',[])):
        frozen=assigned.get('sources',{}).get(layer['id'],{})
        if frozen.get('field')=='path':
            if frozen.get('value'):files.append(frozen['value'])
        elif layer.get('path'):files.append(str(layer['path']))
        files.extend(str(layer[k]) for k in ('source_file','caption_file') if layer.get(k))
        cursor=assigned.get('cursor_sources',{}).get(layer['id'],layer.get('caption_cursor_image'))
        if cursor:files.append(str(cursor))
        if layer.get('kind')=='image':
            from .chroma_key import active_mask
            mask=active_mask(layer)
            if mask:files.append(str(mask))
        for field,cursor in (('sequence_items','sequence_cursor'),('caption_cursor_items','caption_cursor_cursor')):
            if field=='sequence_items' and frozen:continue
            if field=='caption_cursor_items' and layer['id'] in assigned.get('cursor_sources',{}):continue
            items=layer.get(field,[])
            if items:
                selected=items[layer.get(cursor,0)%len(items)]
                if selected.get('path'):files.append(str(selected['path']))
    files.extend(v for v in (assigned.get('background'),assigned.get('voice')) if v)
    from model.ElevenLabsTimings import artifact_files
    for voice in dict.fromkeys(v for v in ((job.get('task_voice_path') if automatic else ''),spoken) if v):
        if Path(voice).is_file():
            files.append(str(Path(voice).with_suffix('.srt')))
            if scan_artifacts:
                for sidecar in artifact_files(voice):files.extend((str(sidecar),str(sidecar.with_suffix('.srt'))))
    files=list(dict.fromkeys(files))
    signature=hashlib.sha256(json.dumps(sorted(set(stamp(p) for p in files),key=lambda v:str(v[0])),ensure_ascii=False).encode('utf8')).hexdigest()
    return signature,files


def refresh(job,settings,*,cancel=None,session=None):
    """Only fill missing automatic links; explicit paths/content remain owned by UI."""
    original=job;value=copy.deepcopy(job);issues=[]
    def check():
        if cancel is not None and cancel.is_set():
            from .engine import Cancelled
            raise Cancelled('已取消关联资料检查。')
    check()
    folder=task_folder(job,settings)
    assigned=value.get('render_assignment')
    automatic=(settings.get('use_voice') and settings.get('voice_pool_mode','task')=='task' and not explicit_voice(job)
               and (assigned or {}).get('voice_origin','task')=='task' and job.get('task_audio_auto') is not False)
    if folder is not None:
        name=job.get('task_audio_name','')
        if not name and not job.get('task_voice_path') and job.get('source_sheet'):name=sheet_audio_name(job)
        from .matching import folder_entry
        cache=session if session is not None else {}
        key=(str(folder),name,bool(job.get('task_voice_optional')))
        if key not in cache:
            cache[key]=folder_entry(folder,dict(audio_name=name,text_only=bool(job.get('task_voice_optional'))))
        found=cache[key]
        if name:value['task_audio_name']=name
        old_task=str(job.get('task_voice_path') or (job.get('voice_path') if not explicit_voice(job) else '') or '')
        task_voice=old_task if old_task and Path(old_task).is_file() else found['voice_path']
        if task_voice:value['task_voice_path']=task_voice
        if task_voice and automatic:
            # Existing fixed assignments stay fixed; only their automatic holes fill.
            if not assigned or not assigned.get('voice') or not Path(assigned['voice']).is_file():
                if not value.get('voice_path') or not Path(value['voice_path']).is_file():value['voice_path']=task_voice
                if assigned:assigned['voice']=task_voice
        subtitle=Path(task_voice).with_suffix('.srt') if task_voice else None
        if old_task and task_voice and Path(old_task).stem.casefold()!=Path(task_voice).stem.casefold():
            # A missing task audio was regenerated under another known stem.
            # Its old sibling subtitle must not be relabelled as the new source.
            old_subtitle=value.get('task_subtitle_path','')
            if old_subtitle and Path(old_subtitle).stem.casefold()==Path(old_task).stem.casefold():value['task_subtitle_path']=''
        if (not value.get('task_subtitle_path') or not Path(value['task_subtitle_path']).is_file()):
            if subtitle is not None and subtitle.is_file():value['task_subtitle_path']=str(subtitle)
            elif found['task_subtitle_path']:value['task_subtitle_path']=found['task_subtitle_path']
        if found['path']:
            value['task_background_path']=found['path']
            old_background=value.get('path','')
            automatic_background=(not job.get('background_override') and settings.get('background_pool_id','')==''
                and (assigned or {}).get('background_origin','task')=='task')
            if automatic_background and (not old_background or not Path(old_background).is_file()):
                if not assigned or not assigned.get('background') or not Path(assigned['background']).is_file():
                    value['path']=found['path']
                    if assigned:assigned['background']=found['path']
        if task_voice:
            value['match_error']=''
        elif found['match_error']:
            value['match_error']=found['match_error'];issues.append(found['match_error'])
        elif job.get('task_voice_optional'):value['match_error']=''
    check();signature,dependency_files=dependencies(value,settings);check()
    value['association_signature']=signature
    fields={key:dict(present=key in original,before=copy.deepcopy(original.get(key)),after=copy.deepcopy(value.get(key)))
            for key in FIELDS if value.get(key)!=original.get(key) or key in value and key not in original}
    assignment={}
    if original.get('render_assignment'):
        for key in ('voice','background'):
            before=original['render_assignment'].get(key);after=value['render_assignment'].get(key)
            if before!=after:assignment[key]=dict(before=before,after=after)
    return dict(job=value,proposal=dict(fields=fields,assignment=assignment,
        guard=dict(voice_override=explicit_voice(original),task_dir=original.get('task_dir',''))),
        dependency_files=dependency_files,issues=issues)


def apply(job,proposal,*,strict=True):
    fields=proposal.get('fields',{});assignments=proposal.get('assignment',{})
    guard=proposal.get('guard',{})
    if guard and (explicit_voice(job)!=guard['voice_override'] or job.get('task_dir','')!=guard['task_dir']):
        if strict:raise ValueError('任务来源已被手动修改，未覆盖其关联。')
        return False
    for key,change in fields.items():
        if job.get(key)!=change['before'] and job.get(key)!=change['after']:
            if strict:raise ValueError('关联资料检查期间任务已修改，未覆盖手动设置。')
            return False
    for key,change in assignments.items():
        current=job.get('render_assignment',{}).get(key)
        if current!=change['before'] and current!=change['after']:
            if strict:raise ValueError('任务固定素材已修改，未覆盖其关联。')
            return False
    changed=False
    for key,change in fields.items():
        if job.get(key)!=change['after'] or key not in job:job[key]=copy.deepcopy(change['after']);changed=True
    for key,change in assignments.items():
        if job['render_assignment'].get(key)!=change['after']:
            job['render_assignment'][key]=copy.deepcopy(change['after']);changed=True
    return changed
