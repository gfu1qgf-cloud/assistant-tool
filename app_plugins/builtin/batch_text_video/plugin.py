from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand


class BatchTextVideoPlugin:
    plugin_id = "batch_text_video"
    display_name = "批量文案视频"
    version = "1.20"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.dialog = None

    def register(self, context):
        self.context = context
        # Optional host capability; compatible with legacy external hosts.
        register_node = getattr(context, "register_node", None)
        if callable(register_node):
            from .workflow_nodes import canvas_nodes
            for definition in canvas_nodes():
                register_node(definition)
            if callable(getattr(context, 'register_workflow_type', None)):
                from .node_effects import register_type
                register_type(context)
        context.register_command(PluginCommand(
            command_id="open", title="批量文案视频…",
            callback=lambda _rows: self.open_dialog(), locations=frozenset({MAIN_MENU}),
            order=17, tooltip="批量搭配视频与文案；控件来源绑定资源池，支持动态元素、抠像和字幕样式图库",
        ))
        context.register_command(PluginCommand(
            command_id="tasks", title="批量文案视频（表格文案）…", callback=self.import_tasks,
            locations=frozenset({TASK_CONTEXT_MENU}), order=70, enabled=lambda rows: bool(rows),
            tooltip="读取所选任务文案，第一行标题、其余正文；可选择原文或中文，背景可统一分配",
        ))

    def start(self):
        pass

    def workflow_runtime_context(self):
        from .project_runtime import project_payload
        from .store import Store
        # The running owner decides the project. Nodes cannot choose a saved
        # state file or switch this editor to another configuration.
        state = self.dialog.state if self.dialog is not None else Store().load()
        return project_payload(state)

    def open_dialog(self):
        if self.dialog is None:
            from .ui import BatchTextVideoDialog
            self.dialog = BatchTextVideoDialog(self.context, parent=self.context.parent_widget)
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def apply_settings(self, _config):
        return True

    def import_tasks(self, rows):
        targets = self.context.task_targets(tuple(rows or ()))
        from .task_texts import record_from_task
        records = []
        for target in targets:
            task = target.get("task")
            records.append(record_from_task(task,str(target.get("target_dir") or "")))
        if records:
            path = self.context.current_task_table_path()
            # Re-read the selected registration rows off-thread to preserve ODS
            # soft line breaks; the host's general task loader may flatten them.
            if path and path.is_file():
                self.open_dialog().import_task_copy(path=path,records=records,
                    selection_keys=[(r["task_type"],r["task_id"]) for r in records])
            else:
                self.open_dialog().import_task_copy(records=records)

    def can_close(self):
        if self.dialog is not None and self.dialog.has_local_work():
            # The host owns final rendering, but not our local preview/import
            # threads. Cancel those without canceling a manager-owned export.
            self.dialog.cancel_work(cancel_host=False)
            return False, "批量文案视频正在停止，请稍候再退出；已完成的视频和音乐进度会保留。"
        return True, ""

    def stop(self):
        if self.dialog is not None:
            if not self.can_close()[0]:
                return False
            self.dialog.close()
            return not getattr(self.dialog, '_close_save_failed', False)
        return True
