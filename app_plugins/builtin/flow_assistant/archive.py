"""Copy only a bound, completed browser download to its submitted task folder."""
import hashlib
import os
from pathlib import Path
import re
import shutil

from model.AppDataStorage import data_file, data_io


def digest(path):
    value = hashlib.sha256()
    with data_file(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def bound_download(job, downloads_root):
    download = job['remote']['download']
    source = Path(download['filename']).resolve()
    root = Path(downloads_root).resolve()
    attempt = str(job['attempt'])
    if (not re.fullmatch(r'[a-f0-9]{32}', attempt)
            or source.parent != root / 'FlowBridge' / attempt
            or not re.fullmatch(r'result(?: \([1-9][0-9]*\))?\.mp4', source.name, re.IGNORECASE)
            or download.get('state') != 'complete' or not download.get('mediaId') or not download.get('submissionId')):
        raise ValueError('下载文件位置、媒体绑定或完成状态与本任务不一致。')
    return source


def archive_download(job, downloads_root, verify=None):
    source = bound_download(job, downloads_root)
    # Chrome's conflictAction=uniquify may append " (1)" to this name.
    # Keep the exact attempt directory and bound media identity mandatory;
    # this does not permit arbitrary files from the Downloads directory.
    if not source.is_file() or source.stat().st_size < 32:
        raise ValueError('下载文件不存在或不完整。')
    if verify is None:
        from model.node_workflow.render import probe, resolve_tools
        verify = lambda path: probe(path, resolve_tools()[1])
    verify(source)
    destination = Path(job['payload']['output'])
    if destination.parent.resolve() != Path(job['payload']['directory']).resolve():
        raise ValueError('保存位置不属于提交时的任务目录。')
    if not destination.parent.is_dir():
        raise ValueError('任务目录已移动或不可用，保留浏览器下载以便重试。')
    from model.MediaStorage import require_media_output
    require_media_output(destination)
    if not destination.exists() and shutil.disk_usage(destination.parent).free < source.stat().st_size + 256 * 1024**2:
        raise OSError('任务磁盘空间不足，浏览器下载原件已保留；释放空间后仅重试归档。')
    checksum = digest(source)
    if destination.exists():
        if destination.is_file() and digest(destination) == checksum:
            return str(destination)
        raise FileExistsError('目标文件已存在且内容不同，未覆盖：' + destination.name)
    temporary = destination.with_name('.' + destination.name + '.' + job['id'] + '.part')
    try:
        with data_file(source).open('rb') as reader, data_file(temporary).open('wb') as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if digest(temporary) != checksum:
            raise ValueError('归档文件校验失败，下载原件已保留。')
        try:
            if os.name == 'nt':
                data_io.rename(temporary, destination)  # Windows rename never overwrites.
            else:
                data_io.link(temporary, destination)
                data_file(temporary).remove()
        except FileExistsError:
            # Another archive may have completed between the initial check and
            # our no-overwrite publish. Accept only the exact same bytes.
            if not destination.is_file() or digest(destination) != checksum:
                raise FileExistsError('目标文件已存在且内容不同，未覆盖：' + destination.name) from None
        return str(destination)
    finally:
        if temporary.exists():
            data_file(temporary).remove()


def cleanup_download(job, downloads_root):
    if job['status'] != 'completed':
        raise ValueError('只清理已采用／核验通过的成品下载副本，待复核文件不清理。')
    source = bound_download(job, downloads_root)
    destination = Path(job['payload']['output']).resolve()
    if destination.parent != Path(job['payload']['directory']).resolve() or source == destination or not destination.is_file():
        raise ValueError('任务成品位置无效，未清理。')
    if not source.exists():
        return True
    if digest(source) != digest(destination):
        raise ValueError('下载副本与任务成品校验不同，未清理。')
    data_file(source).remove()
    return True


def archive_package(job, downloads_root, *, cleanup=False):
    from model.node_workflow.render import probe, resolve_tools
    from .policy import media_review
    metadata = []
    def verify(path):
        value = probe(path, resolve_tools()[1])
        metadata.append(value)
        return value
    output = archive_download(job, downloads_root, verify=verify)
    info = metadata[0]
    review = media_review(info, job['payload'])
    result = {'output': output, 'media': info, 'review': review, 'download_cleaned': False}
    if cleanup and not review:
        try:
            result['download_cleaned'] = cleanup_download({**job, 'status': 'completed'}, downloads_root)
        except (OSError, ValueError) as error:
            # Delivery succeeded; an optional cleanup failure is not a failed generation.
            result['cleanup_warning'] = '成品已保存，自动清理副本未执行：' + str(error)
    return result
