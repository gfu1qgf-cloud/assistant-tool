"""Opt-in file exclusion between visual pools within one composition."""
import copy

FIELD = '_pool_exclusion'


def choices(state):
    result = {'builtin:background':'默认背景池','builtin:image':'默认图片池'}
    result.update({p['id']:p['name'] for p in state.get('resource_pools',[]) if p['kind'] in {'image','video','visual'}})
    return result


def pool_key(key):
    if key == 'backgrounds': return 'builtin:background'
    if key == 'image_pool': return 'builtin:image'
    return key[5:] if key and key.startswith('pool:') else ''


def peers(settings):
    rules = settings.get('pool_exclusions', {})
    if not isinstance(rules,dict) or len(rules)>102: raise ValueError('素材池互斥设置无效。')
    edges = {}
    for key,rule in rules.items():
        if (not isinstance(key,str) or not key or not isinstance(rule,dict)
            or type(rule.get('enabled',False)) is not bool or not isinstance(rule.get('pools',[]),list)
            or len(rule.get('pools',[]))>102 or any(not isinstance(v,str) or not v or v==key for v in rule.get('pools',[]))):
            raise ValueError('素材池互斥设置无效。')
        if rule.get('enabled'):
            for other in rule.get('pools',[]):
                edges.setdefault(key,set()).add(other);edges.setdefault(other,set()).add(key)
    return edges


def attach(layers,state):
    from .components import walk_components
    edges=peers(state.get('settings',{}))
    if not edges and not any(FIELD in v for v in walk_components(layers)):return layers
    result=copy.deepcopy(layers);names=choices(state)
    members = {}
    def pool_paths(identifier):
        if identifier not in members:
            items = state.get('image_pool', []) if identifier=='builtin:image' else next(
                (p['items'] for p in state.get('resource_pools',[]) if p['id']==identifier), [])
            members[identifier] = {v['path'] for v in items}
        return members[identifier]

    for layer in walk_components(result):
        previous=layer.pop(FIELD,None)
        layer.pop('_pool_exclusion_selected_cursor',None)
        if layer.get('kind')!='image':continue
        origin=layer.get('pool_id') or ('builtin:image' if layer.get('source')=='pool' or layer.get('pool_name') else '')
        if origin not in edges:continue
        if layer.get('source') not in {'pool','sequence'}:
            path = layer.get('source_file') if layer.get('source')=='file' else layer.get('path')
            # A manually replaced fixed file no longer belongs to its stale
            # former pool. Frozen task assignments are applied after attach.
            if path not in pool_paths(origin):continue

        layer[FIELD]=dict(pool=origin,peers=sorted(edges[origin]),name=names.get(origin,layer.get('pool_name',origin)),
            background_pool=state.get('settings',{}).get('background_pool_id',''))
    return result


def allocate(layers,job=None):
    """Read-only domains, rotated by each cursor; bounded joint search avoids greedy false failures."""
    slots=[]
    def walk(values):
        for layer in values:
            if not layer.get('enabled',True) or not layer.get('opacity',100):continue
            if layer.get('kind')=='watermark':yield from walk(layer.get('corners',{}).values())
            else:yield layer
    for layer in walk(layers):
        rule=layer.get(FIELD)
        if layer.get('kind')!='image' or not rule:continue
        if layer.get('source') in {'pool','sequence'}:
            items=layer.get('sequence_items',[])
            if not items:continue  # Normal source validation supplies its existing actionable error.
            cursor=layer.get('sequence_cursor',0)%len(items)
            domain=[((cursor+i)%len(items),items[(cursor+i)%len(items)]['path']) for i in range(len(items))]
        else:
            path=layer.get('source_file') if layer.get('source')=='file' else layer.get('path')
            if not path:continue
            domain=[(None,path)]
        slots.append(dict(layer=layer,rule=rule,domain=domain))
    if not slots:return
    bg=next((s['rule'].get('background_pool') for s in slots if s['rule'].get('background_pool') not in {'',None,'none'}),'')
    if bg and (job or {}).get('path'):
        related={s['rule']['pool'] for s in slots if bg in s['rule']['peers']}
        if related:slots.append(dict(layer=None,rule=dict(pool=bg,peers=related,name='底层背景'),domain=[(None,job['path'])]))
    participating={s['rule']['pool'] for s in slots}
    slots=[s for s in slots if participating.intersection(s['rule']['peers'])]
    if not slots:return
    slots.sort(key=lambda s:len(s['domain']))
    from .store import file_identity
    identities={};assigned=[];attempts=0
    def identity(path):
        if path not in identities:identities[path]=file_identity(path)
        return identities[path]
    def visit(index):
        nonlocal attempts
        if index==len(slots):return True
        slot=slots[index];seen=set()
        for cursor,path in slot['domain']:
            attempts+=1
            if attempts>20000:raise ValueError('素材互斥组合过多，未开始渲染；请减少互斥范围或增加不同素材。')
            token=identity(path)
            if token in seen:continue
            seen.add(token)
            if any(token==other_token and (other['rule']['pool'] in slot['rule']['peers']
                or slot['rule']['pool'] in other['rule']['peers']) for other,_,other_token in assigned):continue
            assigned.append((slot,cursor,token))
            if visit(index+1):return True
            assigned.pop()
        return False
    if not visit(0):
        names='、'.join(dict.fromkeys(s['rule']['name'] for s in slots))
        raise ValueError('互斥素材不足或固定素材重复：'+names+'。请增加不同素材、调整互斥设置；已固定的任务可右键重新分配素材。')
    for slot,cursor,_ in assigned:
        if cursor is not None:slot['layer']['_pool_exclusion_selected_cursor']=cursor


def freeze_resolved(layers):
    from .components import walk_components
    for layer in walk_components(layers):
        if layer.get(FIELD) and layer.get('kind')=='image' and layer.get('source') in {'pool','sequence'}:
            layer.update(source='fixed', sequence_items=[], sequence_cursor=0)
    return layers
