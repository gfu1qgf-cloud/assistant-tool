"""Host-owned local bookmark import, verified encrypted backups and cleanup.

No history/cookies/password import, no network calls, no browser process kills.
Firefox full Places recovery snapshots are only made inside protected storage.
"""

from model.AppDataStorage import data_file
import configparser
import copy
import ctypes as C
from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from .AppDataStorage import application_storage, data_file
from .PrivateDataCatalog import no_reparse

LOCK = threading.RLock()
ROOT_GUIDS = ('root________','menu________','toolbar_____','unfiled_____','mobile______','tags________')


@dataclass(frozen=True)
class BrowserProfile:
    browser: str
    name: str
    path: str
    kind: str = 'chromium'

    @property
    def key(self): return hashlib.sha256((self.kind+'|'+self.path.casefold()).encode()).hexdigest()


def discover_profiles(*, local=None, roaming=None, extra=()):
    local = Path(local or os.environ.get('LOCALAPPDATA',''))
    roaming = Path(roaming or os.environ.get('APPDATA',''))
    roots = [('Chrome',local/'Google'/channel/'User Data','chromium')
             for channel in ('Chrome','Chrome Beta','Chrome Dev','Chrome SxS')]
    roots += [('Edge',local/'Microsoft'/channel/'User Data','chromium')
              for channel in ('Edge','Edge Beta','Edge Dev','Edge SxS')]
    roots += [('Firefox',roaming/'Mozilla'/'Firefox','firefox')]
    roots += [(record['browser'],Path(record['path']),record['kind']) for record in extra]
    profiles, known = [], set()
    def add(browser,name,path,kind):
        path=Path(path).absolute(); key=str(path).casefold()
        if path.is_file() and key not in known:
            known.add(key); profiles.append(BrowserProfile(browser,name,str(path),kind))
    for browser,root,kind in roots:
        if kind=='chromium':
            names={}
            try:
                state=data_file(root/'Local State').read_json()
                names=state.get('profile',{}).get('info_cache',{})
            except FileNotFoundError: pass
            if root.exists():
                for directory in sorted(root.iterdir()):
                    if directory.is_dir():
                        name=str(names.get(directory.name,{}).get('name') or directory.name)
                        for filename in ('Bookmarks','AccountBookmarks'):
                            add(browser,name+(' · 账号书签' if filename=='AccountBookmarks' else ''),directory/filename,kind)
            for filename in ('Bookmarks','AccountBookmarks'):
                add(browser,root.name,root/filename,kind)  # Portable/custom profile itself.
        elif kind=='firefox':
            parser=configparser.ConfigParser(interpolation=None)
            ini=root/'profiles.ini'
            if ini.exists():
                parser.read_string(data_file(ini).read_text(encoding='utf-8-sig'))
                for section in parser.sections():
                    if not section.startswith('Profile') or not parser.has_option(section,'Path'): continue
                    path=Path(parser.get(section,'Path'))
                    if parser.getboolean(section,'IsRelative',fallback=True): path=root/path
                    add(browser,parser.get(section,'Name',fallback=section),path/'places.sqlite','firefox')
            for path in (root/'Profiles').glob('*/places.sqlite'):
                add(browser,path.parent.name,path,'firefox')
            add(browser,root.name,root/'places.sqlite','firefox')
        else: raise ValueError('未知浏览器书签格式。')
    return profiles


def url_key(value):
    """Conservative normalization; keep path/query/fragment and trailing slash."""
    if not isinstance(value,str) or not value.strip() or len(value)>65536 or '\0' in value:
        raise ValueError('书签网址缺失或格式异常，不能确认全部已导入。')
    value=value.strip()
    match=re.match(r'([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)(.*)',value,re.S)
    if not match: return value
    # Userinfo is case-sensitive; do not accidentally merge two credentials.
    authority = match[2]
    if '@' in authority:
        userinfo, host = authority.rsplit('@', 1)
        authority = userinfo + '@' + host.lower()
    else: authority = authority.lower()
    return match[1].lower()+'://'+authority+match[3]


