"""Startup chooser runs before private stores, log files and plugins exist."""
from pathlib import Path
import queue
import shutil
import threading

from qt_compat import QtCore, QtWidgets
from model.ProtectedStorage import (migrate, bind, read_bootstrap, read_marker,
                                    commit_bootstrap, StorageUnavailable, StorageCapacityError, WARN_FREE)
from model.VeraCryptVolumes import VeraCryptVolumes, VolumeError
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active


class MigrationWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(str, int)
    outcome = QtCore.pyqtSignal(object)
    def __init__(self, program_root, provider, volume, parent):
        super().__init__(parent)
        self.program_root, self.provider, self.volume = program_root, provider, volume
        self.canceled = threading.Event()
    def run(self):
        def report(message, percent):
            if self.canceled.is_set(): raise StorageUnavailable('迁移已取消，原数据未改变；未完成副本保留在加密卷内。')
            self.progress.emit(message, percent)
        try: result = migrate(self.program_root, self.provider, self.volume, report)
        except Exception as error: self.outcome.emit({'error': str(error)})
        else: self.outcome.emit({'result': result})


class StorageChooser(QtWidgets.QDialog):
    def __init__(self, program_root, expected=None, provider=None, parent=None):
        super().__init__(parent)
        self.program_root, self.expected = Path(program_root), expected
        self.provider = provider
        self.worker, self.selection, self.pending = None, None, None
        self.setWindowTitle('VeraCrypt · 选择加密数据卷')
        self.resize(640, 360)
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel('请先在 VeraCrypt 中手动挂载卷。本程序不接收密码，也不会自动挂载或卸载。\n'
            '仅列出驱动确认的加密卷。启用后，卷不可用时不会回退明文配置。', self)
        hint.setWordWrap(True); layout.addWidget(hint)
        self.volumes = QtWidgets.QComboBox(self); layout.addWidget(self.volumes)
        self.refresh_button = QtWidgets.QPushButton('刷新已挂载的 VC 卷', self); layout.addWidget(self.refresh_button)
        self.status = QtWidgets.QLabel(self); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.progress = QtWidgets.QProgressBar(self); self.progress.setRange(0, 100); layout.addWidget(self.progress)
        self.closed_confirm = QtWidgets.QCheckBox('我已关闭其他辅助工具实例；迁移后旧明文文件仍保留，不会自动删除。', self)
        self.closed_confirm.setVisible(expected is None); layout.addWidget(self.closed_confirm)
        buttons = QtWidgets.QDialogButtonBox(self)
        self.use_button = buttons.addButton('使用所选加密数据', QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.migrate_button = buttons.addButton('迁移现有数据到所选卷', QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.migrate_button.setVisible(expected is None)
        self.cancel_button = buttons.addButton('退出 / 取消迁移', QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self.refresh_button.clicked.connect(self.refresh)
        self.use_button.clicked.connect(self.use_existing)
        self.migrate_button.clicked.connect(self.start_migration)
        self.cancel_button.clicked.connect(self.reject)
        self.volumes.currentIndexChanged.connect(self.selected_changed)
        self.refresh()

    def refresh(self):
        self.volumes.clear()
        try:
            self.provider = self.provider or VeraCryptVolumes()
            choices = self.provider.mounted()
            for volume in choices:
                if volume.readonly: continue
                space = shutil.disk_usage(volume.root).free / 1024**3
                found = (Path(volume.root)/'AssistantToolData').exists()
                self.volumes.addItem(f'{volume.root} · 可用 {space:.1f} GB · ' + ('已有数据目录' if found else '尚未迁移'), volume)
            self.status.setText('请选择加密卷。' if self.volumes.count() else '未找到可写的已挂载 VC 卷。先打开 VeraCrypt 挂载，再点击刷新。')
        except (VolumeError, OSError) as error: self.status.setText(str(error))
        self.selected_changed()

    def selected_changed(self):
        ready = self.volumes.currentData() is not None and self.worker is None
        self.use_button.setEnabled(ready); self.migrate_button.setEnabled(ready)

    def use_existing(self):
        volume = self.volumes.currentData()
        if not volume: return
        root = Path(volume.root)/'AssistantToolData'
        try:
            volume = self.provider.verify(volume.root, identity=self.expected['volume_id'] if self.expected else None)
            marker = read_marker(root)
            if self.expected and any(marker[key] != self.expected[key] for key in ('storage_id','volume_id')):
                raise StorageUnavailable('这不是原先绑定的数据卷；未加载空配置。')
            if marker['volume_id'] != volume.volume_id: raise StorageUnavailable('数据标记与卷身份不同。')
        except (VolumeError, OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, '不能使用该数据卷', str(error)); return
        self.selection = (self.provider, volume, root, marker)
        self.accept()

    def start_migration(self):
        volume = self.volumes.currentData()
        if not volume or self.worker: return
        if not self.closed_confirm.isChecked():
            QtWidgets.QMessageBox.information(self, '先关闭旧实例', '请先关闭其他辅助工具，避免迁移期间旧进程继续写数据。'); return
        if QtWidgets.QMessageBox.question(self, '确认复制并启用 VC 强制模式',
            '复制配置、凭据、插件数据、索引和日志到已验证的 VC 卷，并逐项校验。\n'
            '任务目录、素材视频和模型权重不搬迁。原文件和旧备份不删除。\n'
            '以后启动必须选择绑定的已挂载卷；没有卷不能启动。\n\n是否开始？',
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No) != QtWidgets.QMessageBox.StandardButton.Yes: return
        self.worker = MigrationWorker(self.program_root, self.provider, volume, self)
        self.pending = None
        self.worker.progress.connect(self.migration_progress,QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.outcome.connect(self.migration_outcome,QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.worker,self,'worker',self.migration_finished,pass_thread=True)
        self.refresh_button.setEnabled(False); self.volumes.setEnabled(False); self.selected_changed()
        self.worker.start()

    @QtCore.pyqtSlot(str,int)
    def migration_progress(self,text,percent):self.status.setText(text);self.progress.setValue(percent)
    @QtCore.pyqtSlot(object)
    def migration_outcome(self, value): self.pending = value

    def migration_finished(self,worker):
        volume = worker.volume
        self.worker = None
        self.refresh_button.setEnabled(True); self.volumes.setEnabled(True); self.selected_changed()
        value, self.pending = self.pending, None
        if worker.canceled.is_set():self.reject();return
        if not value or 'error' in value:
            QtWidgets.QMessageBox.warning(self, '未启用加密位置', (value or {}).get('error', '迁移未完成；原文件未改变。')); return
        root, marker = value['result']
        self.selection = (self.provider, volume, root, marker)
        self.accept()

    def reject(self):
        if thread_is_active(self.worker):
            self.worker.canceled.set(); self.status.setText('正在安全取消，原数据不变。请等待当前文件校验结束。'); return
        super().reject()

    def closeEvent(self, event):
        if thread_is_active(self.worker): self.reject(); event.ignore()
        else: super().closeEvent(event)


def configure_startup(program_root, *, force=False):
    expected = read_bootstrap(program_root)
    if expected is None and not force: return None
    dialog = StorageChooser(program_root, expected)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted: raise StorageUnavailable('未选择可用的加密数据卷，程序未启动。')
    provider, volume, root, marker = dialog.selection
    storage = bind(program_root, provider, volume, root, expected)
    if expected is None:
        try: commit_bootstrap(program_root, marker)
        except OSError:
            storage.close()
            raise StorageUnavailable('无法保存强制模式启动绑定。卷内副本已保留，未删除任何原文件；本次不启动。') from None
    return storage


def _probe_storage_health(storage, results):
    """Read-only daemon job; owns no QWidget or QObject references."""
    try:
        results.put(('healthy',storage.guard.health()))
    except StorageCapacityError as error:
        results.put(('space',str(error)))
    except Exception as error:
        results.put(('lost',str(error)))


class StorageMonitor(QtCore.QObject):
    def __init__(self, window, storage):
        super().__init__(window)
        self.window, self.storage, self.reported = window, storage, False
        self.space_warning = self.space_paused = False
        self.notice = None
        self.worker = None
        self.results = queue.SimpleQueue()
        self.timer = QtCore.QTimer(self); self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll); self.timer.start()
    def poll(self):
        # Never perform drive/USB/free-space queries in a Qt timer callback.
        # At most one read-only probe exists, even if a drive takes a long time.
        try: kind,value=self.results.get_nowait()
        except queue.Empty:
            if self.worker is not None and self.worker.is_alive():return
            if self.reported:return
            self.worker=threading.Thread(target=_probe_storage_health,
                args=(self.storage,self.results),name='vc-storage-health',daemon=True)
            self.worker.start();self.timer.setInterval(100)
            return
        self.worker=None
        self.timer.setInterval(2000)
        self.apply_health(kind,value)

    def apply_health(self,kind,value):
        if kind == 'healthy':
            free=value
            if free < WARN_FREE and not self.space_warning:
                self.space_warning = True
                self.show_space_notice('加密卷空间预警', f'加密卷只剩 {free/1024**2:.0f} MB。请清理可再生成的缓存或扩容；低于256 MB会停止新增写入。')
            if free >= WARN_FREE: self.space_warning = False
            self.space_paused = False
        elif kind == 'space':
            if self.space_paused: return
            self.space_paused = True
            self.window.background_tasks.cancel_all()
            self.show_space_notice('加密卷空间不足，任务已暂停', value+'\n\n已取消后台任务以停止继续增长；清理后可重新提交。原数据与索引保留。')
        else:
            if self.reported: return
            self.reported = True; self.timer.stop()
            self.window.background_tasks.stop_accepting(); self.window.background_tasks.cancel_all()
            if hasattr(self.window, 'config_reload'): self.window.config_reload.timer.stop()
            from model.AppLogger import shutdown_application_logging
            shutdown_application_logging(close=True)
            QtWidgets.QMessageBox.critical(self.window, '加密卷不可用', (self.storage.guard.failure or value) +
                '\n\n请等待任务安全停止后退出，再挂载正确卷重新启动。未卸载其他卷。')

    def show_space_notice(self, title, text):
        # Non-modal so worker completion/cleanup can continue normally.
        if self.notice is not None: self.notice.close()
        self.notice = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Warning, title, text,
            QtWidgets.QMessageBox.StandardButton.Ok, self.window)
        self.notice.setModal(False)
        self.notice.show()
