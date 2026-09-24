"""Small, independent Drive upload for ad-hoc task files."""

import os
from pathlib import Path

from qt_compat import QtCore, QtWidgets

from model.ClipboardHelper import set_internal_clipboard_text
from model.GoogleDriveHelper import (
    drive_folder_link,
    folder_name as drive_date_folder_name,
    get_or_create_remote_folder,
    get_or_create_remote_folder_path,
    load_drive_service,
    read_drive_parent_folder_id,
    sync_file,
)
from model.TaskResultOrganizer import get_upload_batch


def validate_folder_name(value):
    name = str(value or "").strip()
    if not name or name in {".", ".."} or any(
        char in name for char in "/\\\r\n\0"
    ):
        raise ValueError("请填写单层文件夹名称，不能包含斜杠或换行。")
    return name


def collect_upload_files(sources):
    """Plan everything before remote writes; reject target-name collisions."""
    paths = []
    seen_sources = set()
    for raw in sources or []:
        path = Path(raw).resolve()
        key = os.path.normcase(str(path))
        if key in seen_sources:
            continue
        if not path.is_file() and not path.is_dir():
            raise ValueError(f"文件或文件夹不存在：{path}")
        seen_sources.add(key)
        paths.append(path)
    if not paths:
        raise ValueError("请先添加要上传的文件或文件夹。")
    paths = [
        path for path in paths
        if not any(parent != path and parent.is_dir() and parent in path.parents
                   for parent in paths)
    ]
    plan = []
    target_names = set()
    for source in paths:
        if source.is_file():
            entries = [(source, Path(source.name))]
        else:
            entries = []
            for root, dirs, files in os.walk(source, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
                for name in sorted(files):
                    local = Path(root) / name
                    if local.is_file():
                        entries.append((local, Path(source.name) / local.relative_to(source)))
        for local, relative in entries:
            key = str(relative).replace("\\", "/").casefold()
            if key in target_names:
                raise ValueError(f"上传目标存在同名文件，请调整来源：{relative}")
            target_names.add(key)
            plan.append((local, relative))
    if not plan:
        raise ValueError("选择的文件夹中没有可上传的文件。")
    return plan


def upload_quick_files(sources, name, parent_folder, progress=None):
    name = validate_folder_name(name)
    parent_id = read_drive_parent_folder_id(str(parent_folder or "").strip())
    plan = collect_upload_files(sources)
    batch_date, batch_slot = get_upload_batch({})  # Never use a manual batch override.
    service = load_drive_service()
    date_id = get_or_create_remote_folder(
        service, parent_id, drive_date_folder_name(batch_date)
    )
    slot_id = get_or_create_remote_folder(service, date_id, batch_slot)
    target_id = get_or_create_remote_folder(service, slot_id, name)
    uploaded = skipped = 0
    failures = []
    folder_ids = {".": target_id}
    for index, (local, relative) in enumerate(plan, 1):
        if progress:
            progress(index - 1, len(plan), str(relative))
        relative_dir = relative.parent
        directory_key = str(relative_dir)
        if directory_key not in folder_ids:
            folder_ids[directory_key] = get_or_create_remote_folder_path(
                service, target_id, relative_dir
            )
        try:
            result = sync_file(service, local, folder_ids[directory_key])
            if result is None:
                failures.append(f"{relative}：云端同名文件无法覆盖")
            elif str(result.get("action") or "").startswith("skipped"):
                skipped += 1
            else:
                uploaded += 1
        except Exception as error:
            failures.append(f"{relative}：{type(error).__name__}: {error}")
        if progress:
            progress(index, len(plan), str(relative))
    return {
        "batch": f"{batch_date:%m%d}/{batch_slot}",
        "folder_name": name,
        "folder_link": drive_folder_link(target_id),
        "uploaded": uploaded,
        "skipped": skipped,
        "failures": failures,
        "total": len(plan),
    }


class QuickUploadThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int, str)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, sources, name, parent_folder, parent=None):
        super().__init__(parent)
        self.sources = list(sources)
        self.name = name
        self.parent_folder = parent_folder

    def run(self):
        try:
            result = upload_quick_files(
                self.sources, self.name, self.parent_folder,
                progress=self.progress.emit,
            )
        except (Exception, SystemExit) as error:
            self.failed.emit(f"{type(error).__name__}: {error}")
        else:
            self.completed.emit(result)


class _SourceList(QtWidgets.QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)

    def add_paths(self, paths):
        existing = {self.item(i).text().casefold() for i in range(self.count())}
        for raw in paths:
            path = str(Path(raw).resolve())
            if path.casefold() not in existing:
                existing.add(path.casefold())
                self.addItem(path)

    def paths(self):
        return [self.item(i).text() for i in range(self.count())]

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        self.add_paths(url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile())
        event.acceptProposedAction()


