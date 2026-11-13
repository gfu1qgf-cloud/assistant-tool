"""Independent caption appearance records, reserved once for each output video."""
import copy
import hashlib
import json

DEFAULTS={'caption_rotation_enabled':False,'caption_variants':[],'caption_variant_cursor':0}
MAX_STYLES=32
SHARED={'id','kind','name','enabled','source','source_file','text','path','thumbnail','caption_file',
        'caption_source','caption_timing','caption_language','caption_cloud_consent',
        'pool_id','pool_name','pool_field','copy_binding','sequence_items','sequence_cursor','show_start','show_end',
        'x','y','width','height','position_anchor_x','position_anchor_y','position_reference','unit'}


def snapshot(layer):
    return {key:copy.deepcopy(value) for key,value in layer.items()
            if key not in SHARED and key not in DEFAULTS and not key.startswith('_')}


def single(layer):
    value=copy.deepcopy(layer)
    value.update(caption_rotation_enabled=False,caption_variants=[],caption_variant_cursor=0)
    value.pop('_caption_variant_id',None);value.pop('_caption_variant_trial',None)
    return value


def effective(layer,entry):
    value=copy.deepcopy(layer)
    value.update(copy.deepcopy(entry['settings']))
    value['opacity']=layer.get('opacity',100)*entry['settings'].get('opacity',100)/100
    return value


def normalize(layer):
    for key,value in DEFAULTS.items():layer.setdefault(key,copy.deepcopy(value))
    if type(layer['caption_rotation_enabled']) is not bool:raise ValueError('字幕样式轮换开关无效。')
    entries=layer['caption_variants']
    if not isinstance(entries,list) or len(entries)>MAX_STYLES:raise ValueError('每个字幕控件最多32套轮换样式。')
    ids,names=set(),set()
    for entry in entries:
        if (not isinstance(entry,dict) or not isinstance(entry.get('id'),str) or not entry['id'] or len(entry['id'])>128
                or entry['id'] in ids or not isinstance(entry.get('name'),str) or not entry['name'].strip()
                or len(entry['name'])>80 or entry['name'] in names or not isinstance(entry.get('settings'),dict)):
            raise ValueError('字幕样式名称、编号或配置无效／重复。')
        ids.add(entry['id']);names.add(entry['name'])
        if any(not isinstance(k,str) or k in SHARED or k in DEFAULTS or k.startswith('_') for k in entry['settings']):
            raise ValueError('轮换样式只保存外观参数，文案及对齐来源由字幕控件共用。')
        # Validate each complete appearance with the existing component schema.
        # The child has an empty rotation list, so nested rotations cannot recur.
        from .layers import normalize_layers
        child=single(layer);child.update(entry['settings']);child['opacity']=entry['settings'].get('opacity',100)
        child=normalize_layers([child])[0]
        entry['settings']=snapshot(child)
    cursor=layer['caption_variant_cursor']
    if type(cursor) is not int or cursor<0:raise ValueError('字幕样式轮换位置无效。')
    layer['caption_variant_cursor']=cursor%len(entries) if entries else 0


def active(layer):
    return (layer.get('kind')=='caption' and layer.get('caption_rotation_enabled',False)
            and layer.get('enabled',True) and layer.get('opacity',100)>0)