def fingerprint(path):
    """Include WAL: closed-browser checks alone aren't a change snapshot."""
    path=Path(path); digest=hashlib.sha256()
    for file in (path,path.with_name(path.name+'-wal')):
        if file.exists():
            digest.update(file.name.encode())
            with data_file(file).open('rb') as handle:
                for block in iter(lambda:handle.read(1024*1024),b''): digest.update(block)
    return digest.hexdigest()


def chromium_entries(value):
    if not isinstance(value,dict) or value.get('version')!=1 or not isinstance(value.get('roots'),dict):
        raise ValueError('浏览器书签格式未知，未清理源数据。')
    output=[]; pending=[(node,(),0) for node in value['roots'].values()]
    count=0
    while pending:
        node,parents,depth=pending.pop(); count+=1
        if count>250000 or depth>128 or not isinstance(node,dict): raise ValueError('书签树过大或损坏。')
        kind=node.get('type')
        if kind=='url':
            url=str(node.get('url') or ''); url_key(url)
            output.append({'url':url,'title':str(node.get('name') or url),'folder':' / '.join(parents)})
        elif kind=='folder':
            children=node.get('children')
            if not isinstance(children,list): raise ValueError('书签文件夹格式损坏。')
            pending += [(child,(*parents,str(node.get('name') or '')),depth+1) for child in reversed(children)]
        else: raise ValueError('未知书签节点类型，未确认完整导入。')
    return output


def empty_chromium(value):
    """Preserve root IDs/UUIDs and recompute Chromium's native file checksum."""
    value=copy.deepcopy(value)
    roots=value['roots']
    if set(roots)!={'bookmark_bar','other','synced'}:
        raise ValueError('Chromium 根目录格式未知，已导入但不清理。')
    md5=hashlib.md5(usedforsecurity=False); sha=hashlib.sha256()
    for key in ('bookmark_bar','other','synced'):
        node=roots[key]
        if node.get('type')!='folder' or not isinstance(node.get('id'),str):
            raise ValueError('Chromium 根节点 ID 或类型无效，未清理。')
        node['children']=[]
        block=node['id'].encode('utf8')+str(node.get('name') or '').encode('utf-16-le')+b'folder'
        md5.update(block);sha.update(block)
    value['checksum']=md5.hexdigest()
    if 'checksum_sha256' in value:value['checksum_sha256']=sha.hexdigest()
    return value


def firefox_entries(path):
    db=data_file(path).connect(read_only=True)
    try:
        rows=db.execute('SELECT b.id,b.parent,b.type,b.title,b.guid,p.url FROM moz_bookmarks b LEFT JOIN moz_places p ON b.fk=p.id ORDER BY b.id').fetchall()
    finally: db.close()
    by_id={row[0]:row for row in rows}; output=[]
    for row in rows:
        if row[2]!=1: continue
        url=row[5]; url_key(url)
        parents,seen=[],set(); parent=row[1]
        for _ in range(128):
            if parent not in by_id: break
            if parent in seen: raise ValueError('Firefox 书签树包含环，未清理。')
            seen.add(parent); item=by_id[parent]
            if item[4]=='root________': break
            parents.append(item[3] or item[4]); parent=item[1]
        else: raise ValueError('Firefox 书签文件夹超过128层。')
        output.append({'url':url,'title':row[3] or url,'folder':' / '.join(reversed(parents))})
    return output


def read_profile(profile):
    path=Path(profile.path)
    no_reparse(path,Path(path.anchor))
    before=fingerprint(path)
    if profile.kind=='chromium': entries=chromium_entries(data_file(path).read_json())
    elif profile.kind=='firefox': entries=firefox_entries(path)
    else: raise ValueError('不支持该浏览器格式。')
    if fingerprint(path)!=before: raise ValueError('读取期间书签发生变化，请刷新后重试；源书签未清理。')
    return entries,before