class QuickUploadDialog(QtWidgets.QDialog):
    upload_requested = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("简易上传到 Google Drive")
        self.resize(650, 430)
        self.setModal(False)
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "零散文件直接上传，不填写任务表或审核表。可拖入多个文件／文件夹；"
            "云端同名文件会按现有规则跳过或更新。",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.name_edit = QtWidgets.QLineEdit(self)
        self.name_edit.setPlaceholderText("云端文件夹名称，例如：临时补交")
        name_row = QtWidgets.QFormLayout()
        name_row.addRow("文件夹名称：", self.name_edit)
        layout.addLayout(name_row)
        self.batch_label = QtWidgets.QLabel(self)
        self.batch_label.setWordWrap(True)
        layout.addWidget(self.batch_label)
        layout.addWidget(QtWidgets.QLabel("待上传文件（可拖入）：", self))
        self.sources = _SourceList(self)
        layout.addWidget(self.sources, 1)
        source_buttons = QtWidgets.QHBoxLayout()
        add_files = QtWidgets.QPushButton("添加文件", self)
        add_folder = QtWidgets.QPushButton("添加文件夹", self)
        remove = QtWidgets.QPushButton("移除选中", self)
        for button in (add_files, add_folder, remove):
            source_buttons.addWidget(button)
        source_buttons.addStretch()
        layout.addLayout(source_buttons)
        self.progress_bar = QtWidgets.QProgressBar(self)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.status_label = QtWidgets.QLabel("等待上传", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        link_row = QtWidgets.QHBoxLayout()
        self.link_edit = QtWidgets.QLineEdit(self)
        self.link_edit.setReadOnly(True)
        self.link_edit.setPlaceholderText("上传后在这里显示文件夹链接")
        self.copy_button = QtWidgets.QPushButton("复制链接", self)
        self.copy_button.setEnabled(False)
        link_row.addWidget(self.link_edit, 1)
        link_row.addWidget(self.copy_button)
        layout.addLayout(link_row)
        self.upload_button = QtWidgets.QPushButton("开始上传", self)
        layout.addWidget(self.upload_button)
        add_files.clicked.connect(self._choose_files)
        add_folder.clicked.connect(self._choose_folder)
        remove.clicked.connect(self._remove_selected)
        self.name_edit.textChanged.connect(lambda _text: self.refresh_batch())
        self.upload_button.clicked.connect(self._request_upload)
        self.copy_button.clicked.connect(
            lambda: set_internal_clipboard_text(self.link_edit.text())
        )
        self._editing_controls = (self.name_edit, self.sources, add_files, add_folder, remove)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self.refresh_batch)
        self.timer.start()
        self.refresh_batch()

    def refresh_batch(self):
        batch_date, slot = get_upload_batch({})
        name = self.name_edit.text().strip() or "〈填写的文件夹名〉"
        self.batch_label.setText(
            f"本次自动归档：{batch_date:%m%d}/{slot}/{name}"
            "（01 截至 12:00，02 截至 18:00，03 截至次日 07:00）"
        )

    def _choose_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "选择上传文件")
        self.sources.add_paths(paths)

    def _choose_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择上传文件夹")
        if path:
            self.sources.add_paths([path])

    def _remove_selected(self):
        for item in self.sources.selectedItems():
            self.sources.takeItem(self.sources.row(item))

    def _request_upload(self):
        try:
            name = validate_folder_name(self.name_edit.text())
            if not self.sources.paths():
                raise ValueError("请先添加要上传的文件或文件夹。")
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self, "无法上传", str(error))
            return
        self.upload_requested.emit(name, self.sources.paths())

    def set_busy(self, busy):
        for control in self._editing_controls:
            control.setEnabled(not busy)
        self.upload_button.setEnabled(not busy)
        self.upload_button.setText("正在上传…" if busy else "开始上传")

    def update_progress(self, done, total, label):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        self.status_label.setText(f"已处理 {done}/{total}：{label}")

    def show_result(self, result):
        self.set_busy(False)
        self.link_edit.setText(result["folder_link"])
        self.copy_button.setEnabled(True)
        failed = len(result["failures"])
        self.status_label.setText(
            f"{result['batch']}/{result['folder_name']}："
            f"上传 {result['uploaded']}，未变化 {result['skipped']}，失败 {failed}。"
        )
        if failed:
            QtWidgets.QMessageBox.warning(
                self, "部分文件上传失败", "\n".join(result["failures"][:20])
            )
