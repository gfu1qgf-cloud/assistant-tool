"""One plugin for the existing DaVinci remote tab and its tools."""

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from app_plugins.api import MAIN_MENU, PluginCommand, PluginTabPage
from .ui import DaVinciRemotePanel


class DaVinciRemotePlugin:
    plugin_id = "davinci_remote"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.panel = None
        self.dialog = None
        self.settings = {}

    def register(self, context):
        self.context = context
        raw = context.load_config().get("davinci_remote_settings", {})
        self.settings = deepcopy(raw) if isinstance(raw, dict) else {}
        context.register_command(PluginCommand(
            command_id="open",
            title="达芬奇遥控器",
            callback=lambda _rows: self.open_dialog(),
            locations=frozenset({MAIN_MENU}),
            order=45,
        ))
        context.register_tab_page(PluginTabPage(
            page_id="remote",
            title="达芬奇遥控器",
            factory=self.create_panel,
            order=10,
        ))

    def create_panel(self, parent=None):
        self.panel = DaVinciRemotePanel(self, parent)
        return self.panel

    def open_dialog(self, page=0, probe=False):
        from .ui import DaVinciRemoteDialog

        created = self.dialog is None
        if self.dialog is None:
            self.dialog = DaVinciRemoteDialog(self, self.context.parent_widget)
        self.dialog.tabs.setCurrentIndex(page)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        if probe or created:
            self.dialog.probe()

    def update_config(self, config):
        config["davinci_remote_settings"] = dict(self.settings)

    def save_settings(self, section, settings):
        saved = dict(settings)
        if section == "export":
            # The task name must be derived from the *current* timeline next run.
            saved.pop("task_name", None)
        self.settings[section] = saved
        self.context.save_config()

    def fusion_presets(self):
        """Return detached node templates from the private main configuration."""
        raw = self.settings.get("fusion_presets", [])
        if not isinstance(raw, list):
            return []
        return [deepcopy(item) for item in raw if isinstance(item, dict)
                and isinstance(item.get("id"), str) and item["id"]
                and isinstance(item.get("name"), str) and item["name"].strip()
                and isinstance(item.get("text"), str) and item["text"].strip()]

    def _write_fusion_presets(self, presets):
        previous = deepcopy(self.settings)
        self.settings["fusion_presets"] = presets
        try:
            if self.context.save_config() is False:
                raise RuntimeError("配置未能保存，常用节点未修改。请查看程序日志。")
        except Exception:
            self.settings = previous
            raise

    def save_fusion_preset(self, name, text, mapping=None):
        name, text = name.strip(), text.strip()
        if not name or not text:
            raise ValueError("节点名称和节点文本不能为空。")
        presets = self.fusion_presets()
        old = next((item for item in presets if item["name"].casefold() == name.casefold()), None)
        entry = deepcopy(old) if old else {"id": uuid.uuid4().hex}
        entry.update(name=name, text=text, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        # Only named ports are reusable. Track/timeline/clip identities must
        # always come from a fresh preview, never from a saved template.
        mapping = mapping or {}
        entry["entries"] = [port for port in mapping.get("entries", []) if isinstance(port, str)]
        entry["exit"] = str(mapping.get("exit") or "")
        entry["has_mapping"] = bool(mapping)
        if old:
            presets[presets.index(old)] = entry
        else:
            presets.append(entry)
        self._write_fusion_presets(presets)
        return deepcopy(entry)

    def rename_fusion_preset(self, preset_id, name):
        name = name.strip()
        if not name:
            raise ValueError("节点名称不能为空。")
        presets = self.fusion_presets()
        entry = next((item for item in presets if item["id"] == preset_id), None)
        if entry is None:
            raise ValueError("该节点已不存在，请重新选择。")
        if any(item["id"] != preset_id and item["name"].casefold() == name.casefold()
               for item in presets):
            raise ValueError("已有同名节点，请换一个名称。")
        entry["name"] = name
        self._write_fusion_presets(presets)

    def delete_fusion_preset(self, preset_id):
        presets = self.fusion_presets()
        remaining = [item for item in presets if item["id"] != preset_id]
        if len(remaining) == len(presets):
            return False
        self._write_fusion_presets(remaining)
        return True

    def report_status(self, status):
        if self.panel is not None:
            self.panel.status.setText(status)

    def can_close(self):
        dialog = self.dialog
        if dialog is not None and dialog.process is not None:
            from qt_compat import QtCore

            if dialog.process.state() != QtCore.QProcess.ProcessState.NotRunning:
                return False, "达芬奇正在处理时间线，请等待完成后再退出程序。"
        return True, ""
