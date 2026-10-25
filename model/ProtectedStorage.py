"""Verified VC binding and copy/validate/commit migration, without passwords."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
from uuid import uuid4

from .PrivateDataCatalog import catalog, enumerate_files, no_reparse, PRIVATE_DIRECTORIES, PRIVATE_KEYS
from .VeraCryptVolumes import VeraCryptVolumes, VolumeError

APPLICATION = 'lzx.assistant-tool'
MARKER = 'storage.identity.json'
BOOTSTRAP = 'storage_bootstrap.json'
MIN_FREE = 256 * 1024 * 1024
WARN_FREE = 512 * 1024 * 1024


def valid_identity(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


class StorageUnavailable(OSError):
    pass


class StorageCapacityError(StorageUnavailable):
    """Space pressure blocks writes but must still allow verified cleanup."""
    pass


def read_marker(root):
    path = no_reparse(Path(root) / MARKER, Path(root))
    try:
        if path.stat().st_size > 16384: raise ValueError()
        value = json.loads(path.read_text(encoding='utf8'))
        if (not isinstance(value, dict) or value.get('application') != APPLICATION
                or value.get('version') != 1 or value.get('status') != 'complete'
                or not valid_identity(value.get('storage_id'), 32)
                or not valid_identity(value.get('volume_id'), 64)):
            raise ValueError()
    except (OSError, ValueError, KeyError):
        raise StorageUnavailable('加密卷内的数据标记缺失、损坏或版本不兼容。未加载空配置，请选择正确卷或恢复副本。') from None
    return value


def atomic_json(path, value):
    path = Path(path)
    fd, pending = tempfile.mkstemp(prefix='.storage-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush(); os.fsync(file.fileno())
        os.replace(pending, path)
    finally:
        if os.path.exists(pending): os.unlink(pending)


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as file:
        for block in iter(lambda: file.read(1024*1024), b''): digest.update(block)
    return digest.hexdigest()


class VolumeSession:
    def __init__(self, provider, volume, root, *, marker=None):
        self.provider, self.volume, self.root = provider, volume, Path(root).absolute()
        self.marker = marker
        self.available, self.failure, self.closed = True, '', False
        self.lock, self.lease = threading.RLock(), None
        self.verifier, self.marker_lease = None, None
        # An open directory handle denies ordinary rename/removal and prevents
        # normal dismount while app data is in use. Forced dismount is NOT safe.
        try:
            if hasattr(provider, 'driver'):
                self.lease = provider.driver.open_handle(self.root, directory=True)
                expected = volume.device + '\\' + self.root.relative_to(Path(volume.root)).as_posix().replace('/', '\\')
                if provider.driver.final_device_path(self.lease).casefold() != expected.casefold():
                    raise StorageUnavailable('数据目录句柄不属于已验证的 VC 卷。')
                if marker:
                    # This metadata is immutable for an active session. Deny
                    # write/delete sharing rather than repeatedly parsing it
                    # under the process-wide I/O lock on every file access.
                    self.marker_lease=provider.driver.open_handle(self.root/MARKER,shared_write=False)
                    if (provider.driver.final_device_path(self.marker_lease).casefold() != (expected+'\\'+MARKER).casefold()
                            or read_marker(self.root) != marker):
                        raise StorageUnavailable('数据身份标记与已验证的 VC 目录不一致。')
            if callable(getattr(provider,'open_verifier',None)):
                self.verifier=provider.open_verifier(volume)
        except BaseException:
            self.close();raise

    def _assert_available(self):
        with self.lock:
            if not self.available or self.closed:
                raise StorageUnavailable(self.failure or '加密存储会话已关闭；不会回退明文位置。')

    def check(self, path=None, *, write=False):
        self._assert_available()
        try:
            verifier=self.verifier
            volume = (verifier.check() if verifier is not None else
                      self.provider.verify(self.volume.root, identity=self.volume.volume_id,
                                               unique_id=self.volume.unique_id)
                      )
            if self.marker and self.marker_lease is None and read_marker(self.root) != self.marker:
                raise StorageUnavailable('数据身份发生改变，停止私有数据读写。')
            if path is not None and Path(path).absolute().is_relative_to(self.root):
                no_reparse(path, Path(volume.root))
        except (VolumeError, OSError, ValueError) as error:
            with self.lock:
                self.available = False
                self.failure = '加密数据卷不可用，私有数据读写已停止，不会另存明文。请重启并重新选择卷。\n' + str(error)
            raise StorageUnavailable(self.failure) from None
        # No shared lock is held during filesystem I/O. A different worker's
        # failure still invalidates this check before an approved path returns.
        self._assert_available()
        if write:
            self._capacity(volume, 0)
            self._assert_available()
        return self.root

    def _capacity(self, volume, required):
        try:
            free = shutil.disk_usage(volume.root).free
            container = volume.container.removeprefix('\\??\\')
            host_free = shutil.disk_usage(Path(container).anchor).free if len(container) >= 3 and container[1:3] in (':\\', ':/') else None
        except OSError:
            self.available = False
            self.failure = '无法读取加密卷或容器磁盘空间，已停止私有数据读写。请检查挂载状态。'
            raise StorageUnavailable(self.failure) from None
        if free < MIN_FREE + required:
            raise StorageCapacityError('加密卷空间不足：需保留256 MB安全余量'
                + (f'，本次写入还需 {required/1024**2:.1f} MB' if required else '')
                + '。已停止新增写入，请清理可再生成的缓存或扩容；配置和索引未删除。')
        if host_free is not None and host_free < MIN_FREE + required:
            raise StorageCapacityError('VC容器所在磁盘空间不足，已停止新增写入；请先释放磁盘空间。')
        return free

    def ensure_capacity(self, path, required):
        self.check(path, write=False)
        incoming=max(0,int(required)) if Path(path).absolute().is_relative_to(self.root) else 0
        self._capacity(self.volume,incoming)
        self._assert_available()

    def health(self):
        self.check()
        free=self._capacity(self.volume,0)
        self._assert_available()
        return free

    def available_space(self):
        self.check()
        try: return shutil.disk_usage(self.volume.root).free
        except OSError:
            self.available = False
            self.failure = '无法读取加密卷空间，请检查卷挂载状态。'
            raise StorageUnavailable(self.failure) from None

    def close(self):
        with self.lock:
            self.closed = True
            if self.verifier is not None:
                self.verifier.close();self.verifier=None
            if self.marker_lease is not None:
                self.provider.driver.close(self.marker_lease);self.marker_lease=None
            if self.lease is not None:
                self.provider.driver.close(self.lease); self.lease = None


def read_bootstrap(program_root):
    path = Path(program_root) / BOOTSTRAP
    if not path.exists(): return None
    try:
        if path.stat().st_size > 16384: raise ValueError()
        value = json.loads(path.read_text(encoding='utf8'))
        if (not isinstance(value, dict) or set(value) != {'version', 'mode', 'storage_id', 'volume_id'} or value['version'] != 1
                or value['mode'] != 'veracrypt' or not valid_identity(value.get('storage_id'), 32)
                or not valid_identity(value.get('volume_id'), 64)): raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        raise StorageUnavailable('VC 启动绑定损坏，拒绝回退旧明文配置。请恢复 storage_bootstrap.json 或重新配置。') from None
    return value


def migrate(program_root, provider, volume, report=lambda *_: None, *, resume=None):
    """Call only before stores/plugins start, after explicit user approval."""
    program_root = Path(program_root).absolute()
    target = Path(volume.root) / 'AssistantToolData'
    provider.verify(volume.root, identity=volume.volume_id, unique_id=volume.unique_id)
    no_reparse(target, Path(volume.root))
    if target.exists(): raise StorageUnavailable('目标数据目录已存在，不覆盖；请加载已有数据或选择另一卷。')
    locations = catalog(program_root)
    files, size = enumerate_files(locations)
    if not any(relative.as_posix() == 'config.json' for _path, relative in files):
        raise StorageUnavailable('原配置 config.json 不存在；为避免看似丢失密钥，本次不创建空配置。')
    stage = Path(resume).absolute() if resume is not None else Path(volume.root) / ('.AssistantToolData-migration-' + uuid4().hex)
    copied_bytes = 0
    if resume is not None:
        no_reparse(stage, Path(volume.root))
        if (stage.parent != Path(volume.root) or not re.fullmatch(r'\.AssistantToolData-migration-[0-9a-f]{32}',stage.name)
                or not stage.is_dir() or (stage/MARKER).exists()):
            raise StorageUnavailable('只能继续本卷内未提交的迁移副本；不能覆盖已完成数据或其他目录。')
        copied_bytes = sum(min((stage/relative).stat().st_size, source.stat().st_size)
                           for source,relative in files if (stage/relative).is_file())
    if shutil.disk_usage(volume.root).free < max(0,size-copied_bytes) * 1.2 + 256*1024*1024:
        raise StorageUnavailable('加密卷空间不够迁移（含校验与运行余量）；原数据未改动。')
    stage.mkdir(exist_ok=resume is not None)
    session = VolumeSession(provider, volume, stage)
    manifest, source_stamps = [], []
    try:
        for index, (source, relative) in enumerate(files):
            session.check(stage / relative, write=True)
            report(f'复制并校验私有数据 {index+1}/{len(files)}', int(index*95/max(1,len(files))))
            destination = stage / relative; destination.parent.mkdir(parents=True, exist_ok=True)
            stat = source.stat(); stamp = (stat.st_size, stat.st_mtime_ns)
            with source.open('rb') as signature_file: signature=signature_file.read(16)
            database = signature == b'SQLite format 3\0'
            if database:
                original = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
                copied = sqlite3.connect(destination)
                try:
                    deadline=time.monotonic()+120
                    def backup_progress(_status, _remaining, _total):
                        session.check(destination,write=True)
                        if time.monotonic()>deadline:
                            raise StorageUnavailable('数据库快照超时；请关闭使用该配置的外部客户端后重试。原数据未删除。')
                    original.backup(copied,pages=256,progress=backup_progress)
                    if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise StorageUnavailable('数据库完整性校验失败，停止迁移。')
                finally: copied.close(); original.close()
                digest = hash_file(destination)
            else:
                digest = hash_file(source)
                # Retry/resume never trusts former bytes merely by timestamp.
                if not destination.exists() or hash_file(destination)!=digest:
                    shutil.copy2(source, destination)
                if hash_file(destination) != digest: raise StorageUnavailable('文件校验失败，未启用加密位置。')
            after = source.stat()
            if not database and (after.st_size, after.st_mtime_ns) != stamp:
                raise StorageUnavailable('迁移期间原数据变化，请关闭其他辅助工具后重试；原数据仍保留。')
            # SQLite backup is a transactional snapshot, including committed
            # WAL. SHM is a volatile lock/index file and even a read connection
            # can update it; its timestamp is not a durable data revision.
            if not database:
                source_stamps.append((source, stamp))
            elif relative.parts[0] != 'CodexAccountProfiles':
                # App databases should stay stable once the user closed the
                # app. Only external Codex client archives may remain live;
                # those are explicitly point-in-time SQLite snapshots.
                source_stamps.append((source,(after.st_size,after.st_mtime_ns)))
                wal=source.with_name(source.name+'-wal')
                if wal.exists():
                    value=wal.stat();source_stamps.append((wal,(value.st_size,value.st_mtime_ns)))
            manifest.append({'relative': relative.as_posix(), 'bytes': destination.stat().st_size, 'sha256': digest})
        for source, stamp in source_stamps:
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != stamp:
                raise StorageUnavailable('校验结束前原数据发生变化，未切换数据位置。')
        config_file=stage/'config.json'
        config=json.loads(config_file.read_text(encoding='utf-8-sig'))
        original_config=config.copy()
        for key in PRIVATE_KEYS:
            raw=config.get(key)
            if not isinstance(raw,str) or not raw.strip():continue
            original=Path(raw).expanduser()
            if not original.is_absolute():original=program_root/original
            for location in locations:
                try:relative=original.absolute().relative_to(Path(location.source))
                except ValueError:continue
                config[key]=(Path(location.relative)/relative).as_posix();break
        if config!=original_config:
            # Preserve exact former bytes INSIDE the encrypted volume before
            # rewriting only registered private-location settings, never keys.
            shutil.copy2(config_file,stage/'migration.original-config.json')
            atomic_json(config_file,config)
            for entry in manifest:
                if entry['relative']=='config.json':
                    entry.update(bytes=config_file.stat().st_size,sha256=hash_file(config_file))
        inventory_file=stage/'InventoryState.json'
        if inventory_file.exists():
            state=json.loads(inventory_file.read_text(encoding='utf-8-sig')); changed=False
            for person in state.get('people',[]):
                raw=person.get('avatar_path')
                if not raw:continue
                for location in locations:
                    if Path(raw).absolute()==Path(location.source):
                        person['avatar_path']=str(target/location.relative);changed=True;break
            if changed:
                shutil.copy2(inventory_file,stage/'migration.original-inventory.json')
                atomic_json(inventory_file,state)
                for entry in manifest:
                    if entry['relative']=='InventoryState.json':entry.update(bytes=inventory_file.stat().st_size,sha256=hash_file(inventory_file))
        identity = {'application': APPLICATION, 'version': 1, 'status': 'complete',
            'storage_id': uuid4().hex, 'volume_id': volume.volume_id}
        atomic_json(stage / 'migration.manifest.json', {'version': 1, 'files': manifest,
            'redirects': [{'source': x.source, 'relative': x.relative} for x in locations]})
        atomic_json(stage / MARKER, identity)
        session.check(write=True)
        session.close()  # Release deny-delete directory lease before rename.
        provider.verify(volume.root, identity=volume.volume_id, unique_id=volume.unique_id)
        stage.rename(target)
        report('复制、校验完成；旧明文数据和旧备份仍保留。', 100)
        return target, identity
    finally:
        session.close()
        # Incomplete files stay INSIDE VC for inspection/recovery. No source
        # deletion and no sensitive backup is made outside the encrypted volume.


def initialize_fresh(program_root, provider, volume, initial_config, report=lambda *_: None):
    """Explicit first-use only; never substitute for missing existing VC data."""
    from .FirstRun import first_use_state
    if first_use_state(program_root) != 'fresh':
        raise StorageUnavailable('存在配置、启动绑定或旧私有数据，不能新建空白配置顶替；请恢复或迁移已有数据。')
    if not isinstance(initial_config, dict):
        raise ValueError('首次配置必须是对象。')
    seed = json.loads(json.dumps(initial_config, ensure_ascii=False, allow_nan=False))
    target = Path(volume.root) / 'AssistantToolData'
    provider.verify(volume.root, identity=volume.volume_id, unique_id=volume.unique_id)
    no_reparse(target, Path(volume.root))
    if target.exists():
        raise StorageUnavailable('加密卷已有数据目录，未覆盖；请选择“使用已有数据”。')
    if shutil.disk_usage(volume.root).free < MIN_FREE:
        raise StorageUnavailable('加密卷空间不足，首次配置至少需要256MiB运行余量。')
    stage = Path(volume.root) / ('.AssistantToolData-initialize-' + uuid4().hex)
    stage.mkdir()
    session = VolumeSession(provider, volume, stage)
    try:
        report('在已验证的加密卷内新建首次配置', 20)
        session.check(stage / 'config.json', write=True)
        atomic_json(stage / 'config.json', seed)
        if json.loads((stage / 'config.json').read_text(encoding='utf-8')) != seed:
            raise StorageUnavailable('首次配置校验失败，未启用该目录。')
        marker = {'application': APPLICATION, 'version': 1, 'status': 'complete',
                  'storage_id': uuid4().hex, 'volume_id': volume.volume_id}
        session.check(write=True)
        atomic_json(stage / 'migration.manifest.json', {'version': 1, 'origin': 'first_use',
            'files': [{'relative': 'config.json', 'bytes': (stage / 'config.json').stat().st_size,
                       'sha256': hash_file(stage / 'config.json')}], 'redirects': []})
        atomic_json(stage / MARKER, marker)
        report('首次配置已校验，准备启用', 90)
        session.check(write=True)
        session.close()
        provider.verify(volume.root, identity=volume.volume_id, unique_id=volume.unique_id)
        if first_use_state(program_root) != 'fresh' or target.exists():
            raise StorageUnavailable('初始化期间出现其他配置或目标数据，未覆盖；请重新选择。')
        stage.rename(target)
        report('配置已在密盘内创建，没有外部明文副本。', 100)
        return target, marker
    finally:
        # Incomplete directories remain only inside the verified volume.
        session.close()


def bind(program_root, provider, volume, root, expected=None):
    from .AppDataStorage import ApplicationStorage, bind_application_storage
    import app_paths
    marker = read_marker(root)
    if marker['volume_id'] != volume.volume_id or expected and any(marker[k] != expected[k] for k in ('storage_id','volume_id')):
        raise StorageUnavailable('选中卷或数据身份与启动绑定不符，未读取任何旧明文配置。')
    session = VolumeSession(provider, volume, root, marker=marker)
    try:
        session.check(write=True)
        config_file = no_reparse(Path(root)/'config.json', Path(root))
        try:
            if config_file.stat().st_size > 32 * 1024 * 1024:
                raise ValueError()
            if not isinstance(json.loads(config_file.read_text(encoding='utf-8-sig')), dict):
                raise ValueError()
        except (OSError, ValueError):
            raise StorageUnavailable('卷内主配置缺失或损坏，拒绝生成空配置。请使用卷内迁移原副本或旧原文件恢复。') from None
        manifest_file = no_reparse(Path(root)/'migration.manifest.json', Path(root))
        if manifest_file.stat().st_size > 128 * 1024 * 1024:
            raise StorageUnavailable('迁移位置清单过大，未加载配置。')
        manifest = json.loads(manifest_file.read_text(encoding='utf8'))
        if (not isinstance(manifest, dict) or manifest.get('version') != 1
                or not isinstance(manifest.get('redirects'), list) or len(manifest['redirects']) > 10000):
            raise StorageUnavailable('迁移位置清单损坏，未加载配置。')
        redirects = []
        for entry in manifest['redirects']:
            if (not isinstance(entry, dict) or not isinstance(entry.get('relative'), str)
                    or not isinstance(entry.get('source'), str) or not Path(entry['source']).is_absolute()):
                raise StorageUnavailable('迁移位置清单损坏。')
            relative = Path(entry['relative'])
            if relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
                raise StorageUnavailable('迁移位置清单损坏。')
            redirects.append((entry['source'], Path(root)/relative))
        # Include missing defaults, so newly created files don't go plaintext.
        for name in PRIVATE_DIRECTORIES:
            redirects.append((Path(program_root)/name, Path(root)/name))
        for path in Path(program_root).glob('*.json'):
            if path.name not in ('config.example.json', 'BUILD_INFO.json', BOOTSTRAP): redirects.append((path, Path(root)/path.name))
        storage = ApplicationStorage(root, guard=session, redirects=redirects)
        storage.directory_at(Path(root)/'Temp').ensure()
        app_paths.bind_data_root(root)
        bind_application_storage(storage)
        # Libraries honoring tempfile/environment now write into the volume.
        temporary = str(Path(root)/'Temp')
        tempfile.tempdir = temporary
        for key in ('TEMP', 'TMP', 'TMPDIR'): os.environ[key] = temporary
        os.environ['CODEX_DESKTOP_SWITCHER_DATA_DIR'] = str(Path(root)/'CodexAccountProfiles')
        return storage
    except BaseException:
        session.close(); raise


def commit_bootstrap(program_root, marker):
    # Only nonsensitive policy/identity outside VC; no passwords/paths/tokens.
    atomic_json(Path(program_root)/BOOTSTRAP, {'version': 1, 'mode': 'veracrypt',
        'storage_id': marker['storage_id'], 'volume_id': marker['volume_id']})
