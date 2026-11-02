"""Video assignment UI and worker ownership, separate from the main window."""
import os
from qt_compat import QtCore, QtWidgets, QMessageBox
from app_plugins.api import MAIN_MENU, PluginCommand
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
from globalValue import globalValue
from .worker import VideoAssignmentThread


class VideoSorterPlugin(QtCore.QObject):
    plugin_id = 'video_sorter'
    display_name = '分拣视频'
    version = '1.0'
    required_api_version = 2

    def __init__(self):
        super().__init__()
        self.context = self.dialog = None
        self.assign_video_thread = None
        self._assign_video_button_text = '分拣视频'
        self._last_video_assign_result = None

    def register(self, context):
        self.context = context
        self.setParent(context.parent_widget)
        context.register_command(PluginCommand('open', '分拣视频…', lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), order=43))

    def open_dialog(self):
        if self.dialog is None:
            from .ui import VideoSorterDialog
            self.dialog = VideoSorterDialog(self, self.context.parent_widget)
        self.dialog.show(); self.dialog.raise_(); self.dialog.activateWindow()
        return self.dialog

    @property
    def assign_video_btn(self):
        if self.dialog is None:
            from .ui import VideoSorterDialog
            self.dialog = VideoSorterDialog(self, self.context.parent_widget)
        return self.dialog.run_button

    def getTodayDir(self):
        if self.dialog is not None: return self.dialog.target.text().strip() or None
        return self.context.current_project_directory(require_loaded=False)

    def source_directory(self):
        if self.dialog is not None: return self.dialog.source.text().strip()
        return str(self.context.load_config().get('video_sorting_station_path') or '').strip()

    def load_config(self):
        config = self.context.load_config()
        if self.dialog is not None: config['video_match_ratio_threshold'] = self.dialog.threshold.value()
        return config

    def Critical(self, message): QMessageBox.critical(self.context.parent_widget, '分拣视频', message)

    def appendLog(self, message):
        self.context.log(message)
        if self.dialog is not None: self.dialog.log.appendPlainText(message)

    def can_close(self):
        if thread_is_active(self.assign_video_thread):
            self.assign_video_thread.requestInterruption()
            if not self.assign_video_thread.wait(2000):
                return False, '已请求停止视频分拣，请等待当前文件处理结束后再退出。'
        return True, ''

    def stop(self):
        thread = self.assign_video_thread
        if thread_is_active(thread):
            thread.requestInterruption()
            if not thread.wait(2000): return False
        if self.dialog is not None: self.dialog.close()
        return True

    def assignVideo(self, *, worker_factory=None):
        if self.assign_video_thread is not None and thread_is_active(self.assign_video_thread):
            self.assign_video_thread.requestInterruption()
            self.assign_video_btn.setEnabled(False)
            self.assign_video_btn.setText("正在停止…")
            self.appendLog("已请求停止视频分拣，等待当前文件处理完成...")
            return self.assign_video_thread

        today_dir = self.getTodayDir()
        source = getattr(type(self), 'source_directory', None)
        VIDEO_ROOT_DIR = source(self) if callable(source) else globalValue.videoSortingStationPath()

        if today_dir is None:
            return None
        if not str(VIDEO_ROOT_DIR or "").strip():
            self.Critical("没有配置视频来源目录。")
            return None

        try:
            MATCH_RATIO_THRESHOLD = float(
                self.load_config().get("video_match_ratio_threshold", 0.03)
            )
        except (TypeError, ValueError):
            MATCH_RATIO_THRESHOLD = 0.03
        MATCH_RATIO_THRESHOLD = min(1.0, max(0.0, MATCH_RATIO_THRESHOLD))

        if not os.path.exists(VIDEO_ROOT_DIR) or not os.path.exists(today_dir):
            self.Critical("视频来源目录或当前任务目录配置错误。")
            return None

        if not self._assign_video_button_text:
            self._assign_video_button_text = self.assign_video_btn.text() or "分拣视频"
        self.assign_video_btn.setText("停止分拣")
        self.assign_video_btn.setEnabled(True)

        thread = (worker_factory or VideoAssignmentThread)(
            today_dir=today_dir,
            video_root_dir=VIDEO_ROOT_DIR,
            match_ratio_threshold=MATCH_RATIO_THRESHOLD,
            parent=self,
        )
        thread.log.connect(self.appendLog)
        thread.completed.connect(self.onAssignVideoCompleted)
        thread.failed.connect(self.onAssignVideoFailed)
        connect_thread_finished(thread, self, 'assign_video_thread', self.onAssignVideoFinished)

        self.assign_video_thread = thread
        if getattr(self, 'dialog', None) is not None: self.dialog.busy(True)
        thread.start()
        return thread

    def onAssignVideoCompleted(self, matched_count, total_count):
        self._last_video_assign_result = (matched_count, total_count)

    def onAssignVideoFailed(self, message, traceback_text):
        self.appendLog(f"分拣视频发生异常：\n{traceback_text}")
        QMessageBox.critical(getattr(getattr(self, 'context', None), 'parent_widget', self), "分拣视频失败", f"分拣视频过程中出现错误：\n{message}")

    def onAssignVideoFinished(self, matched_count=None, total_count=None, *args, **kwargs):
        thread = self.assign_video_thread
        self.assign_video_thread = None
        if getattr(self, 'dialog', None) is not None: self.dialog.busy(False)
        if hasattr(self, "assign_video_btn") and self.assign_video_btn is not None:
            self.assign_video_btn.setEnabled(True)
            self.assign_video_btn.setText(self._assign_video_button_text or "分拣视频")
        if (
            matched_count is not None
            and total_count is not None
            and not isinstance(matched_count, QtCore.QObject)
        ):
            self._last_video_assign_result = (matched_count, total_count)
        if thread is not None:
            delete_later = getattr(thread, "deleteLater", None)
            if callable(delete_later):
                delete_later()