def running_browsers():
    if os.name!='nt': raise ValueError('无法确认浏览器已关闭，拒绝自动清理。')
    class Entry(C.Structure):
        _fields_=[('size',C.c_uint32),('usage',C.c_uint32),('pid',C.c_uint32),('heap',C.c_size_t),
            ('module',C.c_uint32),('threads',C.c_uint32),('parent',C.c_uint32),('priority',C.c_int32),
            ('flags',C.c_uint32),('exe',C.c_wchar*260)]
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[C.c_uint32,C.c_uint32]; kernel.CreateToolhelp32Snapshot.restype=C.c_void_p
    kernel.Process32FirstW.argtypes=[C.c_void_p,C.POINTER(Entry)]; kernel.Process32FirstW.restype=C.c_int32
    kernel.Process32NextW.argtypes=kernel.Process32FirstW.argtypes; kernel.Process32NextW.restype=C.c_int32
    kernel.CloseHandle.argtypes=[C.c_void_p]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==C.c_void_p(-1).value: raise ValueError('无法确认浏览器进程状态，拒绝清理。')
    entry=Entry(); entry.size=C.sizeof(entry); found=set()
    try:
        if not kernel.Process32FirstW(handle,C.byref(entry)): raise ValueError('浏览器进程检查失败，未清理。')
        while True:
            if entry.exe.lower() in {'chrome.exe','msedge.exe','firefox.exe'}: found.add(entry.exe.lower())
            if not kernel.Process32NextW(handle,C.byref(entry)): break
    finally: kernel.CloseHandle(handle)
    return sorted(found)


