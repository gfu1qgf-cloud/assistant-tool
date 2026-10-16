"""Session-owned compressed MP4 resources. Nodes exchange references, never live IO objects."""

from model.AppDataStorage import data_io, data_file, data_directory
from bisect import bisect_right
from dataclasses import replace
import copy
import io
import math
import queue
import subprocess
import threading
import time
from uuid import uuid4

from .contracts import ResourceRef

CHUNK = 64 * 1024
MiB = 1024 * 1024


class MemoryBudget:
    """Bound compressed caches across concurrent jobs (decoder/encoder RAM is separate)."""
    def __init__(self, limit=512 * MiB):
        self.limit, self.used = limit, 0
        self.lock = threading.Lock()

    def reserve(self, count):
        with self.lock:
            if self.used + count > self.limit:
                raise MemoryError("压缩视频内存缓存达到总上限；请改用管道流或减少并发。未写临时视频。")
            self.used += count

    def release(self, count):
        with self.lock:
            self.used -= count
            assert self.used >= 0


PROCESS_BUDGET = MemoryBudget()


class MemoryReader(io.RawIOBase):
    """Seekable, read-only block reader: opening another reader does not copy the whole video."""
    def __init__(self, blocks, seekable=True):
        super().__init__()
        self.blocks, self.offsets = blocks, [0]
        for block in blocks:
            self.offsets.append(self.offsets[-1] + len(block))
        self.position, self.can_seek = 0, seekable

    def readable(self): return True
    def writable(self): return False
    def seekable(self): return self.can_seek
    def tell(self):
        self._checkClosed()
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        self._checkClosed()
        if not self.can_seek:
            raise io.UnsupportedOperation("管道流不可定位；请先接入内存缓存节点。")
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END):
            raise ValueError("无效的定位方式")
        position = offset + (0 if whence == io.SEEK_SET else self.position if whence == io.SEEK_CUR else self.offsets[-1])
        if position < 0: raise ValueError("不能定位到负数位置")
        self.position = position
        return position

    def read(self, size=-1):
        self._checkClosed()
        end = self.offsets[-1] if size < 0 else min(self.offsets[-1], self.position + size)
        pieces = []
        while self.position < end:
            index = bisect_right(self.offsets, self.position) - 1
            offset = self.position - self.offsets[index]
            count = min(end - self.position, len(self.blocks[index]) - offset)
            pieces.append(self.blocks[index][offset:offset + count])
            self.position += count
        return b"".join(pieces)

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        self.blocks = ()
        super().close()


class PipeReader(io.RawIOBase):
    """Bounded output pump; main worker can cancel even when FFmpeg is not producing bytes."""
    def __init__(self, command, context):
        super().__init__()
        self.context, self.stop = context, threading.Event()
        self.items = queue.Queue(maxsize=4)
        self.pending, self.error, self.tail = b"", None, b""
        self.eof = False
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.threads = [threading.Thread(target=self._pump, daemon=True),
                        threading.Thread(target=self._errors, daemon=True)]
        for thread in self.threads: thread.start()

    def _put(self, value):
        while not self.stop.is_set():
            try:
                self.items.put(value, timeout=.05)
                return
            except queue.Full: pass

    def _pump(self):
        try:
            while not self.stop.is_set():
                data = self.process.stdout.read1(CHUNK)
                if not data: break
                self._put(data)
        except Exception as error:
            if not self.stop.is_set(): self.error = error
        finally:
            self._put(None)

    def _errors(self):
        total = 0
        try:
            while not self.stop.is_set():
                data = self.process.stderr.read1(4096)
                if not data: break
                total += len(data)
                self.tail = (self.tail + data)[-4096:]
                if total > MiB:
                    self.error = RuntimeError("媒体封装错误输出异常过大，已中止。")
                    break
        except Exception as error:
            if not self.stop.is_set(): self.error = error

    def readable(self): return True
    def seekable(self): return False

    def read(self, size=-1):
        self._checkClosed()
        if size < 0:
            raise io.UnsupportedOperation("顺序流必须分块读取，不能一次读入全部视频。")
        if not size: return b""
        deadline = time.monotonic() + 60
        while not self.pending and not self.eof:
            if self.stop.is_set(): return b""
            self.context.check_canceled()
            if self.error is not None: raise self.error
            try:
                value = self.items.get(timeout=.05)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError("视频流 60 秒没有输出，已中止。")
                continue
            if value is None:
                self.eof = True
                code = self.process.wait(timeout=5)
                self.threads[1].join(timeout=2)
                if code:
                    raise RuntimeError("MP4 流封装失败：" + self.tail.decode("utf-8", "replace")[-1500:])
            else:
                self.pending = value
        result, self.pending = self.pending[:size], self.pending[size:]
        return result

    def close(self):
        if self.closed: return
        self.stop.set()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        for thread in self.threads: thread.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()
        self.pending = b""
        while not self.items.empty():
            try: self.items.get_nowait()
            except queue.Empty: break
        super().close()


