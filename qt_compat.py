"""Central PyQt6 compatibility surface for the application.

The project is migrated to the PyQt6 runtime through this module.  A small
set of Qt5-style enum aliases is installed here so feature modules do not
need to carry version checks and the aliases can be removed incrementally.
"""

from PyQt6 import QtCore, QtGui, QtNetwork, QtTest, QtWidgets

try:
    from PyQt6 import QtMultimedia, QtMultimediaWidgets
except ImportError:  # pragma: no cover - optional on stripped installations
    QtMultimedia = None
    QtMultimediaWidgets = None


def _alias(owner, old_name, enum_owner, new_name=None):
    if hasattr(owner, old_name):
        return
    value = getattr(enum_owner, new_name or old_name, None)
    if value is not None:
        setattr(owner, old_name, value)


_QT_ENUMS = {
    "DisplayRole": QtCore.Qt.ItemDataRole,
    "UserRole": QtCore.Qt.ItemDataRole,
    "Checked": QtCore.Qt.CheckState,
    "Unchecked": QtCore.Qt.CheckState,
    "PartiallyChecked": QtCore.Qt.CheckState,
    "AlignCenter": QtCore.Qt.AlignmentFlag,
    "AlignRight": QtCore.Qt.AlignmentFlag,
    "AlignLeft": QtCore.Qt.AlignmentFlag,
    "AlignVCenter": QtCore.Qt.AlignmentFlag,
    "ItemIsUserCheckable": QtCore.Qt.ItemFlag,
    "ItemIsEditable": QtCore.Qt.ItemFlag,
    "ItemIsSelectable": QtCore.Qt.ItemFlag,
    "ItemIsEnabled": QtCore.Qt.ItemFlag,
    "CustomContextMenu": QtCore.Qt.ContextMenuPolicy,
    "NonModal": QtCore.Qt.WindowModality,
    "KeepAspectRatio": QtCore.Qt.AspectRatioMode,
    "Horizontal": QtCore.Qt.Orientation,
    "Vertical": QtCore.Qt.Orientation,
    "LeftButton": QtCore.Qt.MouseButton,
    "RightButton": QtCore.Qt.MouseButton,
    "PointingHandCursor": QtCore.Qt.CursorShape,
    "ClosedHandCursor": QtCore.Qt.CursorShape,
    "WaitCursor": QtCore.Qt.CursorShape,
    "SmoothTransformation": QtCore.Qt.TransformationMode,
    "NoBrush": QtCore.Qt.BrushStyle,
    "ElideRight": QtCore.Qt.TextElideMode,
    "ElideMiddle": QtCore.Qt.TextElideMode,
    "TextSelectableByMouse": QtCore.Qt.TextInteractionFlag,
    "WidgetWithChildrenShortcut": QtCore.Qt.ShortcutContext,
    "ShiftModifier": QtCore.Qt.KeyboardModifier,
    "ControlModifier": QtCore.Qt.KeyboardModifier,
    "AltModifier": QtCore.Qt.KeyboardModifier,
    "MetaModifier": QtCore.Qt.KeyboardModifier,
    "ScrollBarAlwaysOff": QtCore.Qt.ScrollBarPolicy,
    "ScrollBarAsNeeded": QtCore.Qt.ScrollBarPolicy,
    "WA_DeleteOnClose": QtCore.Qt.WidgetAttribute,
    "WA_NativeWindow": QtCore.Qt.WidgetAttribute,
    "WindowContextHelpButtonHint": QtCore.Qt.WindowType,
    "WindowMaximizeButtonHint": QtCore.Qt.WindowType,
    "WindowMinimizeButtonHint": QtCore.Qt.WindowType,
    "ToolButtonTextBesideIcon": QtCore.Qt.ToolButtonStyle,
    "RightArrow": QtCore.Qt.ArrowType,
    "DownArrow": QtCore.Qt.ArrowType,
}
for _name, _enum in _QT_ENUMS.items():
    _alias(QtCore.Qt, _name, _enum)
for _name in (
    "Key_Backspace", "Key_Delete", "Key_Down", "Key_End", "Key_Enter",
    "Key_Escape", "Key_F", "Key_F1", "Key_F24", "Key_Home", "Key_Insert", "Key_Left",
    "Key_PageDown", "Key_PageUp", "Key_Return", "Key_Right", "Key_Space",
    "Key_Tab", "Key_Up",
):
    _alias(QtCore.Qt, _name, QtCore.Qt.Key)

for _name in ("MouseButtonDblClick", "MouseButtonPress", "Resize", "Wheel"):
    _alias(QtCore.QEvent, _name, QtCore.QEvent.Type)