class BookmarkLibrary:
    def __init__(self,storage):
        self.storage=storage
        self.file=storage.file('bookmarks/library.json')
    def load(self):
        if not self.file.exists(): return {'version':1,'items':[]}
        value=self.file.read_json()
        if not isinstance(value,dict) or value.get('version')!=1 or not isinstance(value.get('items'),list):
            raise ValueError('书签库损坏，未覆盖或清理浏览器书签。')
        for item in value['items']:
            if not isinstance(item,dict) or item.get('key')!=url_key(item.get('url')): raise ValueError('书签记录损坏。')
        return value
    def add(self,title,url):
        with LOCK:
            value=self.load(); key=url_key(url)
            item=next((item for item in value['items'] if item['key']==key),None)
            if item is None:
                value['items'].append({'id':uuid4().hex,'key':key,'url':url,'title':title or url,'origins':[]})
            else: item['title']=title or item['title']
            self.file.write_json(value,indent=2)
    def remove(self,ids):
        with LOCK:
            value=self.load(); value['items']=[item for item in value['items'] if item['id'] not in set(ids)]
            self.file.write_json(value,indent=2)
    def import_profiles(self,profiles,report=lambda *_:None):
        snapshots,errors=[],[]
        for index,profile in enumerate(profiles):
            report(f'读取书签 · {profile.browser} / {profile.name}',int(index*80/max(1,len(profiles))))
            try:
                entries,stamp=read_profile(profile)
                snapshots.append({'profile':asdict(profile),'stamp':stamp,'entries':entries})
            except Exception as error: errors.append({'profile':asdict(profile),'error':str(error)})
        with LOCK:
            value=self.load(); by_key={item['key']:item for item in value['items']}; added=0; total=0
            for snapshot in snapshots:
                profile=snapshot['profile']
                for row in snapshot['entries']:
                    total+=1; key=url_key(row['url'])
                    if key not in by_key:
                        item={'id':uuid4().hex,'key':key,'url':row['url'],'title':row['title'],'origins':[]}
                        value['items'].append(item); by_key[key]=item; added+=1
                    origin={'browser':profile['browser'],'profile':profile['name'],'path':profile['path'],
                            'folder':row['folder'],'title':row['title']}
                    if origin not in by_key[key]['origins']: by_key[key]['origins'].append(origin)
            self.file.write_json(value,indent=2)
            saved={item['key'] for item in self.load()['items']}
            if any(url_key(row['url']) not in saved for snap in snapshots for row in snap['entries']):
                raise ValueError('保存后校验失败，源书签不清理。')
            batch=uuid4().hex
            self.storage.file('bookmarks/imports/'+batch+'.json').write_json({'version':1,'profiles':snapshots},indent=2)
            self.storage.file('bookmarks/last_import.json').write_json({'batch':batch,'read':total,
                'added':added,'duplicates':total-added,'errors':errors,'profiles':len(snapshots)},indent=2)
        report('导入并校验完成，浏览器书签尚未清理。',100)
        return {'batch':batch,'read':total,'added':added,'duplicates':total-added,'errors':errors,
                'profiles':len(snapshots),'count':len(value['items'])}

    def clear_imported(self,batch,report=lambda *_:None):
        if not application_storage().protected:
            raise ValueError('清理源书签前请先启用 VC 加密模式，确保恢复副本保存在加密卷内。当前书签仅导入，未删除。')
        if not re.fullmatch('[0-9a-f]{32}',batch): raise ValueError('导入批次编号无效。')
        opened=running_browsers()
        if opened: raise ValueError('请关闭所有 Chrome、Edge、Firefox（含后台进程）后重试；未清理：'+', '.join(opened))
        with LOCK:
            saved={item['key'] for item in self.load()['items']}
            manifest=self.storage.file('bookmarks/imports/'+batch+'.json').read_json()
            results=[]
            result_file=self.storage.file('bookmarks/backups/'+batch+'/results.json')
            for index,snapshot in enumerate(manifest['profiles']):
                profile=BrowserProfile(**snapshot['profile']); path=Path(profile.path)
                backup_name = backup_hash = None
                report('校验并清理 · '+profile.browser+' / '+profile.name,int(index*95/max(1,len(manifest['profiles']))))
                try:
                    if running_browsers(): raise ValueError('浏览器重新启动，停止清理剩余配置。')
                    rows,stamp=read_profile(profile)
                    if stamp!=snapshot['stamp']: raise ValueError('导入后源书签已变化，请重新导入；本配置未清理。')
                    if any(url_key(row['url']) not in saved for row in rows): raise ValueError('尚有未保存书签，未清理。')
                    backup_name=f'bookmarks/backups/{batch}/{profile.key}.'+('json' if profile.kind=='chromium' else 'sqlite')
                    backup=self.storage.file(backup_name)
                    if backup.exists(): raise ValueError('该批次已有恢复副本，请勿覆盖；查看批次结果或重新导入。')
                    if profile.kind=='chromium':
                        backup.copy_from(path)
                        if self.byte_hash(path)!=self.byte_hash(backup.path): raise ValueError('恢复副本校验失败。')
                        backup_hash = self.byte_hash(backup.path)
                        result_file.write_json({'version':1,'profiles':results+[{'profile':asdict(profile),
                            'state':'prepared','backup':backup_name,'backup_sha256':backup_hash,'before':stamp}]},indent=2)
                        value=empty_chromium(data_file(path).read_json())
                        if running_browsers() or fingerprint(path)!=stamp: raise ValueError('清理前源数据变化，未清理。')
                        data_file(path).write_json(value,indent=3)
                    else:
                        source=data_file(path).connect(read_only=True); copy_db=backup.connect()
                        try:
                            source.backup(copy_db)
                            if copy_db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Firefox 恢复副本校验失败。')
                        finally: copy_db.close(); source.close()
                        backup_hash = self.byte_hash(backup.path)
                        result_file.write_json({'version':1,'profiles':results+[{'profile':asdict(profile),
                            'state':'prepared','backup':backup_name,'backup_sha256':backup_hash,'before':stamp}]},indent=2)
                        if running_browsers() or fingerprint(path)!=stamp: raise ValueError('Firefox 源数据变化，未清理。')
                        clear_firefox(path)
                    left,_=read_profile(profile)
                    if left: raise ValueError('清理后仍有书签，请检查恢复副本；未伪报成功。')
                    results.append({'profile':asdict(profile),'state':'cleared','backup':backup_name,
                                    'backup_sha256':backup_hash,'after':fingerprint(path),'removed':len(rows)})
                except Exception as error:
                    results.append({'profile':asdict(profile),'state':'failed','error':str(error),
                                    'backup':backup_name,'backup_sha256':backup_hash})
                result_file.write_json({'version':1,'profiles':results},indent=2)
        return {'profiles':results,'removed':sum(item.get('removed',0) for item in results),
                'failed':sum(item['state']!='cleared' for item in results),'batch':batch}

    @staticmethod
    def byte_hash(path):
        digest=hashlib.sha256()
        with data_file(path).open('rb') as file:
            for block in iter(lambda:file.read(1024*1024),b''): digest.update(block)
        return digest.hexdigest()

    def restore(self,batch):
        if not application_storage().protected: raise ValueError('恢复源书签需要先解锁原 VC 数据卷。')
        if not re.fullmatch('[0-9a-f]{32}',batch): raise ValueError('导入批次无效。')
        if running_browsers(): raise ValueError('请关闭 Chrome、Edge、Firefox 后恢复。')
        with LOCK:
            manifest_file=self.storage.file('bookmarks/backups/'+batch+'/results.json')
            manifest=manifest_file.read_json(); restored=0
            for item in manifest['profiles']:
                if item['state']!='cleared': continue
                path=Path(item['profile']['path']); no_reparse(path,Path(path.anchor))
                if fingerprint(path)!=item['after']:
                    raise ValueError('清理后浏览器数据已变化，拒绝覆盖新数据。原恢复副本仍保留在加密卷内。')
                backup=self.storage.file(item['backup'])
                if not backup.exists(): raise ValueError('恢复副本不存在，未修改浏览器。')
                if self.byte_hash(backup.path) != item.get('backup_sha256'):
                    raise ValueError('恢复副本 SHA-256 校验失败，未覆盖浏览器。')
                if running_browsers(): raise ValueError('浏览器重新启动，停止恢复。')
                data_file(path).copy_from(backup.path)
                if item['profile']['kind']=='firefox':
                    # A snapshot backup is a complete SQLite file; never apply
                    # a former WAL against the restored database.
                    for suffix in ('-wal','-shm'): data_file(path.with_name(path.name+suffix)).remove(missing_ok=True)
                item['state']='restored'; restored+=1
                manifest_file.write_json(manifest,indent=2)
            return restored


