"""Host-owned data I/O. A development contract, not a Python security sandbox.

Namespaces use relative names. Legacy stores bind their existing locations through
file_at/directory_at so this refactor never moves, resets or encrypts user data.
External media tools may receive a broker-provided path when they cannot use a
Python handle. VeraCrypt selection and data migration are deliberately separate.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import builtins
import weakref

_LOCKS = {}
_GUARD = threading.RLock()
_APPLICATION_STORAGE = None


def _lock(path):
    key = os.path.normcase(str(Path(path).resolve()))
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class ApplicationStorage:
    def __init__(self, root, *, guard=None, redirects=()):
        # Binding is immutable for this instance; no live-volume hot switching.
        self.root = Path(root).resolve()
        self.guard = guard
        self.redirects = sorted(((Path(a).absolute(), Path(b).absolute()) for a, b in redirects),
                                key=lambda pair: len(str(pair[0])), reverse=True)
        # Resolve lexical redirects with strings. Python 3.10 Path comparison
        # lazily invokes _flavour.casefold_parts on every newly created parent;
        # a failure there stopped long-running exports in the field. These
        # redirects need no filesystem access or Path comparison at all.
        self._redirect_names = [(os.path.normcase(str(a)), len(a.parts), len(a.name), b)
                                for a, b in self.redirects]
        self.connections = weakref.WeakSet()

    @property
    def protected(self):
        return self.guard is not None

    def resolve(self, path):
        value = Path(os.path.abspath(path))
        if self.protected:
            from .PrivateDataPaths import remounted_data_path
            value = Path(remounted_data_path(value,self.root))
        lexical = os.path.normcase(str(value))
        parent, name = os.path.split(lexical)
        for original, depth, name_length, destination in self._redirect_names:
            old_parent, old_name = os.path.split(original)
            if parent == old_parent and (name.startswith(old_name + '.')
                    or name in {old_name+'-wal', old_name+'-shm', old_name+'-journal'}):
                return destination.with_name(destination.name + value.name[name_length:])
            if lexical == original:
                return destination
            prefix = original.rstrip(os.sep) + os.sep
            if lexical.startswith(prefix):
                return destination.joinpath(*value.parts[depth:])
        return value

    def check(self, path=None, *, write=False):
        value = self.resolve(path) if path is not None else self.root
        if self.guard:
            self.guard.check(value, write=write)
        return value

    def close(self):
        # Connections are normally owned/closed by their worker. Do not close
        # another thread's SQLite connection during a failure notification.
        for connection in tuple(self.connections):
            try: connection.close()
            except sqlite3.Error: pass
        if self.guard: self.guard.close()

    def check_capacity(self, path, required):
        value = self.resolve(path)
        if self.guard and hasattr(self.guard, 'ensure_capacity'):
            # ensure_capacity already verifies live identity, boundaries and
            # free space. Do not duplicate that entire check before each write.
            self.guard.ensure_capacity(value, required)
        else:
            self.check(value,write=True)
        return value

    def for_plugin(self, plugin_id):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", str(plugin_id)):
            raise ValueError("插件数据命名空间无效。")
        return ManagedDirectory(self.root / "PluginData" / plugin_id, storage=self)

    def file_at(self, path):
        """Host adapter for reviewed, existing/user-selected data locations."""
        return ManagedFile(path, storage=self)

    def directory_at(self, path):
        return ManagedDirectory(path, storage=self)


def bind_application_storage(storage):
    global _APPLICATION_STORAGE
    with _GUARD:
        if _APPLICATION_STORAGE is not None:
            raise RuntimeError('存储服务已初始化，不能在运行中切换数据卷；请重启。')
        _APPLICATION_STORAGE = storage
    return storage


def application_storage():
    global _APPLICATION_STORAGE
    with _GUARD:
        if _APPLICATION_STORAGE is None:
            import app_paths
            _APPLICATION_STORAGE = ApplicationStorage(app_paths.DATA_ROOT)
        return _APPLICATION_STORAGE


def data_file(path):
    return application_storage().file_at(path)


def data_directory(path):
    return application_storage().directory_at(path)


class ManagedDirectory:
    def __init__(self, path, *, storage=None):
        self.storage = storage
        self.path = storage.resolve(path) if storage else Path(path).resolve()

    def _check(self, *, write=False):
        if self.storage: self.storage.check(self.path, write=write)

    def exists(self):
        self._check()
        return self.path.exists()

    def _child(self, name):
        self._check()
        raw = str(name)
        relative = Path(raw)
        if not raw or relative.is_absolute() or relative.drive or ".." in relative.parts or ":" in raw:
            raise ValueError("数据名称必须是命名空间内的相对名称。")
        target = (self.path / relative).resolve()
        if target == self.path or self.path not in target.parents:
            raise ValueError("数据路径超出插件命名空间。")
        return target

    def file(self, name):
        return ManagedFile(self._child(name), storage=self.storage)

    def directory(self, name):
        return ManagedDirectory(self._child(name), storage=self.storage)

    def ensure(self, mode=0o777, *, parents=True, exist_ok=True):
        self._check(write=True)
        self.path.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)
        return self.path

    def remove(self):
        self._check()
        self.path.rmdir()

    @contextmanager
    def temporary(self, *, prefix=".data-", suffix=""):
        self.ensure()
        temporary = tempfile.TemporaryDirectory(prefix=prefix, suffix=suffix, dir=self.path)
        if self.storage and self.storage.protected:
            temporary = GuardedTemporaryDirectory(temporary, self.storage)
        with temporary as path:
            yield ManagedDirectory(path, storage=self.storage)


class ManagedFile:
    def __init__(self, path, *, storage=None):
        # Keep lexical final filenames: an atomic replace must replace a link,
        # not silently dereference it and overwrite some different target file.
        self.storage = storage
        self.path = storage.resolve(path) if storage else Path(os.path.abspath(path))

    def _check(self, *, write=False):
        if self.storage: self.storage.check(self.path, write=write)

    def exists(self):
        self._check()
        return self.path.exists()

    def read_text(self, encoding="utf-8", **options):
        with self.open("r", encoding=encoding, **options) as stream:
            return stream.read()

    def read_bytes(self):
        with self.open("rb") as stream:
            return stream.read()

    def read_json(self, *, encoding="utf-8"):
        # Corruption propagates; missing files are not silently replaced by {}.
        return json.loads(self.read_text(encoding))

    @contextmanager
    def open(self, mode="rb", **options):
        self._check(write=any(flag in mode for flag in "wax+"))
        if any(flag in mode for flag in "wax+"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open(mode, **options) as stream:
            yield GuardedHandle(stream, self) if self.storage and self.storage.protected else stream

    @contextmanager
    def atomic_writer(self, *, binary=False, encoding="utf-8", newline=None, prefix=".data-"):
        # Temp/WAL/SHM and the final file always share the same data directory.
        with _lock(self.path):
            self._check(write=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=self.path.parent)
            pending_descriptor = descriptor
            try:
                stream = os.fdopen(descriptor, "wb" if binary else "w",
                                   **({} if binary else {"encoding": encoding, "newline": newline}))
                pending_descriptor = None
                with stream:
                    yield GuardedHandle(stream, self) if self.storage and self.storage.protected else stream
                    self._check(write=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._check(write=True)
                os.replace(temporary, self.path)
            finally:
                if pending_descriptor is not None:
                    try:
                        os.close(pending_descriptor)
                    except OSError:
                        pass
                # Never follow a reused drive letter for cleanup after loss.
                cleanup_safe = True
                if self.storage and self.storage.protected:
                    try: self.storage.check(temporary)
                    except OSError: cleanup_safe = False
                if cleanup_safe and os.path.exists(temporary):
                    os.unlink(temporary)

    def write_text(self, text, encoding="utf-8", **options):
        with self.atomic_writer(encoding=encoding, **options) as stream:
            return stream.write(text)

    def write_bytes(self, value):
        with self.atomic_writer(binary=True) as stream:
            return stream.write(value)

    def write_text_direct(self, text, encoding='utf-8', **options):
        """Legacy host atomic transactions already own a unique temp file."""
        with self.open('w',encoding=encoding,**options) as stream:
            return stream.write(text)

    def write_bytes_direct(self, value):
        with self.open('wb') as stream:
            return stream.write(value)

    def write_json(self, value, *, encoding="utf-8", **options):
        options.setdefault("ensure_ascii", False)
        # json.dump emits thousands of tiny writes. Serialize once so every
        # actual guarded I/O remains verified without thousands of driver calls.
        serialized = json.dumps(value, **options)
        with self.atomic_writer(encoding=encoding) as stream:
            stream.write(serialized)

    def connect(self, *, timeout=5, read_only=False, **options):
        """Create the connection here; plugin owns SQL/transaction/close policy."""
        self._check(write=not read_only)
        if not read_only: self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.storage and self.storage.protected:
            options['factory'] = GuardedConnection
        database = str(self.path)
        if read_only:
            database = self.path.as_uri() + '?mode=ro'
            options['uri'] = True
        connection = sqlite3.connect(database, timeout=timeout, **options)
        if isinstance(connection, GuardedConnection):
            connection.managed = self
            self.storage.connections.add(connection)
            connection.execute('PRAGMA temp_store=MEMORY')
        return connection

    def copy_from(self, source):
        source = self.storage.check(source) if self.storage else Path(source)
        with source.open("rb") as reader, self.atomic_writer(binary=True) as writer:
            shutil.copyfileobj(reader, writer)
        self._check(write=True)
        shutil.copystat(source, self.path)
        return self.path

    def remove(self, *, missing_ok=False):
        with _lock(self.path):
            self._check()  # Verified cleanup must work when the disk is low.
            self.path.unlink(missing_ok=missing_ok)

    def unlink(self, *, missing_ok=False):
        return self.remove(missing_ok=missing_ok)

    def touch(self, mode=0o666, exist_ok=True):
        self._check(write=True)
        self.path.touch(mode=mode, exist_ok=exist_ok)


class GuardedHandle:
    def __init__(self, handle, managed): self.handle, self.managed = handle, managed
    def write(self, value):
        size = len(value.encode(getattr(self.handle, 'encoding', None) or 'utf8',
                                errors=getattr(self.handle, 'errors', None) or 'strict')) if isinstance(value, str) else memoryview(value).nbytes
        self.managed.storage.check_capacity(self.managed.path, size)
        return self.handle.write(value)
    def writelines(self, lines):
        for line in lines: self.write(line)
    def truncate(self, size=None):
        target = self.handle.tell() if size is None else size
        current = os.fstat(self.handle.fileno()).st_size
        self.managed.storage.check_capacity(self.managed.path, max(0,target-current))
        return self.handle.truncate(size)
    def __getattr__(self, name):
        value = getattr(self.handle, name)
        if callable(value) and name in {'read', 'readline', 'readlines', 'readinto', 'seek', 'write', 'writelines', 'truncate', 'flush'}:
            def call(*args, **kwargs):
                self.managed._check(write=name in {'write', 'writelines', 'truncate', 'flush'})
                return value(*args, **kwargs)
            return call
        return value
    def __enter__(self): return self
    def __exit__(self, *args): return self.handle.__exit__(*args)
    def __iter__(self): return self
    def __next__(self):
        self.managed._check()
        return next(self.handle)


class GuardedTemporaryDirectory:
    def __init__(self,temporary,storage):
        self.temporary,self.storage=temporary,storage
        self._finalizer=weakref.finalize(self,self._cleanup,temporary,storage)
    def __getattr__(self,name): return getattr(self.temporary,name)
    def __enter__(self): return self.temporary.name
    def __exit__(self,*args): self.cleanup()
    def cleanup(self): self._finalizer()
    @staticmethod
    def _cleanup(temporary,storage):
        try: storage.check(temporary.name)
        except OSError:
            # A drive letter can be reused. Never let tempfile's late finalizer
            # remove data from a different filesystem after losing the volume.
            temporary._finalizer.detach()
            return
        temporary.cleanup()


class GuardedNamedTemporaryFile:
    """Close the original handle, but never unlink a reused drive letter."""
    def __init__(self, temporary, storage, delete):
        self.name, self.delete = temporary.name, delete
        self.file = GuardedHandle(temporary.file, storage.file_at(self.name))
        self._finalizer = weakref.finalize(self, self._cleanup, temporary, storage, delete)
    def __getattr__(self, name): return getattr(self.file, name)
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self._finalizer()
    @staticmethod
    def _cleanup(temporary, storage, delete):
        temporary.close()  # Created with delete=False: this only closes its FD.
        if not delete: return
        try: storage.check(temporary.name)
        except OSError: return
        Path(temporary.name).unlink(missing_ok=True)


class GuardedCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        self.connection.managed._check(write=True)
        return super().execute(*args, **kwargs)
    def executemany(self, *args, **kwargs):
        self.connection.managed._check(write=True)
        return super().executemany(*args, **kwargs)
    def executescript(self, *args, **kwargs):
        self.connection.managed._check(write=True)
        return super().executescript(*args, **kwargs)


class GuardedConnection(sqlite3.Connection):
    def cursor(self, *args, **kwargs):
        self.managed._check()
        return super().cursor(factory=GuardedCursor)
    # GuardedCursor verifies immediately before executing. Calling cursor()
    # here would repeat a complete live-volume check for the same SQL command.
    def execute(self, *args, **kwargs): return super().cursor(factory=GuardedCursor).execute(*args, **kwargs)
    def executemany(self, *args, **kwargs): return super().cursor(factory=GuardedCursor).executemany(*args, **kwargs)
    def executescript(self, *args, **kwargs): return super().cursor(factory=GuardedCursor).executescript(*args, **kwargs)
    def commit(self):
        self.managed._check(write=True)
        return super().commit()
    def __exit__(self, kind, value, trace):
        if kind is None: self.commit()
        else: self.rollback()
        return False
    def close(self):
        try: return super().close()
        finally:
            if hasattr(self, 'managed'): self.managed.storage.connections.discard(self)


class HostFileIO:
    """Reviewed legacy grants; creation/opening still happens in this host layer.

    These adapters preserve external-tool paths, private-credential permissions,
    OS file-descriptor semantics and existing cleanup/rollback exactly. New data
    stores should use context.storage.file(relative_name), not arbitrary paths.
    No monkey-patching and no claim to restrict trusted same-process Python code.
    """
    _operations = {
        "open": (builtins, "open"),
        "open_fd": (os, "open"),
        **{name: (tempfile, name) for name in
           ("mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryDirectory")},
        **{name: (os, name) for name in
           ("replace", "rename", "remove", "unlink", "mkdir", "makedirs", "symlink", "link")},
        **{name: (shutil, name) for name in
           ("copy", "copy2", "copyfile", "copytree", "move", "rmtree")},
    }

    def __getattr__(self, name):
        if name not in self._operations:
            raise AttributeError(name)
        module, operation = self._operations[name]
        def granted_operation(*args, **kwargs):
            storage = application_storage()
            if not storage.protected:
                return getattr(module, operation)(*args, **kwargs)
            storage.check()
            values = list(args)
            if module is tempfile:
                # No system-temp fallback for application-controlled secrets.
                if kwargs.get('dir') is None:
                    kwargs['dir'] = str(storage.root / 'Temp')
                    storage.directory_at(kwargs['dir']).ensure()
                else:
                    kwargs['dir'] = str(storage.check(kwargs['dir'], write=True))
            else:
                positions = (0, 1) if name in {'replace', 'rename', 'copy', 'copy2', 'copyfile', 'copytree', 'move', 'link', 'symlink'} else (0,)
                for index in positions:
                    if index < len(values) and isinstance(values[index], (str, bytes, os.PathLike)):
                        values[index] = str(storage.check(values[index], write=name not in {'open', 'open_fd', 'remove', 'unlink', 'rmtree'} or index > 0))
                for key in ('file', 'path', 'src', 'dst', 'filename'):
                    if key in kwargs and isinstance(kwargs[key], (str, os.PathLike)):
                        kwargs[key] = str(storage.check(kwargs[key], write=name not in {'remove', 'unlink', 'rmtree'}))
                if name in {'copy', 'copy2', 'copyfile', 'copytree'}:
                    source = Path(values[0] if values else kwargs['src'])
                    destination = Path(values[1] if len(values)>1 else kwargs['dst'])
                    if destination.is_relative_to(storage.root):
                        size = (sum(p.stat().st_size for p in source.rglob('*') if p.is_file())
                                if source.is_dir() else source.stat().st_size)
                        storage.check_capacity(destination, size)
            delete_temporary = False
            if name == 'NamedTemporaryFile':
                # NamedTemporaryFile positional arguments include delete/dir.
                # Current host callers use named arguments; reject ambiguity.
                if len(values) > 5:
                    raise ValueError('加密模式临时文件请使用命名参数 delete 和 dir。')
                delete_temporary = kwargs.get('delete', True)
                kwargs['delete'] = False
            result = getattr(module, operation)(*values, **kwargs)
            if name == 'open':
                return GuardedHandle(result, storage.file_at(result.name))
            if name == 'TemporaryDirectory':
                return GuardedTemporaryDirectory(result,storage)
            if name == 'NamedTemporaryFile':
                return GuardedNamedTemporaryFile(result, storage, delete_temporary)
            return result
        return granted_operation


data_io = HostFileIO()
