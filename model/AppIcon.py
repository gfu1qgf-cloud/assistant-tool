"""One icon for source runs, frozen windows and desktop notifications."""
from pathlib import Path
import sys

from qt_compat import QtGui


def application_icon(app_root, bundle_root=None):
    roots = [Path(app_root)]
    if bundle_root is None:
        bundle_root = getattr(sys, '_MEIPASS', None)
    if bundle_root:
        roots.append(Path(bundle_root))
    for root in roots:
        for name in ('app_icon.ico', 'app_icon.png'):
            path = root / name
            if path.is_file():
                icon = QtGui.QIcon(str(path))
                if not icon.isNull():
                    return icon
    return QtGui.QIcon()
