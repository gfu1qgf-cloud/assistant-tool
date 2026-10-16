"""Portable private references, bound to this launch's verified data volume.

Old absolute references under the data-root namespace are interpreted against
the current mount, without probing the old drive. This does not switch live
volumes, move media, rewrite arbitrary strings or hook filesystem APIs.
"""
import os
from pathlib import Path, PureWindowsPath
import re

from .PrivateDataCatalog import PRIVATE_KEYS


def remounted_data_path(path, root):
    """Pure Windows mount-letter rebasing of the complete data-root prefix."""
    source, current = PureWindowsPath(str(path)), PureWindowsPath(str(root))
    local_drive = lambda value: bool(re.fullmatch(r'[a-zA-Z]:',value.drive) and value.root)
    if not local_drive(source) or not local_drive(current) or source.drive.casefold() == current.drive.casefold():
        return path
    namespace = current.parts[1:]
    if (not namespace or len(source.parts)-1 < len(namespace)
            or tuple(p.casefold() for p in source.parts[1:1+len(namespace)]) != tuple(p.casefold() for p in namespace)):
        return path
    suffix = source.parts[1+len(namespace):]
    if '..' in suffix: raise ValueError('私有数据引用不能跨出数据目录。')
    return str(current.joinpath(*suffix))


def private_path(value, *, base=None, storage=None):
    """Resolve a registered private setting; never fall back to plain storage."""
    from .AppDataStorage import application_storage
    storage = storage or application_storage()
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute() and ':' in str(path):
        raise ValueError('私有数据相对路径不能包含盘符、网址或备用数据流。')
    if path.drive and not path.is_absolute():
        raise ValueError('私有数据路径不能使用盘符相对写法，请使用相对数据目录或完整路径。')
    if not storage.protected:
        if not path.is_absolute():
            if base is None:
                from app_paths import DATA_ROOT
                base = DATA_ROOT
            path = Path(base)/path
        return storage.resolve(path)
    storage.check()
    if not path.is_absolute(): path = storage.root/path
    target = storage.resolve(path)
    if not target.is_relative_to(storage.root):
        from .ProtectedStorage import StorageUnavailable
        raise StorageUnavailable('私有数据位置不属于当前已验证的加密数据目录；未读取旧盘或回退明文。请将凭据／私有记录放入当前数据卷。')
    return target


def private_config(config, *, portable=False, storage=None):
    """Only registered path fields; keys, URLs, prompts and task/media stay put."""
    from .AppDataStorage import application_storage
    storage = storage or application_storage()
    result = dict(config)
    if not storage.protected: return result
    storage.check()
    for key in PRIVATE_KEYS:
        value = result.get(key)
        if not isinstance(value,str) or not value.strip(): continue
        path = Path(value.strip()).expanduser()
        if not path.is_absolute() and ':' in str(path):
            raise ValueError('私有数据相对路径不能包含盘符、网址或备用数据流：'+key)
        if path.drive and not path.is_absolute(): raise ValueError('私有数据路径不能使用盘符相对写法：'+key)
        target = storage.resolve(path if path.is_absolute() else storage.root/path)
        if not target.is_relative_to(storage.root):
            from .ProtectedStorage import StorageUnavailable
            raise StorageUnavailable('私有配置位置不属于当前加密数据目录：'+key+'；不会回退旧盘或明文目录。')
        result[key] = target.relative_to(storage.root).as_posix() if portable else str(target)
    return result


def remap_path_fields(value, fields):
    """Schema-owned file references only; never alter text/code or external paths."""
    from .AppDataStorage import application_storage
    storage = application_storage()
    if not storage.protected: return value
    def visit(item):
        if isinstance(item,list):
            for child in item: visit(child)
        elif isinstance(item,dict):
            for key,child in item.items():
                if key in fields and isinstance(child,str) and child and Path(child).is_absolute():
                    original = Path(os.path.abspath(child))
                    target = storage.resolve(original)
                    if target != original: item[key] = str(target)
                elif isinstance(child,(list,dict)): visit(child)
    visit(value)
    return value
