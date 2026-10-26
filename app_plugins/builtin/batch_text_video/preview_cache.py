"""Disposable visual/audio previews explicitly kept outside the private volume."""
import os
from pathlib import Path


def cache_root(kind):
    if kind not in {'thumbnails', 'audition', 'dnx-alpha'}:
        raise ValueError('未知预览缓存类型。')
    from model.MediaStorage import private_volume_path
    from model.PrivateDataCatalog import no_reparse
    from model.AppDataStorage import application_storage
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local')
    target = base/'AssistantTool'/'BatchTextVideoPreview'/kind
    target = application_storage().resolve(target)
    if private_volume_path(target):
        raise ValueError('预览缓存目录位于加密卷，请将LOCALAPPDATA指向卷外本地目录。')
    return no_reparse(target, Path(target.anchor))


def audition_temporary():
    from model.MediaStorage import media_temporary
    return media_temporary(cache_root('audition'), prefix='voice-space-audition-')
