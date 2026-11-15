"""Offline, additive Firefox container editing, scoped to an explicit Profile.

Only containers.json is modified. Existing identifiers/unknown fields are kept.
The real Windows parent.lock is held across re-read, backup and atomic commit.
No browser termination, extension installation, cookie or password access.
"""
from contextlib import contextmanager
import configparser
import copy
import ctypes
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from model.AppDataStorage import application_storage,data_file
from model.PrivateDataCatalog import no_reparse

MAX_ID=2**32-1  # Firefox reserves this value for extension storage.
VERSIONS=frozenset(range(3,9))
COLORS=('blue','turquoise','green','yellow','orange','red','pink','purple')
MODERN_COLORS=('blue','cyan','green','yellow','orange','red','pink','purple')
ICONS=('briefcase','fingerprint','dollar','cart','circle','vacation','gift','food','fruit','pet','tree','chill','fence')


def profile_key(path):return hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode('utf8')).hexdigest()


def _profile_path(path):
    path=Path(os.path.abspath(path))
    no_reparse(path,Path(path.anchor))
    if not path.is_dir():raise ValueError('目标Profile目录不存在，请核对完整路径。')
    if not any((path/name).is_file() for name in ('prefs.js','compatibility.ini','containers.json')):
        raise ValueError('该目录没有Firefox Profile标记，请选择实际的Profile目录。')
    return path


def _read(path,limit=4*1024*1024):
    no_reparse(path,Path(path.anchor))
    with data_file(path).open('rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('配置文件过大，未修改。')
    return raw


def _pairs(values):
    result={}
    for key,value in values:
        if key in result:raise ValueError('容器配置包含重复字段，未覆盖原文件。')
        result[key]=value
    return result


def _invalid_number(_value):raise ValueError('容器配置包含非法数值。')


def _integer(value):return type(value) is int


def validate(value):
    if not isinstance(value,dict) or type(value.get('version')) is not int or value['version'] not in VERSIONS:
        raise ValueError('容器配置版本未知；为防止Firefox重置容器，未修改。')
    identities=value.get('identities');last=value.get('lastUserContextId')
    if not isinstance(identities,list) or not _integer(last) or not 0<=last<MAX_ID:
        raise ValueError('容器列表或编号记录无效，未修改。')
    used=set()
    for identity in identities:
        if not isinstance(identity,dict):raise ValueError('容器条目格式无效。')
        number=identity.get('userContextId')
        if not _integer(number) or not 0<number<=MAX_ID or number in used:
            raise ValueError('已有容器编号无效或重复，未修改原配置。')
        if type(identity.get('public')) is not bool:raise ValueError('已有容器可见性字段无效。')
        if identity.get('public') and (number==MAX_ID or not isinstance(identity.get('name',identity.get('l10nId')),str)):
            raise ValueError('已有公开容器身份无效。')
        used.add(number)
    return value


def snapshot(path):
    root=_profile_path(path);target=root/'containers.json'
    try:raw=_read(target)
    except FileNotFoundError:
        raise ValueError('此Profile尚未初始化容器配置；请先在该Profile启用容器标签页，再刷新。') from None
    try:value=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=_pairs,parse_constant=_invalid_number)
    except (UnicodeError,json.JSONDecodeError) as error:raise ValueError('容器配置不是有效JSON，未覆盖。') from error
    validate(value)
    visible=[]
    defaults={1:'个人',2:'工作',3:'银行',4:'购物'}
    for item in value['identities']:
        if item['public']:
            visible.append({'id':item['userContextId'],'name':item.get('name') or defaults.get(item['userContextId']) or item.get('l10nId',''),
                            'color':item.get('color',''),'icon':item.get('icon','')})
    return {'path':str(root),'sha256':hashlib.sha256(raw).hexdigest(),'raw':raw,'data':value,'containers':visible}


def discover_profiles(root=None,extra=(),check=lambda:None):
    if root is None:
        roaming=os.environ.get('APPDATA')
        root=Path(roaming)/'Mozilla'/'Firefox' if roaming else None
    else:root=Path(root)
    profiles={};warnings=[];installed=set()
    def add(path,name,default=False):
        path=Path(os.path.abspath(path));key=profile_key(path)
        if key in profiles:
            profiles[key]['default']|=default;return
        item={'key':key,'name':name or path.name,'path':str(path),'default':default,'count':None,'status':'','error':''}
        try:
            state=snapshot(path);item['count']=len(state['containers']);item['status']='可读取'
        except (OSError,ValueError) as error:item.update(status='需检查',error=str(error))
        profiles[key]=item
    if root is not None:
        check();ini=root/'profiles.ini'
        if ini.is_file():
            parser=configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(_read(ini,2*1024*1024).decode('utf-8-sig'))
                for section in parser.sections():
                    if section.startswith('Install') and parser.has_option(section,'Default'):
                        installed.add(profile_key(root/parser.get(section,'Default')))
                for section in parser.sections():
                    check()
                    if not section.startswith('Profile') or not parser.has_option(section,'Path'):continue
                    path=Path(parser.get(section,'Path'))
                    if parser.getboolean(section,'IsRelative',fallback=True):path=root/path
                    elif not path.is_absolute():raise ValueError('绝对Profile路径不是绝对路径。')
                    add(path,parser.get(section,'Name',fallback=section),profile_key(path) in installed or parser.getboolean(section,'Default',fallback=False))
            except (OSError,ValueError,configparser.Error,UnicodeError) as error:warnings.append('profiles.ini读取异常：'+str(error))
        directory=root/'Profiles'
        if directory.is_dir():
            for index,path in enumerate(sorted(directory.iterdir())):
                check()
                if index>=2000:warnings.append('Profile目录过多，请按目录分批添加。');break
                if path.is_dir() and any((path/name).is_file() for name in ('containers.json','prefs.js','compatibility.ini')):
                    add(path,path.name,profile_key(path) in installed)
    for path in extra:check();add(path,Path(path).name)
    return {'profiles':sorted(profiles.values(),key=lambda item:(item['name'].casefold(),item['path'].casefold())),'warnings':warnings}