def clear_firefox(path):
    """Closed-profile, transactional deletion of bookmarks, not browsing history.

    Unknown schemas/SQL-trigger requirements roll back rather than deleting the
    Places file. Root GUIDs, history, logins and cookies are never removed.
    """
    db=data_file(path).connect()
    try:
        columns={row[1] for row in db.execute('PRAGMA table_info(moz_bookmarks)')}
        if not {'id','fk','guid','type','parent','lastModified','syncStatus','syncChangeCounter'}<=columns:
            raise ValueError('Firefox 数据库版本未知，只导入，不自动清理。')
        roots=db.execute('SELECT guid FROM moz_bookmarks WHERE guid IN (?,?,?,?,?,?)',ROOT_GUIDS).fetchall()
        if len(roots)<5: raise ValueError('Firefox 根书签结构异常，未清理。')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TEMP TABLE removing AS SELECT * FROM moz_bookmarks WHERE guid NOT IN (?,?,?,?,?,?)',ROOT_GUIDS)
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        now=int(time.time()*1000000)
        if 'moz_bookmarks_deleted' in tables:
            db.execute('INSERT OR IGNORE INTO moz_bookmarks_deleted (guid,dateRemoved) SELECT guid,? FROM removing WHERE syncStatus=2',(now,))
        if 'moz_items_annos' in tables: db.execute('DELETE FROM moz_items_annos WHERE item_id IN (SELECT id FROM removing)')
        places_columns={row[1] for row in db.execute('PRAGMA table_info(moz_places)')}
        if 'foreign_count' in places_columns:
            db.execute('UPDATE moz_places SET foreign_count=max(0,foreign_count-(SELECT count(*) FROM removing WHERE fk=moz_places.id)) WHERE id IN (SELECT fk FROM removing)')
        db.execute('DELETE FROM moz_bookmarks WHERE id IN (SELECT id FROM removing)')
        db.execute('UPDATE moz_bookmarks SET lastModified=?,syncChangeCounter=syncChangeCounter+1 WHERE guid IN (?,?,?,?,?)',(now,*ROOT_GUIDS[1:]))
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Firefox 清理完整性检查失败。')
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally: db.close()


def run_bookmark_import(payload,context):
    library=BookmarkLibrary(application_storage().for_plugin('chrome_launcher'))
    profiles=[BrowserProfile(**item) for item in payload['profiles']]
    return library.import_profiles(profiles,context.report)


def run_bookmark_cleanup(payload,context):
    return BookmarkLibrary(application_storage().for_plugin('chrome_launcher')).clear_imported(payload['batch'],context.report)
