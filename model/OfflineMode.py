"""Startup-only application offline policy, not a hostile-code sandbox.

Install before importing SDKs, models or plugins. Python sockets, DNS, known
Qt client APIs and browser launches are guarded. Cached models and local pipe
IPC remain usable. Native DLLs/arbitrary child code require an OS firewall for
isolation; this module deliberately does not change system firewall settings.
"""

import functools
import json
import logging
import os
from pathlib import Path
import re
import shlex
import socket
import sys
import threading
import weakref
import webbrowser


OFFLINE_CONFIG_KEY = "offline_mode"
OFFLINE_ENV_KEY = "LZX_OFFLINE_MODE"
_enabled = False
_qt_installed = False
_reported = set()
_report_lock = threading.Lock()
_ipc_sockets = weakref.WeakSet()
_local = threading.local()
_network_scheme = re.compile(
    r"^(?:https?|ftps?|sftp|smb|tcp|udp|tls|rtsp|rtmp|srt|ws|wss|ssh|gopher):",
    re.IGNORECASE,
)


class OfflineModeError(PermissionError):
    """A requested operation is unavailable in the current offline session."""


def offline_setting(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是"}
    return bool(value)


def is_offline():
    return _enabled


def _deny(operation):
    # Never log URLs, request bodies, credentials, command lines or DNS names.
    with _report_lock:
        first = operation not in _reported
        _reported.add(operation)
    if first:
        logging.getLogger(__name__).warning("[离线模式] 已拦截：%s", operation)
    raise OfflineModeError(
        f"离线安全模式已阻止{operation}。如需联网，请关闭离线模式并重启程序。"
    )


def require_online(operation="联网请求"):
    if _enabled:
        _deny(operation)


def _internet_socket(sock):
    return (
        not getattr(_local, "creating_socketpair", False)
        and sock not in _ipc_sockets
        and getattr(sock, "family", None) in {socket.AF_INET, socket.AF_INET6}
    )


def _socket_guard(original):
    @functools.wraps(original)
    def guarded(sock, *args, **kwargs):
        if _enabled and _internet_socket(sock):
            _deny("网络套接字操作")
        return original(sock, *args, **kwargs)
    return guarded


def _blocked_call(operation):
    def blocked(*args, **kwargs):
        _deny(operation)
    return blocked


def _remote_target(value):
    text = os.fsdecode(value) if isinstance(value, bytes) else str(value)
    text = text.strip().strip('\"\'')
    if text.startswith("--") and "=" in text:
        text = text.split("=", 1)[1].strip('\"\'')
    return bool(_network_scheme.match(text)) or text.startswith(("\\\\", "//"))


def _check_process(executable, args):
    # Windows Popen audits the converted command-line string, not the caller's
    # original list. Parse for policy checks only; never rebuild execution args.
    if isinstance(args, (str, bytes)):
        try:
            arguments = shlex.split(os.fsdecode(args), posix=False)
        except ValueError:
            _deny("无法验证的外部命令")
    else:
        arguments = list(args or ())
    program = str(executable or (arguments[0] if arguments else ""))
    name = Path(program.strip('"')).stem.casefold()
    if name in {
        "chrome", "msedge", "firefox", "iexplore", "opera", "brave",
        "curl", "wget", "powershell", "pwsh", "bitsadmin", "ftp", "ssh",
    } or any(_remote_target(arg) for arg in arguments):
        _deny("外部联网程序")


def _audit(event, args):
    if not _enabled:
        return
    if event in {"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"}:
        if _internet_socket(args[0]):
            _deny("网络套接字操作")
    elif event in {
        "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
        "socket.getnameinfo",
    }:
        _deny("DNS 查询")
    elif event == "subprocess.Popen":
        _check_process(args[0], args[1])
    elif event == "os.system":
        _deny("外部命令执行")
    elif event in {"os.startfile", "os.startfile/2"} and _remote_target(args[0]):
        _deny("打开网络链接")
    elif event == "webbrowser.open":
        _deny("打开外部浏览器")


def enable_offline():
    """Idempotent, irreversible for this process; restart to go online."""
    global _enabled
    if _enabled:
        return
    _enabled = True
    for name in (
        "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK", OFFLINE_ENV_KEY,
    ):
        os.environ[name] = "1"
    os.environ["WANDB_MODE"] = "offline"
    sys.addaudithook(_audit)
    for name in ("connect", "connect_ex", "bind", "listen", "accept",
                 "send", "sendall", "sendto", "sendmsg"):
        original = getattr(socket.socket, name, None)
        if original is not None:
            setattr(socket.socket, name, _socket_guard(original))
    for name in ("create_connection", "getaddrinfo", "gethostbyname",
                 "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        setattr(socket, name, _blocked_call("DNS / 网络连接"))

    # Windows implements socketpair using TCP loopback. Only the genuine pair
    # creation and its returned private sockets are exempt, not localhost URLs.
    original_pair = socket.socketpair
    def local_socketpair(*args, **kwargs):
        _local.creating_socketpair = True
        try:
            pair = original_pair(*args, **kwargs)
            _ipc_sockets.update(pair)
            return pair
        finally:
            _local.creating_socketpair = False
    socket.socketpair = local_socketpair

    import ssl
    for name in ("send", "sendall", "write", "do_handshake"):
        setattr(ssl.SSLSocket, name, _socket_guard(getattr(ssl.SSLSocket, name)))
    for name in ("open", "open_new", "open_new_tab"):
        setattr(webbrowser, name, _blocked_call("打开外部浏览器"))


def configure_offline(config_path, argv=None, environ=None):
    """Read without rewriting any user setting, key or business record."""
    args = sys.argv if argv is None else argv
    environment = os.environ if environ is None else environ
    forced = "--offline" in args or offline_setting(environment.get(OFFLINE_ENV_KEY))
    path = Path(config_path)
    config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if forced or offline_setting(config.get(OFFLINE_CONFIG_KEY, False)):
        enable_offline()
    return _enabled


def install_qt_network_guard(QtCore, QtGui, QtNetwork):
    """Guard Python-facing Qt networking, preserving QLocalSocket pipe IPC."""
    global _qt_installed
    if not _enabled or _qt_installed:
        return
    for cls, methods in (
        (QtNetwork.QNetworkAccessManager,
         ("get", "head", "post", "put", "deleteResource", "sendCustomRequest")),
        (QtNetwork.QTcpSocket, ("connectToHost",)),
        (QtNetwork.QSslSocket, ("connectToHostEncrypted",)),
        (QtNetwork.QUdpSocket, ("writeDatagram",)),
        (QtNetwork.QHostInfo, ("lookupHost", "fromName")),
        (QtNetwork.QDnsLookup, ("lookup",)),
    ):
        for name in methods:
            setattr(cls, name, _blocked_call("Qt 联网请求"))
    original = QtGui.QDesktopServices.openUrl
    def open_url(url):
        if not url.isLocalFile() or _remote_target(url.toLocalFile()):
            try:
                _deny("打开网络链接")
            except OfflineModeError:
                return False
        return original(url)
    QtGui.QDesktopServices.openUrl = staticmethod(open_url)
    _qt_installed = True
