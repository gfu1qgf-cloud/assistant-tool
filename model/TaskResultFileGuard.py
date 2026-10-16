"""Local, non-blocking checks for render outputs; no Resolve/API connection."""

from model.AppDataStorage import data_directory, data_file, data_io

import ctypes
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
SETTLE_SECONDS = 2.0
TEMP_PREFIX = ".task-result-"


class FileStillWritingError(OSError):
    """A source is not safe to consume yet; retry on the next organize run."""


def is_copy_temporary(path):
    path = Path(path)
    return path.name.startswith(TEMP_PREFIX) and path.suffix == ".tmp"


def file_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@contextmanager
def read_without_writer(path):
    """Windows sharing rules reject even writers that allow concurrent readers.

    Used for brief probes, not held throughout a potentially large video copy.
    Never request write access, wait for a writer, or forcibly unlock somebody
    else's handle. On other OSes
    the before/after checks remain, but are not a mandatory Windows-style lock.
    """
    path = Path(path)
    if os.name == "nt":
        from ctypes import wintypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                           wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        # GENERIC_READ, FILE_SHARE_READ only, OPEN_EXISTING. Omitting share-write
        # and share-delete excludes an existing writer or replacement handle.
        native_path = str(path.resolve())
        if len(native_path) >= 260 and not native_path.startswith("\\\\?\\"):
            native_path = ("\\\\?\\UNC\\" + native_path[2:] if native_path.startswith("\\\\")
                           else "\\\\?\\" + native_path)
        handle = create(native_path, 0x80000000, 1, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            code = ctypes.get_last_error()
            if code in {32, 33}:
                raise FileStillWritingError("文件仍被写入或替换进程占用")
            if code in {2, 3}:
                raise FileStillWritingError("源文件已消失，稍后重试")
            if code == 5:
                raise FileStillWritingError("暂时无法读取源文件，请检查占用或访问权限")
            raise ctypes.WinError(code)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
    else:
        try:
            descriptor = data_io.open_fd(path, os.O_RDONLY)
        except (FileNotFoundError, PermissionError) as error:
            raise FileStillWritingError("源文件暂时无法读取") from error
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def require_settled_video(path, info):
    if Path(path).suffix.lower() not in VIDEO_SUFFIXES:
        return
    if info.st_size <= 0:
        raise FileStillWritingError("视频文件还是空的")
    if time.time() - info.st_mtime < SETTLE_SECONDS:
        raise FileStillWritingError("视频刚更新，等待写入收尾后再整理")


def check_file_ready(path):
    """For already-exported pending files / direct-to-result output."""
    with read_without_writer(path) as descriptor:
        before = os.fstat(descriptor)
        require_settled_video(path, before)
        if file_signature(before) != file_signature(Path(path).stat()):
            raise FileStillWritingError("检查期间源文件发生变化")


def copy_if_ready_and_updated(src, dst):
    """Commit a whole snapshot, or leave the existing result entirely intact."""
    src, dst = Path(src), Path(dst)
    with read_without_writer(src) as descriptor:
        before = os.fstat(descriptor)
        if src.suffix.lower() in VIDEO_SUFFIXES and before.st_size <= 0:
            raise FileStillWritingError("视频文件还是空的")
        if dst.exists():
            target = dst.stat()
            if (before.st_mtime_ns <= target.st_mtime_ns
                    and before.st_size == target.st_size):
                print(f"跳过未更新: {dst}")
                return False
        require_settled_video(src, before)
    # Do not deny a new render write for the duration of a large file copy.
    # Copy normally, then reacquire the short probe and reject any new writer
    # or changed source before committing the temporary snapshot.
    data_directory(dst.parent).ensure(parents=True, exist_ok=True)
    fd, name = data_io.mkstemp(prefix=TEMP_PREFIX, suffix=".tmp", dir=dst.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        try:
            data_io.copy2(src, temporary)
        except OSError as error:
            if getattr(error, "winerror", None) in {32, 33}:
                raise FileStillWritingError("复制期间文件被写入进程占用，原成品未改动") from error
            if not src.exists():
                raise FileStillWritingError("复制期间源文件被替换或移走") from error
            raise
        with read_without_writer(src) as descriptor:
            if (file_signature(before) != file_signature(os.fstat(descriptor))
                    or file_signature(before) != file_signature(src.stat())
                    or temporary.stat().st_size != before.st_size):
                raise FileStillWritingError("复制期间文件变化，已丢弃临时副本")
            try:
                data_io.replace(temporary, dst)
            except OSError as error:
                if getattr(error, "winerror", None) in {32, 33}:
                    raise FileStillWritingError("目标成品被其他程序占用，原成品未改动") from error
                raise
    finally:
        if temporary.exists():
            # copystat can carry a read-only bit from the source on Windows.
            temporary.chmod(0o600)
            data_file(temporary).unlink()
    print(f"已复制: {dst}")
    return True
