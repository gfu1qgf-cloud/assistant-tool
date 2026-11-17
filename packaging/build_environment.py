"""Keep unrelated desktop tools' native DLLs out of Windows release builds."""
import os
from pathlib import Path
import sys


def windows_build_path(executable, base_prefix, system_root):
    """Use the selected interpreter and Windows, never the caller's PATH."""
    python = Path(executable)
    base = Path(base_prefix)
    windows = Path(system_root)
    if not all(path.is_absolute() for path in (python, base, windows)):
        raise ValueError('Release build requires absolute Python and Windows paths')
    directories = (python.parent, base, base/'DLLs', windows/'System32', windows,
                   windows/'System32'/'Wbem')
    return os.pathsep.join(dict.fromkeys(str(path) for path in directories))


def isolate_windows_build_environment(environment=None):
    target = os.environ if environment is None else environment
    target['PATH'] = windows_build_path(sys.executable, sys.base_prefix, target['SystemRoot'])
    # Inherited Qt overrides can point to an unrelated application's plugins.
    for key in ('QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'PYTHONPATH'):
        target.pop(key, None)