class SequentialReader(io.RawIOBase):
    """Enforce a stream reference's capabilities even if its factory returns seekable IO."""
    def __init__(self, reader, context):
        super().__init__()
        self.reader, self.context = reader, context

    def readable(self): return True
    def seekable(self): return False
    def read(self, size=-1):
        self._checkClosed()
        self.context.check_canceled()
        if size < 0: raise io.UnsupportedOperation("顺序流必须分块读取。")
        return self.reader.read(min(size, CHUNK))

    def close(self):
        if not self.closed:
            try: self.reader.close()
            finally: super().close()


class MediaSession:
    def __init__(self, context, configured="", budget=None):
        self.context, self.configured = context, configured
        self.budget = budget or PROCESS_BUDGET
        self.entries, self.readers, self.cache = {}, [], {}
        self.memory_bytes, self.peak_bytes, self.closed = 0, 0, False

    def _entry(self, ref):
        if self.closed: raise ValueError("媒体资源会话已释放，请重新运行工作流。")
        ref.verify()
        entry = self.entries.get(ref.resource_id)
        if entry is None or entry["ref"] != ref:
            raise ValueError("媒体资源不属于本次执行或已释放；不能跨任务传递内存句柄。")
        return entry

    def verify(self, ref):
        ref.verify()
        if ref.storage != "file":
            entry = self._entry(ref)
            for dependency in entry.get("dependencies", ()):
                dependency.verify()
        return ref

    def register_stream(self, factory, info, dependencies=()):
        """Trusted producer API: factory(context) must create a fresh readable MP4 stream per call.

        No callbacks or live handles cross node ports or get saved into workflow JSON.
        Generated media does not need an intermediate video path.
        """
        self.context.check_canceled()
        if self.closed: raise ValueError("媒体资源会话已释放。")
        if not callable(factory): raise TypeError("流生产者必须是可重新打开的工厂，不能传一次性生成器。")
        duration = info.get("duration") if isinstance(info, dict) else None
        video = info.get("video") if isinstance(info, dict) else None
        if (type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 86400
                or not isinstance(video, dict) or type(info.get("audio")) is not bool
                or any(type(video.get(k)) is not int or not 0 < video[k] <= 16384 for k in ("width", "height"))):
            raise ValueError("流生产者必须提供有效时长、尺寸和音轨信息。")
        if any(not isinstance(dep, ResourceRef) or dep.storage != "file" for dep in dependencies):
            raise TypeError("流生产者依赖必须使用本地资源快照。")
        for dependency in dependencies: dependency.verify()
        ref = ResourceRef("", "video", 0, 0, "stream", uuid4().hex)
        self.entries[ref.resource_id] = {"ref": ref, "factory": factory, "info": copy.deepcopy(info),
                                         "dependencies": tuple(dependencies), "bytes": 0}
        return ref

    def info(self, ref):
        self.verify(ref)
        if ref.storage != "file": return copy.deepcopy(self._entry(ref)["info"])
        from .render import probe, resolve_tools
        _, ffprobe = resolve_tools(self.configured)
        return probe(ref.path, ffprobe)

    def source_file(self, ref):
        """Recover only an unchanged original behind our lossless transport."""
        for _ in range(32):
            self.verify(ref)
            if ref.storage == 'file':
                return ref
            entry = self._entry(ref)
            if 'factory' in entry or not isinstance(entry.get('source'), ResourceRef):
                raise ValueError('这个流没有可定位的原文件，无法用于多视频循环拼接。')
            ref = entry['source']
        raise ValueError('媒体传输引用嵌套过深。')

    def _mp4_reader(self, ref):
        if ref.storage != "file": return self.open_reader(ref)
        from .render import resolve_tools
        ffmpeg, _ = resolve_tools(self.configured)
        # delay_moov preserves the AAC priming/edit-list timing. empty_moov shifted test audio/video.
        reader = PipeReader([ffmpeg, "-nostdin", "-v", "error", "-i", ref.path,
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags",
            "frag_keyframe+delay_moov+default_base_moof", "-f", "mp4", "pipe:1"], self.context)
        self.readers.append(reader)
        return reader

    def open_reader(self, ref):
        self.verify(ref)
        if ref.storage == "file":
            reader = data_io.open(ref.path, "rb")
        else:
            entry = self._entry(ref)
            if ref.storage == "memory":
                reader = MemoryReader(entry["blocks"])
            elif "factory" in entry:
                reader = entry["factory"](self.context)
                if not callable(getattr(reader, "read", None)) or not callable(getattr(reader, "close", None)):
                    raise TypeError("流生产者必须返回可读取、可关闭的媒体流。")
                reader = SequentialReader(reader, self.context)
            elif entry["source"].storage == "file":
                return self._mp4_reader(entry["source"])
            else:
                source = self._entry(entry["source"])
                reader = MemoryReader(source["blocks"], seekable=False)
        self.readers.append(reader)
        return reader

    def convert(self, source, mode, limit_mb=128):
        self.context.check_canceled()
        self.verify(source)
        if mode not in {"memory", "stream"}: raise ValueError("媒体传输方式无效。")
        if type(limit_mb) is not int or not 16 <= limit_mb <= 512:
            raise ValueError("压缩缓存上限必须在 16–512 MB 之间。")
        if source.storage == mode:
            if mode == "memory" and self._entry(source)["bytes"] > limit_mb * MiB:
                raise MemoryError("已缓存的视频超过该节点的缓存上限。")
            return source
        if mode == "memory" and source.storage == "stream":
            underlying = self._entry(source).get("source")
            if underlying is not None and underlying.storage == "memory":
                return self.convert(underlying, mode, limit_mb)
        key = (source, mode)
        if key in self.cache:
            ref = self.cache[key]
            if mode == "memory" and self._entry(ref)["bytes"] > limit_mb * MiB:
                raise MemoryError("已缓存的视频超过该节点的缓存上限。")
            return ref
        info = self.info(source)
        ref = replace(source, storage=mode, resource_id=uuid4().hex)
        dependencies = ((source,) if source.storage == "file"
                        else self._entry(source).get("dependencies", ()))
        entry = {"ref": ref, "info": info, "source": source, "bytes": 0,
                 "dependencies": dependencies}
        if mode == "memory":
            blocks, count = [], 0
            try:
                with self._mp4_reader(source) as reader:
                    while True:
                        self.context.check_canceled()
                        data = reader.read(CHUNK)
                        if not data: break
                        if count + len(data) > limit_mb * MiB:
                            raise MemoryError("视频超过该节点的压缩缓存上限；请改用管道流。未写临时视频。")
                        self.budget.reserve(len(data))
                        count += len(data)
                        blocks.append(data)
                self.verify(source)
                if not count: raise ValueError("视频流没有输出有效的 MP4 数据。")
            except BaseException:
                self.budget.release(count)
                raise
            entry.update(blocks=tuple(blocks), bytes=count)
            self.memory_bytes += count
            self.peak_bytes = max(self.peak_bytes, self.memory_bytes)
            self.context.log(f"内存压缩 MP4：{count / MiB:.2f} MB；仅重新封装，没有重新编码。")
        self.entries[ref.resource_id] = entry
        self.cache[key] = ref
        return ref

    def close(self):
        if self.closed: return
        errors = []
        try:
            for reader in self.readers:
                try: reader.close()
                except Exception as error: errors.append(error)
        finally:
            self.budget.release(self.memory_bytes)
            self.memory_bytes = 0
            self.entries.clear()
            self.cache.clear()
            self.readers.clear()
            self.closed = True
        if errors: raise RuntimeError("媒体资源清理失败：" + str(errors[0])) from errors[0]

    def __enter__(self): return self
    def __exit__(self, error_type, error, traceback):
        try: self.close()
        except Exception:
            if error is None: raise
            self.context.log("媒体会话清理异常；已保留原始任务错误。")
