"""Bind an installed executable and its actual user-data directory together."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import psutil
from model.AppDataStorage import data_file


class ClientSelectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AntigravityClient:
    name: str
    executable: Path
    user_data: Path
    running: bool = False
    auth_kind: str = 'ide'
    version: str = ''

    @property
    def database(self):
        return self.user_data / "User/globalStorage/state.vscdb"

    @property
    def identity(self):
        value = str(self.executable).casefold() + '\0' + str(self.user_data).casefold()
        if self.auth_kind != 'ide':
            value += '\0' + self.auth_kind
        return hashlib.sha256(value.encode('utf8')).hexdigest()[:24]

    @property
    def title(self):
        return self.name + (' ' + self.version if self.version else '') + ('（正在运行）' if self.running else '') + ' · ' + str(self.user_data)

    @property
    def auth_source(self):
        return ('Windows 凭据管理器 · gemini:antigravity（当前 Windows 用户共享）'
                if self.auth_kind == 'windows' else str(self.database))


def command_user_data(arguments):
    for index, value in enumerate(arguments):
        if value.startswith('--user-data-dir='):
            return Path(value.partition('=')[2]).expanduser().resolve()
        if value == '--user-data-dir' and index + 1 < len(arguments):
            return Path(arguments[index + 1]).expanduser().resolve()
    return None


def installed_client(executable):
    executable = Path(executable).resolve()
    product_file = executable.parent / 'resources/app/product.json'
    try:
        if not executable.is_file():
            return None
        if not product_file.is_file():
            from .package_metadata import packaged_metadata
            metadata = packaged_metadata(executable)
            if metadata is None:
                return None
            return AntigravityClient(metadata['name'], executable,
                (Path(os.environ['APPDATA']) / metadata['name']).resolve(),
                auth_kind=metadata['auth_kind'], version=metadata['version'])
        product = json.loads(data_file(product_file).read_text(encoding='utf8'))
        name = product.get('nameShort')
        app = product.get('applicationName')
        if name not in ('Antigravity', 'Antigravity IDE') or app not in ('antigravity', 'antigravity-ide'):
            return None
        portable = executable.parent / 'data/user-data'
        user_data = portable if portable.is_dir() else Path(os.environ['APPDATA']) / name
        return AntigravityClient(name, executable, user_data.resolve())
    except (OSError, ValueError, KeyError, TypeError):
        return None


def discover_clients():
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home())
    candidates = [local / 'Programs/antigravity/Antigravity.exe',
                  local / 'Programs/Antigravity IDE/Antigravity IDE.exe']
    running = []
    unknown_running = False
    try:
        for process in psutil.process_iter(['name']):
            if (process.info.get('name') or '').casefold() not in ('antigravity.exe', 'antigravity ide.exe'):
                continue
            try:
                arguments = process.cmdline()
                if any(value.startswith('--type=') for value in arguments):
                    continue
                exe = Path(process.exe()).resolve()
                candidates.append(exe)
                running.append((exe, command_user_data(arguments)))
            except (OSError, psutil.Error):
                unknown_running = True
    except (OSError, psutil.Error) as error:
        raise ClientSelectionError('无法检查正在运行的 Antigravity，未自动读取旧客户端账号。') from error
    result = {}
    for executable in candidates:
        client = installed_client(executable)
        if client is None:
            if any(exe == executable for exe, _custom in running):
                unknown_running = True
            continue
        active = [(exe, data) for exe, data in running if exe == client.executable]
        if active:
            for _, custom in active:
                value = AntigravityClient(client.name, client.executable, custom or client.user_data, True,
                    client.auth_kind, client.version)
                result[value.identity] = value
        else:
            result.setdefault(client.identity, client)
    if unknown_running:
        raise ClientSelectionError('正在运行的 Antigravity 版本或数据来源无法确认，未改读另一套旧 IDE 的账号。')
    return sorted(result.values(), key=lambda item: (not item.running, item.name, str(item.user_data)))


def default_client():
    clients = discover_clients()
    active = [item for item in clients if item.running]
    if len(active) == 1:
        return active[0]
    if len(clients) == 1:
        return clients[0]
    if not clients:
        raise ClientSelectionError('没有找到可确认产品信息的 Antigravity 客户端；未读取其他软件或旧目录的账号。')
    raise ClientSelectionError('检测到多个 Antigravity 客户端，请在插件中明确选择客户端，不能默认读取第一套旧缓存。')
