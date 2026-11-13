"""Replace one original visual file within a project; restage only its users."""
import copy
import hashlib
import json
from pathlib import Path
import uuid

VISUAL_KINDS={'image','video','visual'}


def libraries(data):
    yield 'backgrounds',data.get('backgrounds',[])
    yield 'image_pool',data.get('image_pool',[])
    for pool in data.get('resource_pools',[]):
        if pool['kind'] in VISUAL_KINDS:yield 'pool:'+pool['id'],pool['items']


def selected(data,key,identifier):
    items=next((items for name,items in libraries(data) if name==key),None)
    if items is None:raise ValueError('请在图片、背景或自定义视觉池中替换素材。')
    item=next((v for v in items if (v if isinstance(v,str) else v['id'])==identifier),None)
    if item is None:raise ValueError('素材记录已变化，请重新选择。')
    return item if isinstance(item,str) else item['path']


class Replacement:
    def __init__(self,old,new,data):
        from .store import file_identity
        self.identity=file_identity;self.old=old;self.new=str(new);self.memo={}
        self.old_key=file_identity(old);self.new_key=file_identity(new)
        self.pools={name:items for name,items in libraries(data)}
        self.needs_image=False

    def matches(self,path):
        if not path:return False
        if path not in self.memo:self.memo[path]=self.identity(path)
        return self.memo[path]==self.old_key

    def path(self,item,field='path'):
        if self.matches(item.get(field,'')):
            item[field]=self.new
            if field=='path':item.pop('thumbnail',None)
            return True
        return False

    def patch_layer(self,layer):
        if layer.get('kind')=='image':
            changed=self.path(layer)
            for item in layer.get('sequence_items',[]):self.path(item)
            if changed:layer.pop('_matting_binding',None)
        if self.path(layer,'caption_cursor_image'):self.needs_image=True
        for item in layer.get('caption_cursor_items',[]):
            if self.path(item):self.needs_image=True
        for entry in layer.get('caption_variants',[]):self.patch_layer(entry['settings'])
        for corner in layer.get('corners',{}).values():self.patch_layer(corner)

    def patch_settings(self,settings):
        for layer in settings.get('layers',[]):self.patch_layer(layer)

    def patch_background_record(self,item):
        self.path(item)
        pair=item.get('pair_key')
        if isinstance(pair,(list,tuple)) and len(pair)>=2 and self.matches(pair[1]):
            pair=list(pair);pair[1]=self.new_key;item['pair_key']=pair

    def layers(self,layers):
        for layer in layers:
            if not layer.get('enabled',True) or not layer.get('opacity',100):continue
            if layer.get('kind')=='watermark':yield from self.layers(layer.get('corners',{}).values())
            else:yield layer

    def pool_paths(self,identifier):
        items=self.pools.get(identifier,[])
        return (v if isinstance(v,str) else v['path'] for v in items)

    def background(self,job,settings,index):
        pool=settings.get('background_pool_id','')
        if pool=='none':return ''
        if not pool:return job.get('path','')
        key='backgrounds' if pool=='builtin:background' else 'pool:'+pool
        paths=list(self.pool_paths(key))
        return paths[job.get('_background_pool_position',index)%len(paths)] if paths else ''

    def uses(self,job,settings,index):
        assigned=job.get('render_assignment')
        background=assigned.get('background','') if assigned else self.background(job,settings,index)
        found=self.matches(background)
        sources=(assigned or {}).get('sources',{})
        styles=(assigned or {}).get('caption_styles',{})
        cursors=(assigned or {}).get('cursor_sources',{})
        for original in self.layers(settings.get('layers',[])):
            layer=original
            if original['id'] in styles:layer={**original,**styles[original['id']]['settings']}
            if not layer.get('opacity',100):continue
            if layer['kind']=='image':
                fixed=sources.get(layer['id'])
                if fixed and fixed.get('field')=='path':paths=[fixed['value']]
                elif layer.get('source')=='pool':paths=self.pool_paths('pool:'+layer['pool_id'] if layer.get('pool_id') else 'image_pool')
                elif layer.get('source')=='sequence':paths=[v['path'] for v in layer.get('sequence_items',[])]
                else:paths=[layer.get('path','')]
                found=any(self.matches(path) for path in paths) or found
            if layer['kind']=='caption':
                from .caption_cursor import image_enabled
                if not image_enabled(layer):continue
                if layer['id'] in cursors:paths=[cursors[layer['id']]]
                elif layer.get('caption_cursor_source')=='pool':paths=self.pool_paths('pool:'+layer['caption_cursor_pool_id'] if layer.get('caption_cursor_pool_id') else 'image_pool')
                elif layer.get('caption_cursor_source')=='sequence':paths=[v['path'] for v in layer.get('caption_cursor_items',[])]
                else:paths=[layer.get('caption_cursor_image','')]
                if any(self.matches(path) for path in paths):self.needs_image=True;found=True
        return found

    def patch_assignment(self,assignment):
        self.path(assignment,'background')
        for source in assignment.get('sources',{}).values():
            if source.get('field')=='path' and self.matches(source.get('value','')):source['value']=self.new
        for identifier,path in assignment.get('cursor_sources',{}).items():
            if self.matches(path):assignment['cursor_sources'][identifier]=self.new;self.needs_image=True
        for style in assignment.get('caption_styles',{}).values():self.patch_layer(style['settings'])


