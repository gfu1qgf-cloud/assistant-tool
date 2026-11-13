"""Named global music libraries, excluded from project snapshots; each has a revision."""
import copy
import math

ID='shared:music'
KEY='shared_music_pools'
MAX_ITEMS=10000


class SharedMusicChanged(ValueError):
    """Another project reserved music; preparation can retry without failing the job."""


def fresh(name='共享背景音乐',identifier=ID):
    return dict(id=identifier,name=name,kind='music',revision=0,items=[],next_id='')


def is_shared(identifier):return isinstance(identifier,str) and (identifier==ID or identifier.startswith(ID+':'))


def normalize(state):
    pools=state.setdefault(KEY,[copy.deepcopy(state['shared_music'])] if 'shared_music' in state else [fresh()])
    if not isinstance(pools,list) or len(pools)>100:raise ValueError('共享音乐库列表异常。')
    ids,names=set(),set()
    for pool in pools:
        normalize_pool(pool)
        if pool['id'] in ids or pool['name'] in names:raise ValueError('共享音乐库名称或编号重复。')
        ids.add(pool['id']);names.add(pool['name'])
    return pools


def get(state,identifier=ID):
    pool=next((p for p in normalize(state) if p['id']==identifier),None)
    if pool is None:raise ValueError('共享音乐库已删除或不存在，请重新选择音乐来源。')
    return pool


def normalize_pool(pool):
    if (not isinstance(pool,dict) or not is_shared(pool.get('id')) or pool.get('kind')!='music'
            or not isinstance(pool.get('name'),str) or not pool['name'].strip() or len(pool['name'])>80
            or type(pool.get('revision')) is not int or pool['revision']<0 or not isinstance(pool.get('next_id'),str)
            or not isinstance(pool.get('items'),list) or len(pool['items'])>MAX_ITEMS):
        raise ValueError('共享音乐池记录异常，未清空原记录。')
    seen=set()
    for item in pool['items']:
        if (not isinstance(item,dict) or not isinstance(item.get('id'),str) or not item['id'] or item['id'] in seen
                or not isinstance(item.get('path'),str) or not item['path'] or len(item['path'])>4096):
            raise ValueError('共享音乐条目记录无效。')
        seen.add(item['id'])
        cursor=item.get('cursor',0)
        if type(cursor) not in (int,float) or not math.isfinite(cursor) or cursor<0:raise ValueError('共享音乐续播位置无效。')
    order=pool.get('original_order',[])
    if not isinstance(order,list) or len(order)>MAX_ITEMS or any(not isinstance(v,str) for v in order) or len(set(order))!=len(order):
        raise ValueError('共享音乐原始顺序记录无效。')
    return pool


def merge(requested,current):
    """Unrelated project edits always retain the latest global playhead."""
    live=copy.deepcopy(normalize(current));identifier=requested.pop('_shared_music_dirty',None)
    if identifier:
        wanted=get(requested,identifier);old=next((p for p in live if p['id']==identifier),None)
        if old is None:raise ValueError('共享音乐库已删除，请重新选择音乐来源。')
        live_pool=old
        if wanted['revision']!=live_pool['revision']:raise SharedMusicChanged('共享音乐已被其他项目使用，正在重新分配。')
        replacement=copy.deepcopy(wanted);replacement['revision']+=1
        live[live.index(old)]=replacement
    requested[KEY]=copy.deepcopy(live)
    return live


def add_paths(pool,paths):
    from pathlib import Path
    from .custom_pools import accepted_suffixes
    from .store import file_identity,add_paths as add_music
    known={file_identity(item['path']) for item in pool['items']};additions=[]
    for value in paths:
        path=Path(value).resolve();identity=file_identity(path)
        if identity not in known and path.is_file() and path.suffix.casefold() in accepted_suffixes('music'):
            additions.append(str(path));known.add(identity)
    if len(pool['items'])+len(additions)>MAX_ITEMS:raise ValueError(f'共享音乐池最多{MAX_ITEMS}首，未添加本批文件。')
    return add_music(pool['items'],additions,music=True)


def reserve(store,state,job_id,prepared):
    """Reserve before encoding, including node jobs whose other sources settle later."""
    job=next(j for j in state['jobs'] if j['id']==job_id)
    if job.get('shared_music_assignment'):
        prepared.music=None
        return state
    music=prepared.music
    if not music or not is_shared(music.get('pool_id')):return state
    value=copy.deepcopy(state);pool=get(value,music['pool_id'])
    if music.get('shared_revision')!=pool['revision']:raise SharedMusicChanged('共享音乐分配已更新。')
    advanced=store.completion_state(value,job_id,prepared.output,music)
    value[KEY]=advanced[KEY];value['_shared_music_dirty']=music['pool_id']
    selection=getattr(prepared,'assignment',{}).get('music')
    if not selection:raise ValueError('共享音乐缺少本次来源快照，未推进音乐池。')
    next(j for j in value['jobs'] if j['id']==job_id)['shared_music_assignment']=copy.deepcopy(selection)
    value=store.save(value,state['revision']);prepared.music=None
    return value
