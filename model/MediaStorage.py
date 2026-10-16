"""Explicit placement and disk preflight for generated, reproducible media."""
from contextlib import contextmanager
from pathlib import Path
import re
import shutil

from model.AppDataStorage import application_storage, data_directory, data_io

RESERVE = 256 * 1024**2
CACHE_DIRECTORY = '.辅助工具媒体缓存'


def generated_media_path(path):
    """Internal scratch must never be organized/countable as user task media."""
    return any(part.casefold() in {CACHE_DIRECTORY.casefold(), '.整理上传临时'}
               for part in Path(path).parts)


def private_volume_path(path):
    storage = application_storage()
    if not storage.protected:
        return False
    volume = getattr(storage.guard, 'volume', None)
    root = Path(volume.root).resolve() if volume is not None else storage.root
    return storage.resolve(path).resolve().is_relative_to(root)


def require_media_space(path, expected=0):
    storage = application_storage()
    if storage.protected: storage.check(write=True)
    parent = Path(path).absolute()
    while not parent.exists(): parent = parent.parent
    if shutil.disk_usage(parent).free < RESERVE + max(0,int(expected)):
        raise OSError(f'媒体磁盘空间不足：需保留256 MB余量。位置：{path}。已停止生成，请清理或更换任务／素材位置。')


def require_media_output(path):
    if private_volume_path(path):
        raise ValueError('大型媒体输出不能写入程序绑定的加密数据卷，请使用卷外的任务／素材目录。位置：'+str(path))
    require_media_space(path)


def media_cache_root(preferred, *, anchors, namespace):
    """Keep existing non-VC paths/keys; reroute only private-volume caches."""
    preferred = Path(preferred)
    if not private_volume_path(preferred):
        return preferred
    if not re.fullmatch('[a-z][a-z0-9_-]{0,79}',namespace):
        raise ValueError('媒体缓存名称无效。')
    for anchor in anchors:
        if anchor and not private_volume_path(anchor):
            root = Path(anchor).expanduser().resolve()/CACHE_DIRECTORY/namespace
            require_media_space(root)
            return root
    raise ValueError('没有可用的外部任务／素材目录，未将媒体缓存写入加密卷。')


@contextmanager
def media_temporary(anchor, *, prefix):
    root = Path(anchor).expanduser().resolve()
    require_media_output(root)
    data_directory(root).ensure()
    with data_io.TemporaryDirectory(prefix=prefix, dir=str(root)) as folder:
        yield folder