def replace_project(data,key,identifier,new_path,*,expected_source=None):
    old=selected(data,key,identifier)
    from .store import file_identity
    if expected_source is not None and file_identity(expected_source)!=file_identity(old):
        raise ValueError('素材已被其他窗口替换，请刷新后重试。')
    target=Path(new_path).resolve()
    from .visual_media import VISUAL_SUFFIXES
    if not target.is_file() or target.suffix.casefold() not in VISUAL_SUFFIXES:
        raise ValueError('请选择存在的图片、视频或GIF文件。')
    replacement=Replacement(old,target,data)
    # Keep IDs and order; never silently merge two usage records in one pool.
    for name,items in libraries(data):
        paths=[v if isinstance(v,str) else v['path'] for v in items]
        if any(replacement.matches(p) for p in paths) and replacement.old_key!=replacement.new_key:
            if any(file_identity(p)==replacement.new_key for p in paths):
                raise ValueError('替换文件已存在于同一素材池，请选择其他文件；原记录未更改。')
    result=copy.deepcopy(data)
    affected=[];legacy=[];config_updates={};configs=result.setdefault('render_configs',{})
    from .task_render_config import capture,normalize
    from .node_effects import active_effects
    fallback=capture({},data)['settings']

    def register(settings):
        value=copy.deepcopy(settings);replacement.patch_settings(value)
        digest=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf8')).hexdigest()
        configs.setdefault(digest,value)
        return digest

    for index,job in enumerate(result['jobs']):
        config_id=job.get('render_config_id')
        settings=data.get('render_configs',{}).get(config_id,data['settings'])
        uses=replacement.uses(job,settings,index)
        # Row background paths remain current even when its frozen assignment wins.
        replacement.patch_background_record(job)
        if not uses:continue
        if active_effects(settings) or active_effects(data['settings']):
            raise ValueError('受影响任务包含节点工作流，暂不能保证完整原配置；本次替换未保存，请在项目中调整后导出。')
        assigned=job.get('render_assignment')
        if assigned:
            replacement.patch_assignment(assigned)
            cache_key=config_id or '@legacy'
            if cache_key not in config_updates:config_updates[cache_key]=register(settings if config_id else fallback)
            job['render_config_id']=config_updates[cache_key]
            if not config_id:
                job['render_content']={k:copy.deepcopy(job[k]) for k in ('title','body','requires_title','caption_subtitles') if k in job}
                legacy.append(job['id'])
            job['render_from_snapshot']=True
        else:
            job.pop('render_from_snapshot',None)
            if job.get('status')=='已完成':legacy.append(job['id'])
        job.update(status='待生成',error='',render_checked=True,render_dirty=True,
            render_dirty_reason='素材已替换：'+Path(old).name+' → '+target.name,render_cache_token=uuid.uuid4().hex)
        job.pop('render_timing',None)
        affected.append(job['id'])
    replacement.patch_settings(result['settings'])
    updated_records=0
    for name,items in libraries(result):
        if name=='backgrounds':
            for i,path in enumerate(items):
                if replacement.matches(path):items[i]=str(target);updated_records+=1
        else:
            for item in items:
                previous=item['path']
                if replacement.path(item):
                    if item.get('name') in {Path(previous).name,Path(previous).stem}:item['name']=target.name
                    updated_records+=1
    for item in result.get('copy_pool',[]):replacement.patch_background_record(item)
    # Background usage tokens are paths; other pools retain their item IDs.
    if 'backgrounds' in result.get('resource_orders',{}):
        result['resource_orders']['backgrounds']=[str(target) if replacement.matches(v) else v for v in result['resource_orders']['backgrounds']]
    from .layers import IMAGE_SUFFIXES
    if replacement.needs_image and target.suffix.casefold() not in IMAGE_SUFFIXES:
        raise ValueError('此素材还用于图片光标，请选择图片文件；原记录未更改。')
    normalize(result)
    return result,dict(affected_ids=affected,legacy_ids=legacy,updated_records=updated_records,old=old,new=str(target))


def replace(state,profile_id,key,identifier,path,*,expected_revision,expected_source=None):
    from .queue_selection import ACTIVE,apply_checks
    profile=next((p for p in state['profiles'] if p['id']==profile_id),None)
    if profile is None or profile.get('revision',0)!=expected_revision:
        raise ValueError('项目已被其他窗口修改，请刷新后重新替换。')
    if any(p['profile_id']==profile_id and p['status'] in ACTIVE for p in state['render_plans']):
        raise ValueError('此项目正在渲染，请结束本批后替换素材。')
    from .project_migration import profile_data
    from .store import DEFAULTS
    from .pools import snapshot
    data=profile_data(state,profile_id,DEFAULTS)
    updated,info=replace_project(data,key,identifier,path,expected_source=expected_source)
    result=copy.deepcopy(state)
    selected_profile=next(p for p in result['profiles'] if p['id']==profile_id)
    selected_profile['data']=snapshot(updated)
    if result['active_profile']==profile_id:result.update(updated)
    if info['affected_ids']:
        result,_=apply_checks(result,{profile_id:dict.fromkeys(info['affected_ids'],True)})
        for plan in result['render_plans']:
            if plan['profile_id']==profile_id and plan['status']=='staged' and set(plan['ids'])&set(info['affected_ids']):
                plan['error']='';plan.pop('render_timing',None)
    selected_profile=next(p for p in result['profiles'] if p['id']==profile_id)
    selected_profile['revision']=expected_revision+1
    result['revision']=state['revision']+1
    return result,info