_alias(QtCore.QProcess, "NotRunning", QtCore.QProcess.ProcessState)
_alias(QtCore.QIODevice, "ReadWrite", QtCore.QIODevice.OpenModeFlag)
_alias(QtCore.QEventLoop, "ExcludeUserInputEvents", QtCore.QEventLoop.ProcessEventsFlag)
for _name in ("AllEntries", "NoDotAndDotDot", "AllDirs"):
    _alias(QtCore.QDir, _name, QtCore.QDir.Filter)
_alias(QtNetwork.QLocalSocket, "UnconnectedState", QtNetwork.QLocalSocket.LocalSocketState)

for _name in ("Accepted", "Rejected"):
    _alias(QtWidgets.QDialog, _name, QtWidgets.QDialog.DialogCode)
for _name in ("Ok", "Cancel", "Save", "Close"):
    _alias(QtWidgets.QDialogButtonBox, _name, QtWidgets.QDialogButtonBox.StandardButton)
_alias(QtWidgets.QDialogButtonBox, "AcceptRole", QtWidgets.QDialogButtonBox.ButtonRole)
for _name in ("Yes", "No", "Ok", "Cancel", "Close"):
    _alias(QtWidgets.QMessageBox, _name, QtWidgets.QMessageBox.StandardButton)
for _name in ("Stretch", "ResizeToContents", "Interactive"):
    _alias(QtWidgets.QHeaderView, _name, QtWidgets.QHeaderView.ResizeMode)
for _name in ("Expanding", "Minimum", "Preferred", "Ignored"):
    _alias(QtWidgets.QSizePolicy, _name, QtWidgets.QSizePolicy.Policy)
for _name in ("SelectRows",):
    _alias(QtWidgets.QAbstractItemView, _name, QtWidgets.QAbstractItemView.SelectionBehavior)
for _name in ("SingleSelection", "ExtendedSelection", "NoSelection"):
    _alias(QtWidgets.QAbstractItemView, _name, QtWidgets.QAbstractItemView.SelectionMode)
_alias(QtWidgets.QAbstractItemView, "NoEditTriggers", QtWidgets.QAbstractItemView.EditTrigger)
_alias(QtWidgets.QAbstractItemView, "DropOnly", QtWidgets.QAbstractItemView.DragDropMode)
_alias(QtWidgets.QAbstractItemView, "PositionAtCenter", QtWidgets.QAbstractItemView.ScrollHint)
_alias(QtWidgets.QAbstractItemView, "ScrollPerItem", QtWidgets.QAbstractItemView.ScrollMode)
_alias(QtWidgets.QFormLayout, "AllNonFixedFieldsGrow", QtWidgets.QFormLayout.FieldGrowthPolicy)
_alias(QtWidgets.QFrame, "StyledPanel", QtWidgets.QFrame.Shape)
for _name in ("Normal", "Password"):
    _alias(QtWidgets.QLineEdit, _name, QtWidgets.QLineEdit.EchoMode)
for _name in ("Information", "Critical"):
    _alias(QtWidgets.QSystemTrayIcon, _name, QtWidgets.QSystemTrayIcon.MessageIcon)
for _name in ("SP_MessageBoxInformation", "SP_DirHomeIcon"):
    _alias(QtWidgets.QStyle, _name, QtWidgets.QStyle.StandardPixmap)
_alias(
    QtWidgets.QComboBox,
    "AdjustToMinimumContentsLengthWithIcon",
    QtWidgets.QComboBox.SizeAdjustPolicy,
)

for _name in ("PortableText",):
    _alias(QtGui.QKeySequence, _name, QtGui.QKeySequence.SequenceFormat)
_alias(QtGui.QKeySequence, "Copy", QtGui.QKeySequence.StandardKey)
_alias(QtGui.QImage, "Format_RGB888", QtGui.QImage.Format)
_alias(QtGui.QPalette, "Window", QtGui.QPalette.ColorRole)
_alias(QtGui.QTextCursor, "End", QtGui.QTextCursor.MoveOperation)

# Classes moved from QtWidgets to QtGui in Qt6.
QtWidgets.QAction = QtGui.QAction
QtWidgets.QShortcut = QtGui.QShortcut
QtWidgets.QFileSystemModel = QtGui.QFileSystemModel

# Frequently imported names kept in one place for concise feature imports.
QApplication = QtWidgets.QApplication
QDate = QtCore.QDate
QDir = QtCore.QDir
QFileSystemModel = QtGui.QFileSystemModel
QHeaderView = QtWidgets.QHeaderView
QInputDialog = QtWidgets.QInputDialog
QLabel = QtWidgets.QLabel
QLineEdit = QtWidgets.QLineEdit
QListWidgetItem = QtWidgets.QListWidgetItem
QMenu = QtWidgets.QMenu
QMessageBox = QtWidgets.QMessageBox
QThread = QtCore.QThread
QTreeView = QtWidgets.QTreeView
QVBoxLayout = QtWidgets.QVBoxLayout
QWidget = QtWidgets.QWidget
Qt = QtCore.Qt
pyqtSignal = QtCore.pyqtSignal
