"""Activate bridge files only when the matching main program starts normally."""
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .setup_support import EXTENSION_VERSION


def builder(root):
    spec=importlib.util.spec_from_file_location('flow_bridge_startup_builder',root/'tools/build_flow_bridge.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def atomic(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    name=None
    try:
        with tempfile.NamedTemporaryFile(prefix='.flow-update-',suffix='.tmp',dir=path.parent,delete=False) as stream:
            name=Path(stream.name);stream.write(data)
        os.replace(name,path)
    finally:
        if name and name.exists():name.unlink()


def prepare(root=None):
    root=Path(root) if root is not None else (Path(sys.executable).resolve().parent
        if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[3])
    active=root/'browser_extensions/flow_bridge'
    before=json.loads((active/'manifest.json').read_text('utf-8'))
    current=before.get('version','')
    if current==EXTENSION_VERSION:return False
    if getattr(sys,'frozen',False):
        raise ValueError('程序与桥接扩展版本不一致，请恢复完整发行包；未运行源码构建工具或改写原扩展。')
    old=re.fullmatch(r'1\.1\.([0-9]+)',current)
    if not old or int(old[1])>int(EXTENSION_VERSION.rsplit('.',1)[1]):
        raise ValueError('当前扩展文件版本无法安全自动替换，请核对主程序与扩展来源。')
    parent=active.parent.resolve()
    with tempfile.TemporaryDirectory(prefix='flow-bridge-build-',dir=parent) as name:
        temporary=Path(name).resolve();assert temporary.parent==parent
        staged=temporary/'package';builder(root).build(root,destination=staged)
        manifest=json.loads((staged/'manifest.json').read_text('utf-8'))
        if manifest.get('version')!=EXTENSION_VERSION or {k:v for k,v in before.items() if k!='version'}!={k:v for k,v in manifest.items() if k!='version'}:
            raise ValueError('新扩展版本或权限核对失败，保留原扩展文件。')
        files={path.relative_to(staged):path.read_bytes() for path in staged.rglob('*') if path.is_file()}
        assert sum(map(len,files.values()))<128*1024*1024
        originals={path:(active/path).read_bytes() if (active/path).is_file() else None for path in files}
        copied=[]
        try:
            for path in sorted(files,key=lambda p:(p.name=='manifest.json',str(p))):
                atomic(active/path,files[path]);copied.append(path)
        except Exception:
            for path in reversed(copied):
                if originals[path] is None:(active/path).unlink()
                else:atomic(active/path,originals[path])
            raise
    return True
