"""Immutable, deduplicated task settings and explicit single-source rerenders."""
import copy
import hashlib
import json
import uuid
from pathlib import Path


def clean(value):
    if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if not k.startswith('_')}
    if isinstance(value,list):return [clean(v) for v in value]
    return copy.deepcopy(value)


def capture(job,state):
    if job.get('render_from_snapshot') and job.get('render_config_id'):
        settings=state.get('render_configs',{}).get(job['render_config_id'])
        if not settings:raise ValueError('任务配置快照缺失。')
        return dict(version=1,settings=copy.deepcopy(settings),content=copy.deepcopy(job.get('render_content',{})))
    settings=clean(state['settings'])
    from .components import walk_components
    for layer in walk_components(settings['layers']):
        if layer.get('source')=='pool':layer['sequence_items']=[]
    return dict(version=1,settings=settings,content={k:copy.deepcopy(job[k]) for k in
        ('title','body','requires_title','caption_subtitles') if k in job})


def record(state,job_id,config):
    if not config:return False
    job=next(v for v in state['jobs'] if v['id']==job_id)
    settings=config['settings']
    key=hashlib.sha256(json.dumps(settings,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf8')).hexdigest()
    if (job.get('render_config_id')==key and key in state.get('render_configs',{})
            and job.get('render_content',{})==config.get('content',{})):return False
    state.setdefault('render_configs',{})[key]=copy.deepcopy(settings)
    job.update(render_config_id=key,render_content=copy.deepcopy(config.get('content',{})))
    return True


def apply(job,state):
    key=job.get('render_config_id')
    if not key or not (job.get('_use_render_config') or job.get('render_from_snapshot')):return job,state
    settings=state.get('render_configs',{}).get(key)
    if not isinstance(settings,dict):raise ValueError('任务配置快照缺失，不能用当前项目设置冒充；请重新配置此任务。')
    state={**state,'settings':copy.deepcopy(settings)};job=copy.deepcopy(job)
    # Output policy is chosen at export, independent of the frozen composition.
    from .render_settings import KEYS
    for field in (*KEYS,'output_destination','output_dir','output_relative_root'):
        if field in state.get('_live_settings',{}):state['settings'][field]=state['_live_settings'][field]
    job.update(copy.deepcopy(job.get('render_content',{})))
    from model.PrivateDataPaths import remap_path_fields
    remap_path_fields(state['settings'],{'path','source_file','caption_file','caption_cursor_image','voice_path'})
    return job,state


def bound(job,state):
    job={**job,'_use_render_config':True}
    state={**state,'_live_settings':state['settings']}
    from .render_assignment import apply as assignment
    return assignment(job,state)


def media(job,state):
    job,state=bound(job,state)
    result=[dict(id='@background',name='底层背景',path=job.get('path',''))]
    from .components import walk_components
    for layer in walk_components(state['settings']['layers']):
        if layer.get('kind')=='image':result.append(dict(id=layer['id'],name=layer.get('name','图片／视频'),path=layer.get('path','')))
        if layer.get('kind')=='caption' and layer.get('caption_cursor_mode')=='image':
            result.append(dict(id='@cursor:'+layer['id'],name=layer.get('name','字幕')+' · 图片光标',path=layer.get('caption_cursor_image','')))
    return result


def replace(job,state,identifier,path):
    path=Path(path).resolve()
    from .layers import IMAGE_SUFFIXES
    from .visual_media import VISUAL_SUFFIXES
    if not path.is_file() or path.suffix.casefold() not in VISUAL_SUFFIXES:
        raise ValueError('请选择存在的图片或视频素材。')
    if not job.get('render_assignment'):
        raise ValueError('此旧任务没有固定素材分配记录，无法保证其他素材不变；请先按原配置导出一次。')
    valid={v['id'] for v in media(job,state)}
    if identifier not in valid:raise ValueError('所选控件已不存在，请重新打开任务。')
    if identifier.startswith('@cursor:') and path.suffix.casefold() not in IMAGE_SUFFIXES:
        raise ValueError('图片光标需要图片素材。')
    result=copy.deepcopy(job);assignment=result['render_assignment'];value=str(path)
    if identifier=='@background':assignment['background']=value
    elif identifier.startswith('@cursor:'):assignment.setdefault('cursor_sources',{})[identifier[8:]]=value
    else:assignment.setdefault('sources',{})[identifier]={'field':'path','value':value}
    # Do not reuse this task's earlier generated playback; originals are untouched.
    result['render_cache_token']=uuid.uuid4().hex
    return result


def stage(state,pid,job_id,edited,expected):
    from .queue_selection import ACTIVE
    from .pools import snapshot
    result=copy.deepcopy(state)
    profile=next((v for v in result['profiles'] if v['id']==pid),None)
    if profile is None or profile.get('revision',0)!=expected:raise ValueError('项目已修改，请重新打开任务后再渲染。')
    if any(p['profile_id']==pid and p['status'] in ACTIVE for p in result['render_plans']):
        raise ValueError('此项目正在渲染，请结束本批后重新渲染。')
    data=result if result['active_profile']==pid else profile.get('data') or {}
    job=next((v for v in data['jobs'] if v['id']==job_id),None)
    if job is None:raise ValueError('任务已删除，请刷新队列。')
    from .node_effects import active_effects
    _view_job,view_state=bound(job,data)
    if active_effects(view_state['settings']) or active_effects(data['settings']):
        raise ValueError('节点工作流尚未保存完整执行快照，暂不能保证原配置重新渲染；请在项目中导出。')
    # The UI can replace media and content only through this guarded operation.
    for field in ('render_assignment','render_cache_token'):
        if field in edited:job[field]=copy.deepcopy(edited[field])
    if job.get('render_assignment') and not job.get('render_config_id'):
        record(data,job_id,capture(job,data))
    job.update(status='待生成',error='',render_checked=True,render_cache_token=uuid.uuid4().hex,
        render_from_snapshot=bool(job.get('render_assignment')))
    job.pop('render_timing',None)
    plans=[]
    for plan in result['render_plans']:
        if plan['profile_id']==pid and plan['status']=='staged':
            plan['ids']=[v for v in plan['ids'] if v!=job_id]
            plan['known_ids']=[v for v in plan.get('known_ids',[]) if v!=job_id]
            if not plan['ids']:continue
        plans.append(plan)
    plans.append(dict(id=uuid.uuid4().hex,profile_id=pid,name=profile['name'],ids=[job_id],
        known_ids=[v['id'] for v in data['jobs']],status='staged',task_id='',error='',retry_only=True))
    result['render_plans']=plans
    profile['data']=snapshot(data);profile['revision']=profile.get('revision',0)+1
    result['revision']+=1
    return result


def normalize(state):
    values=state.setdefault('render_configs',{})
    if not isinstance(values,dict):raise ValueError('任务配置快照格式异常，未覆盖原记录。')
    used={v.get('render_config_id') for v in state.get('jobs',[]) if v.get('render_config_id')}
    if any(key not in values or not isinstance(values[key],dict) for key in used):
        raise ValueError('任务配置快照缺失，未覆盖原记录。')
    state['render_configs']={key:value for key,value in values.items() if key in used}
