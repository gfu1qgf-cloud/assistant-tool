import sys
from pathlib import Path


def _resolve_app_root() -> Path:
    """Return the directory that should hold user-managed runtime files."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = _resolve_app_root()
DATA_ROOT = APP_ROOT


def bind_data_root(root):
    """Startup only, before importing configuration, stores or plugins."""
    global DATA_ROOT
    DATA_ROOT = Path(root).resolve()
    return DATA_ROOT