def numbered_names(prefix,start,count,digits=2):
    if not isinstance(prefix,str) or not prefix.strip():raise ValueError('请填写容器名称前缀。')
    if not all(_integer(v) for v in (start,count,digits)) or start<0 or not 1<=count<=1000 or not 1<=digits<=8:
        raise ValueError('编号或数量无效，每次可创建1至1000个。')
    return [prefix.strip()+str(index).zfill(digits) for index in range(start,start+count)]


def build_plan(state,names,*,color='auto',icon='briefcase'):
    if not isinstance(names,(list,tuple)) or not 1<=len(names)<=1000:raise ValueError('请填写1至1000个容器名称。')
    if icon not in ICONS:raise ValueError('容器图标无效。')
    palette=MODERN_COLORS if state['data']['version']>=6 else COLORS
    if color!='auto' and color not in palette:raise ValueError('当前Firefox配置不支持所选颜色。')
    value=copy.deepcopy(state['data']);used={item['userContextId'] for item in value['identities']}
    known={item.get('name','').strip().casefold() for item in value['identities'] if item['public']}
    known.update(item['name'].strip().casefold() for item in state['containers'])
    current=max([value['lastUserContextId'],*[number for number in used if number<MAX_ID]])
    added=[];skipped=[]
    for name in names:
        if not isinstance(name,str):raise ValueError('容器名称必须为文字。')
        name=name.strip()
        if not name or len(name)>120 or any(ord(c)<32 for c in name):raise ValueError('容器名称不能为空、包含控制字符或超过120字。')
        key=name.casefold()
        if key in known:skipped.append(name);continue
        known.add(key);current+=1
        if current>=MAX_ID:raise ValueError('容器编号已达Firefox上限。')
        item={'userContextId':current,'public':True,'name':name,'icon':icon,'color':palette[len(added)%len(palette)] if color=='auto' else color}
        value['identities'].append(item);added.append(copy.deepcopy(item))
    if added:value['lastUserContextId']=current
    return {'path':state['path'],'expected_sha':state['sha256'],'added':added,'skipped':skipped,'data':value}


@contextmanager
def profile_lock(path):
    """Use Firefox's own exclusive lock; stale parent.lock files are not busy."""
    if os.name!='nt':raise ValueError('当前无扩展写入器仅支持Windows，未修改Profile。')
    root=_profile_path(path);target=no_reparse(root/'parent.lock',root)
    application_storage().check(target,write=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
    kernel.CreateFileW.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=[ctypes.c_void_p];kernel.CloseHandle.restype=ctypes.c_int
    handle=kernel.CreateFileW(str(target),0xC0000000,0,None,4,0x80,None)
    if handle==ctypes.c_void_p(-1).value:
        error=ctypes.get_last_error()
        if error in (32,33):raise ValueError('这个Profile仍被Firefox或其他程序占用。请退出使用此Profile的Firefox后重试；其他Profile可以保持打开。')
        raise OSError(error,'无法独占锁定目标Profile，未修改容器。')
    try:yield
    finally:kernel.CloseHandle(handle)


def create_containers(path,names,*,expected_sha,color='auto',icon='briefcase',check=lambda:None):
    check()
    with profile_lock(path):
        check();state=snapshot(path)
        if state['sha256']!=expected_sha:raise ValueError('容器配置已在预览后变化，请刷新目标Profile再确认；未覆盖。')
        plan=build_plan(state,names,color=color,icon=icon)
        if not plan['added']:return {**{key:plan[key] for key in ('path','added','skipped')},'committed':False,'backup':''}
        target=Path(state['path'])/'containers.json'
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup=no_reparse(target.with_name('containers.json.assistant-'+stamp+'-'+uuid4().hex[:8]+'.bak'),target.parent)
        check()
        with data_file(backup).open('xb') as stream:
            stream.write(state['raw']);stream.flush();os.fsync(stream.fileno())
        if _read(backup)!=state['raw']:raise ValueError('备份校验失败，未修改原容器配置。')
        check()
        with data_file(target).atomic_writer(encoding='utf-8',prefix='.containers-assistant-') as stream:
            stream.write(json.dumps(plan['data'],ensure_ascii=False,indent=2)+'\n')
            check()
            if _read(target)!=state['raw']:raise ValueError('写入前检测到配置变化，已取消覆盖。')
        return {'path':state['path'],'added':plan['added'],'skipped':plan['skipped'],'backup':str(backup),'committed':True}


def run_action(payload,context):
    action=payload['action'];context.check_canceled()
    if action=='scan':return {'action':action,**discover_profiles(extra=payload.get('extra',()),check=context.check_canceled)}
    if action=='inspect':
        state=snapshot(payload['path']);state.pop('raw')
        return {'action':action,'state':state}
    if action=='create':
        result=create_containers(payload['path'],payload['names'],expected_sha=payload['expected_sha'],
            color=payload.get('color','auto'),icon=payload.get('icon','briefcase'),check=context.check_canceled)
        return {'action':action,**result}
    raise ValueError('未知Firefox容器操作。')
