from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand

from .batch import unique_jobs
from .task_cache import cache_directory, cached_sheets, extract_facebook_references


class FacebookContactSheetPlugin:
    plugin_id = "facebook_contact_sheet"
    display_name = "Facebook 视频分镜速览"
    version = "1.1"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.dialog = None
        self.batch_dialog = None

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand(
            command_id="open",
            title="Facebook 视频分镜速览…",
            callback=lambda _rows: self.open_dialog(),
            locations=frozenset({MAIN_MENU}),
            tooltip="粘贴 Facebook 链接或选择本地视频，自动检测换镜头并导出分镜大图",
            order=18,
        ))
        context.register_command(PluginCommand(
            command_id="generate_task_references",
            title="生成 Facebook 参考大图…",
            callback=self.generate_task_references,
            locations=frozenset({TASK_CONTEXT_MENU}),
            tooltip="批量生成所选任务的全部 Facebook 参考视频大图；已有缓存自动跳过",
            order=34,
        ))
        context.register_command(PluginCommand(
            command_id="view_task_reference",
            title="查看 Facebook 参考大图…",
            callback=self.view_task_reference,
            locations=frozenset({TASK_CONTEXT_MENU}),
            tooltip="优先打开任务目录中的参考大图；没有缓存时自动在后台生成并打开",
            order=35,
        ))

    def start(self):
        return None

    def open_dialog(self):
        dialog = self._show_dialog()
        if not dialog.is_busy():
            dialog.clear_task_reference()
        return dialog

    def _show_dialog(self):
        if self.dialog is None:
            from .ui import ContactSheetDialog
            self.dialog = ContactSheetDialog(logger=self.context.log, parent=self.context.parent_widget)
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def view_task_reference(self, rows):
        from qt_compat import QtCore, QtGui, QtWidgets

        targets = self.context.task_targets(rows)
        if not targets:
            return None
        references = [
            (target, url)
            for target in targets
            for url in extract_facebook_references(
                getattr(target["task"], "task_reference_link", "")
            )
        ]
        if not references:
            QtWidgets.QMessageBox.information(
                self.context.parent_widget,
                "没有 Facebook 参考",
                "所选任务的参考链接中没有可识别的 Facebook 视频地址。",
            )
            return None
        choices = [
            (target, url, cached_sheets(cache_directory(target["target_dir"], url), url))
            for target, url in references
        ]
        selected_index = 0
        if len(choices) > 1:
            labels = [
                f"{index + 1}. {target['label']} · {'已缓存' if sheets else '自动生成'} · {url[:90]}"
                for index, (target, url, sheets) in enumerate(choices)
            ]
            label, accepted = QtWidgets.QInputDialog.getItem(
                self.context.parent_widget,
                "选择参考视频",
                "该任务选择中有多个 Facebook 链接，请选择要查看的一个：",
                labels, 0, False,
            )
            if not accepted:
                return None
            selected_index = labels.index(label)
        target, url, sheets = choices[selected_index]
        if not sheets:
            # Reuse the existing worker and task-local cache instead of downloading on the GUI thread.
            if self.batch_dialog is not None and self.batch_dialog.is_busy():
                self.batch_dialog.show()
                self.batch_dialog.raise_()
                self.batch_dialog.activateWindow()
                QtWidgets.QMessageBox.information(
                    self.batch_dialog, "正在处理",
                    "参考大图批量生成正在进行；请等待完成后再查看，避免重复下载。",
                )
                return None
            dialog = self._show_dialog()
            if dialog.is_busy():
                QtWidgets.QMessageBox.information(
                    dialog, "正在处理", "参考大图正在生成；完成或取消后再查看其他参考。",
                )
                return None
            if dialog.start_task_reference(url, cache_directory(target["target_dir"], url)):
                self.context.log(f"任务 {target['label']}：未找到参考大图，正在后台自动生成；完成后自动打开。")
                return dialog
            return None
        local = sheets[0] if len(sheets) == 1 else cache_directory(target["target_dir"], url)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(local)))
        self.context.log(f"任务 {target['label']}：参考大图命中本地缓存，未访问 Facebook。")
        return sheets

    def generate_task_references(self, rows):
        from qt_compat import QtWidgets

        jobs = unique_jobs(self.context.task_targets(rows))
        if not jobs:
            QtWidgets.QMessageBox.information(
                self.context.parent_widget, "没有 Facebook 参考",
                "所选任务的参考链接中没有可识别的 Facebook 视频地址。",
            )
            return None
        if self.batch_dialog is None:
            from .batch_ui import BatchContactSheetDialog
            self.batch_dialog = BatchContactSheetDialog(
                logger=self.context.log, parent=self.context.parent_widget,
            )
        dialog = self.batch_dialog
        if not dialog.set_jobs(jobs):
            QtWidgets.QMessageBox.information(
                dialog, "正在处理", "批量生成还在运行；完成或取消后再载入新的任务选择。",
            )
        dialog.show()
        dialog.showNormal()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def apply_settings(self, _config):
        return True

    def can_close(self):
        if self.dialog is not None and self.dialog.is_busy():
            return False, "视频分镜仍在处理；请先取消并等待后台任务结束。"
        if self.batch_dialog is not None and self.batch_dialog.is_busy():
            return False, "批量参考大图仍在处理；请先取消并等待后台任务结束。"
        return True, ""

    def stop(self):
        if self.dialog is not None:
            self.dialog.cancel_work()
            if not self.dialog.is_busy():
                self.dialog.dispose()
        if self.batch_dialog is not None:
            self.batch_dialog.cancel_work()