def signature(layer):
    return hashlib.sha256(json.dumps(layer.get('caption_variants',[]),ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def resolve(layer):
    if not active(layer) or layer.get('_caption_variant_id'):return None
    entries=layer.get('caption_variants',[])
    if not entries:raise ValueError(layer.get('name','字幕')+'：请先添加轮换字幕样式，或关闭样式轮换。')
    cursor=layer.get('caption_variant_cursor',0)%len(entries)
    entry=entries[cursor]
    token=signature(layer)
    value=effective(layer,entry);layer.clear();layer.update(value)
    layer['_caption_variant_id']=entry['id']
    return dict(caption_style=True,id=layer['id'],cursor=cursor,next_cursor=(cursor+1)%len(entries),signature=token)


def commit(layers,steps):
    by_id={v['id']:v for v in layers};seen=set()
    for step in steps:
        layer=by_id.get(step['id'])
        if (not layer or step['id'] in seen or not active(layer) or signature(layer)!=step['signature']
                or layer.get('caption_variant_cursor',0)!=step['cursor'] or not layer.get('caption_variants')
                or step['next_cursor']!=(step['cursor']+1)%len(layer['caption_variants'])):
            raise ValueError('轮换字幕样式或位置已改变，未推进使用顺序。')
        seen.add(step['id']);layer['caption_variant_cursor']=step['next_cursor']


def bind(layer,state):
    from .cursor_pool import bind as bind_cursor,DEFAULTS as CURSOR_FIELDS
    for entry in layer.get('caption_variants',[]):
        child=effective(layer,entry);child['enabled']=active(layer);bind_cursor(child,state)
        entry['settings'].update({k:copy.deepcopy(child[k]) for k in CURSOR_FIELDS})


def frozen(layer):
    result=snapshot(layer)
    if result.get('caption_cursor_source') in {'pool','sequence'}:
        result.update(caption_cursor_source='file',caption_cursor_items=[],caption_cursor_pool_id='',caption_cursor_cursor=0)
    entry=next((v for v in layer.get('caption_variants',[]) if v['id']==layer.get('_caption_variant_id')),None)
    return dict(id=layer.get('_caption_variant_id'),name=entry['name'] if entry else '字幕样式',settings=result)


def apply_frozen(layer,value):
    layer.update(copy.deepcopy(value['settings']))
    layer.update(caption_rotation_enabled=False,caption_variants=[],caption_variant_cursor=0)
    layer.pop('_caption_variant_id',None)


def clipboard(layer):
    import uuid
    for entry in layer.get('caption_variants',[]):
        style=entry['settings']
        if style.get('caption_cursor_source')=='pool':
            style.update(caption_cursor_source='sequence',caption_cursor_pool_id='')
            for item in style.get('caption_cursor_items',[]):item['id']=uuid.uuid4().hex


def prepare_node(layer,services):
    from .cursor_pool import prepare_node as prepare_cursor
    value=copy.deepcopy(layer)
    if any(e['settings'].get('caption_cursor_source')=='pool' for e in value['caption_variants']):
        from .project_runtime import project_runtime
        bind(value,project_runtime(services)['data'])
    return {'caption_rotation':{k:copy.deepcopy(value[k]) for k in DEFAULTS},
            'style_cursors':{e['id']:prepare_cursor(single(effective(value,e)),services) for e in value['caption_variants']}}


def resolve_node(layer,context):
    if not active(layer):return
    prepared=getattr(context,'node_prepared',{})
    if 'caption_rotation' not in prepared:raise ValueError('字幕样式快照缺失，请重新提交节点。')
    layer.update(copy.deepcopy(prepared['caption_rotation']))
    owner=getattr(context,'workflow_input',{}).get('effect_layer',{})
    key=context.node_id+':style'
    old=owner.get('source_rotations',{}).get(key)
    entries=layer['caption_variants'];token=signature(layer)
    if old and old['signature']==token and entries:
        layer['caption_variant_cursor']=next((i for i,e in enumerate(entries) if e['id']==old['next_id']),old['cursor']%len(entries))
    step=resolve(layer)
    if owner and step:
        plans=getattr(context,'component_source_steps',{})
        if key in plans:raise ValueError('同一轮换字幕在节点循环中重复执行，请拆分控件。')
        plans[key]=dict(node_source=True,id=owner['id'],workflow_id=owner['workflow_id'],node_id=key,before=copy.deepcopy(old),
            after=dict(signature=token,cursor=step['next_cursor'],next_id=entries[step['next_cursor']]['id']))
        context.component_source_steps=plans
