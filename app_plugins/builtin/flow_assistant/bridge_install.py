"""Activate bridge files only when the matching main program starts normally."""
import importlib.util
import json
from pathlib import Path
import re
import sys
import uuid
from model.AppDataStorage import data_directory, data_file, data_io

from .setup_support import EXTENSION_VERSION


def builder(root):
    spec=importlib.util.spec_from_file_location('flow_bridge_startup_builder',root/'tools/build_flow_bridge.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def atomic(path, data):
    data_directory(path.parent).ensure()
    name=None
    try:
        # tempfile can retry PermissionError millions of times on Windows when
        # os.access reports a read-only directory as writable. Fail promptly;
        # exclusive creation still prevents following an existing link/file.
        candidate=path.parent/('.flow-update-'+uuid.uuid4().hex+'.tmp')
        with data_file(candidate).open('xb') as stream:
            name=candidate;stream.write(data)
        data_io.replace(name,path)
    finally:
        if name and name.exists():data_file(name).remove()


def prepare(root=None):
    root=Path(root) if root is not None else (Path(sys.executable).resolve().parent
        if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[3])
    active=root/'browser_extensions/flow_bridge'
    before=json.loads(data_file(active/'manifest.json').read_text('utf-8'))
    current=before.get('version','')
    if current==EXTENSION_VERSION:return False
    if getattr(sys,'frozen',False):
        raise ValueError('程序与桥接扩展版本不一致，请恢复完整发行包；未运行源码构建工具或改写原扩展。')
    old=re.fullmatch(r'1\.1\.([0-9]+)',current)
    if not old or int(old[1])>int(EXTENSION_VERSION.rsplit('.',1)[1]):
        raise ValueError('当前扩展文件版本无法安全自动替换，请核对主程序与扩展来源。')
    parent=active.parent.resolve()
    # Use a single exclusive mkdir rather than tempfile's Windows permission
    # retry loop. Cleanup owns only the directory successfully created here.
    temporary=parent/('flow-bridge-build-'+uuid.uuid4().hex)
    data_io.mkdir(temporary)
    try:
        assert temporary.resolve().parent==parent
        staged=temporary/'package';builder(root).build(root,destination=staged)
        manifest=json.loads(data_file(staged/'manifest.json').read_text('utf-8'))
        # Description names the audited upstream version; it is display text,
        # not an extension privilege. All other manifest fields stay exact.
        stable=lambda data:{k:v for k,v in data.items() if k not in {'version','description'}}
        if manifest.get('version')!=EXTENSION_VERSION or stable(before)!=stable(manifest):
            raise ValueError('新扩展版本或权限核对失败，保留原扩展文件。')
        files={path.relative_to(staged):data_file(path).read_bytes() for path in staged.rglob('*') if path.is_file()}
        assert sum(map(len,files.values()))<128*1024*1024
        originals={path:data_file(active/path).read_bytes() if (active/path).is_file() else None for path in files}
        copied=[]
        try:
            for path in sorted(files,key=lambda p:(p.name=='manifest.json',str(p))):
                atomic(active/path,files[path]);copied.append(path)
        except Exception:
            for path in reversed(copied):
                if originals[path] is None:data_file(active/path).remove()
                else:atomic(active/path,originals[path])
            raise
    finally:
        data_io.rmtree(temporary)
    return True
