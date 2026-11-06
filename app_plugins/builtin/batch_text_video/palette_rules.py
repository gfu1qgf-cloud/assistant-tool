"""One deterministic colour choice per field and video, never per frame."""
import copy
import hashlib
import json
import math
import random
from qt_compat import QtGui
from .palette_library import canonical

KEY='color_rules'
ROOT_FIELDS={'color','outline_color','shadow_color','background_color','key_color','caption_cursor_color',
             'background_tint_color','title_color','body_color','caption_group_palette'}

def valid_path(path):
    if path in ROOT_FIELDS:return True
    parts=path.split('/')
    if len(parts)==2 and parts[0]=='caption_effects':
        from .caption_styles import COLOR_KEYS
        return parts[1] in COLOR_KEYS
    return False

def normalize(rules):
    if not isinstance(rules,dict) or len(rules)>64:raise ValueError('随机颜色参数过多或格式无效。')
    result={}
    for path,rule in rules.items():
        if not isinstance(path,str) or not valid_path(path) or not isinstance(rule,dict):raise ValueError('随机颜色的目标字段无效。')
        if rule.get('mode') not in {'palette','family'}:raise ValueError('颜色随机方式无效。')
        seed=rule.get('seed',0)
        if type(seed) is not int or not 0<=seed<=2147483647:raise ValueError('颜色种子须为0–2147483647。')
        if type(rule.get('alpha',False)) is not bool:raise ValueError('颜色透明通道开关无效。')
        value=dict(mode=rule['mode'],seed=seed,alpha=rule.get('alpha',False))
        if rule['mode']=='palette':
            colors=rule.get('colors')
            if not isinstance(colors,list) or not 1<=len(colors)<=32:raise ValueError('套餐随机至少需要一种颜色，最多32种。')
            value.update(colors=list(dict.fromkeys(canonical(c) for c in colors)),
                         name=str(rule.get('name','自选颜色'))[:60],palette_id=str(rule.get('palette_id',''))[:96])
            if path=='caption_group_palette' and len(value['colors'])<2:raise ValueError('字幕组轮换套餐至少需要两种不同颜色。')
        else:
            for key,default in (('hue',8.),('saturation',8.),('value',10.)):
                number=rule.get(key,default)
                if type(number) not in (int,float) or not math.isfinite(number) or not 0<=number<=30:raise ValueError('同色系变化范围须为0–30。')
                value[key]=number
        result[path]=value
    if len(json.dumps(result).encode())>48*1024:raise ValueError('颜色随机设置超过48KB。')
    return result

def set_rule(layer,path,rule):
    rules=copy.deepcopy(layer.get(KEY,{}))
    if rule is None:rules.pop(path,None)
    else:rules[path]=rule
    layer[KEY]=normalize(rules)

def merge_controls(layer,controls,path_for=lambda k:k):
    result=copy.deepcopy(layer.get(KEY,{}))
    for key,control in controls.items():
        if hasattr(control,'choice_rule'):
            path=path_for(key)
            if control.choice_rule is None:result.pop(path,None)
            else:result[path]=copy.deepcopy(control.choice_rule)
    return normalize(result)

def choose(value,rule,scope,identifier,path):
    values=value if isinstance(value,list) else str(value).split(',')
    values=[canonical(c,rule['mode']=='family' or rule.get('alpha',False)) for c in values]
    seed=hashlib.sha256(json.dumps(['palette-v1',str(scope),str(identifier),path,rule['seed']],ensure_ascii=False).encode()).digest()
    rng=random.Random(seed);result=[]
    palette=list(rule.get('colors',[]))
    if palette:rng.shuffle(palette)
    for index,base in enumerate(values):
        if rule['mode']=='palette':color=canonical(palette[index%len(palette)],rule['alpha'])
        else:
            original=QtGui.QColor(base);h,s,v,a=original.getHsvF()
            if h<0 or s<1/255:h,s=0,0
            else:
                h=(h+rng.uniform(-rule['hue'],rule['hue'])/360)%1
                s=min(1,max(0,s+rng.uniform(-rule['saturation'],rule['saturation'])/100))
            v=min(1,max(0,v+rng.uniform(-rule['value'],rule['value'])/100))
            sample=QtGui.QColor.fromHsvF(h,s,v,a);color=canonical(sample.name(sample.NameFormat.HexArgb),rule['alpha'] or original.alpha()!=255)
        result.append(color)
    return result if isinstance(value,list) else ','.join(result)

def resolve_object(value,scope='design'):
    result=dict(value);rules=normalize(result.pop(KEY,{}))
    if any(path.startswith('caption_effects/') for path in rules):
        result['caption_effects']=copy.deepcopy(value.get('caption_effects',{}))
    for path,rule in rules.items():
        parts=path.split('/');target=result
        if len(parts)==2:
            from .caption_styles import effects
            base=effects(result)[parts[1]]
            target=result.setdefault('caption_effects',{})
        else:base=result.get(path,'#ffffff')
        target[parts[-1]]=choose(base,rule,scope,result.get('id','settings'),path)
        if path=='color':result['color_mode']='fixed'
    return result

def resolve_settings(settings,scope):
    scope=settings.get('_palette_scope',scope)
    result=resolve_object(settings,scope)
    from .components import walk_components
    if any(layer.get(KEY) for layer in walk_components(settings.get('layers',[]))):
        result['layers']=copy.deepcopy(settings['layers'])
        for layer in walk_components(result['layers']):
            if layer.get(KEY):
                updated=resolve_object(layer,scope);layer.clear();layer.update(updated)
    return result

def node_scope(context):
    if getattr(context,'batch_color_scope',None):return context.batch_color_scope
    task,globals_value=getattr(context,'task',None),getattr(context,'workflow_globals',None)
    return f"node:{getattr(task,'directory','')}:{getattr(task,'key','')}:{getattr(globals_value,'iteration',1)}"

def palette_scope(job):
    if isinstance(job.get('_palette_scope'),str) and job['_palette_scope']:return job['_palette_scope']
    from .text_colors import job_color_scope
    token=job.get('quantity_key') or job.get('copy_id') or job.get('id') or ''
    return hashlib.sha256(json.dumps([job_color_scope(job),token],ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def node_palette_scope(context):return getattr(context,'batch_palette_scope',None) or node_scope(context)
