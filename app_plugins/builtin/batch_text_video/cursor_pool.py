"""Per-caption image rotation; selection is read-only until export reservation."""
import copy
import hashlib
import json
from pathlib import Path

DEFAULTS={'caption_cursor_source':'file','caption_cursor_pool_id':'',
          'caption_cursor_items':[],'caption_cursor_cursor':0}


def normalize(layer):
    for key,value in DEFAULTS.items():layer.setdefault(key,copy.deepcopy(value))
    if not isinstance(layer['caption_cursor_source'],str) or layer['caption_cursor_source'] not in {'file','pool','sequence'}:
        raise ValueError('图片光标来源无效。')
    if not isinstance(layer['caption_cursor_pool_id'],str) or len(layer['caption_cursor_pool_id'])>128:
        raise ValueError('图片光标资源池编号无效。')
    items=layer['caption_cursor_items']
    if not isinstance(items,list) or len(items)>1000:raise ValueError('图片光标来源最多1000项。')
    seen=set()
    for item in items:
        if (not isinstance(item,dict) or not isinstance(item.get('id'),str) or not item['id'] or item['id'] in seen
                or not isinstance(item.get('path'),str) or not item['path'] or len(item['path'])>4096):
            raise ValueError('图片光标来源条目无效。')
        seen.add(item['id'])
    cursor=layer['caption_cursor_cursor']
    if type(cursor) is not int or cursor<0:raise ValueError('图片光标轮换位置无效。')
    layer['caption_cursor_cursor']=cursor%len(items) if items else 0


def enabled(layer):
    # The source bridge also uses minimal caption records without any style.
    # It must not load/validate a display preset merely to obtain subtitles.
    return (layer.get('kind')=='caption' and layer.get('enabled',True) and layer.get('opacity',100)>0
            and layer.get('caption_cursor',False) and layer.get('caption_display_mode')=='char'
            and layer.get('caption_cursor_mode','bar')=='image' and layer.get('caption_cursor_opacity',100)>0)


def bind(layer,state):
    normalize(layer)
    if layer['caption_cursor_source']!='pool':return
    from .custom_pools import get_pool
    identifier=layer['caption_cursor_pool_id']
    try:
        pool=get_pool(state,identifier) if identifier else None
        if pool and pool['kind'] not in {'image','video','visual'}:raise ValueError('图片光标需要视觉素材池。')
    except ValueError:
        if enabled(layer):raise
        return
    old=layer['caption_cursor_items'];cursor=layer['caption_cursor_cursor']
    next_id=old[cursor%len(old)]['id'] if old else None
    from .layers import IMAGE_SUFFIXES
    items=pool['items'] if pool else state.get('image_pool',[])
    layer['caption_cursor_items']=[{key:copy.deepcopy(item[key]) for key in ('id','path','name','thumbnail') if key in item}
        for item in items if Path(item['path']).suffix.casefold() in IMAGE_SUFFIXES|{'.gif'}]
    items=layer['caption_cursor_items']
    layer['caption_cursor_cursor']=next((i for i,item in enumerate(items) if item['id']==next_id),cursor%len(items) if items else 0)


def signature(layer):
    return hashlib.sha256(json.dumps([layer.get(k,v) for k,v in DEFAULTS.items() if k!='caption_cursor_cursor'],
        ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def resolve(layer):
    normalize(layer)
    if not enabled(layer) or layer['caption_cursor_source']=='file':return None
    items=layer['caption_cursor_items'];cursor=layer['caption_cursor_cursor']
    if not items:raise ValueError(layer.get('name','字幕')+'：光标素材池没有可用图片，请添加图片或改用固定图片。')
    layer['caption_cursor_image']=items[cursor]['path']
    return dict(cursor_source=True,id=layer['id'],cursor=cursor,next_cursor=(cursor+1)%len(items),signature=signature(layer))


def commit(layers,steps):
    by_id={v['id']:v for v in layers};seen=set()
    for step in steps:
        layer=by_id.get(step['id'])
        if (layer is None or step['id'] in seen or not enabled(layer)
                or layer.get('caption_cursor_source') not in {'pool','sequence'}
                or signature(layer)!=step['signature'] or layer['caption_cursor_cursor']!=step['cursor']
                or not layer['caption_cursor_items'] or step['next_cursor']!=(step['cursor']+1)%len(layer['caption_cursor_items'])):
            raise ValueError('图片光标来源或位置已改变，未推进轮换。')
        seen.add(step['id']);layer['caption_cursor_cursor']=step['next_cursor']


def virtual(layer):
    return dict(id=layer['id']+':cursor',kind='image',source='pool',pool_id=layer.get('caption_cursor_pool_id',''),
        name=layer.get('name','字幕')+' · 光标',pool_name='光标图片池',_cursor_owner=layer['id'],
        sequence_items=layer.get('caption_cursor_items',[]),sequence_cursor=layer.get('caption_cursor_cursor',0))


def prepare_node(layer,services):
    if not enabled(layer) or layer.get('caption_cursor_source')=='file':return {}
    from dataclasses import asdict
    from model.node_workflow.contracts import ResourceRef
    value=copy.deepcopy(layer)
    if value['caption_cursor_source']=='pool':
        from .project_runtime import project_runtime
        bind(value,project_runtime(services)['data'])
    return {'cursor_source':{key:copy.deepcopy(value[key]) for key in DEFAULTS},
            'cursor_files':[asdict(ResourceRef.capture(item['path'],'caption_cursor')) for item in value['caption_cursor_items']]}


def resolve_node(layer,context):
    if not enabled(layer) or layer.get('caption_cursor_source')=='file':return
    prepared=getattr(context,'node_prepared',{})
    if 'cursor_source' not in prepared:raise ValueError('光标来源快照缺失，请重新提交节点。')
    layer.update(copy.deepcopy(prepared['cursor_source']))
    owner=getattr(context,'workflow_input',{}).get('effect_layer',{})
    key=context.node_id+':cursor' if owner else ''
    old=owner.get('source_rotations',{}).get(key)
    token=signature(layer);items=layer['caption_cursor_items']
    if old and old['signature']==token and items:
        layer['caption_cursor_cursor']=next((i for i,item in enumerate(items) if item['id']==old['next_id']),old['cursor']%len(items))
    step=resolve(layer)
    from model.node_workflow.contracts import ResourceRef
    refs=[ResourceRef(**v) for v in prepared.get('cursor_files',[])]
    selected=next((r for r in refs if r.path==str(Path(layer['caption_cursor_image']).resolve())),None)
    if selected is None:raise ValueError('光标图片不在本次节点资源快照中。')
    selected.verify();context.component_source_refs=getattr(context,'component_source_refs',[])+[selected]
    if owner and step:
        plans=getattr(context,'component_source_steps',{})
        if key in plans:raise ValueError('同一光标来源在循环中重复执行，请拆分字幕控件。')
        plans[key]=dict(node_source=True,id=owner['id'],workflow_id=owner['workflow_id'],node_id=key,before=copy.deepcopy(old),
            after=dict(signature=token,cursor=step['next_cursor'],next_id=items[step['next_cursor']]['id']))
        context.component_source_steps=plans
