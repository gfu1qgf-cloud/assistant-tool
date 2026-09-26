import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtGui, QtWidgets

from app_plugins.api import (
    MAIN_MENU,
    TASK_CONTEXT_MENU,
    TOOLS_MENU,
    PluginCommand,
    PluginMainWidget,
    PluginSettingsPage,
)
from app_plugins.builtin.audio_splitter import (
    AUDIO_SPLITTER_CONFIG_KEY,
    AudioSplitterPlugin,
    AudioSplitterSettingsPage,
)
from app_plugins.builtin.chrome_launcher import (
    ChromeLauncherPlugin,
    ChromeLauncherSettingsPage,
)
from app_plugins.builtin.inventory import InventoryPlugin, InventorySettingsPage
from app_plugins.builtin.music_ducker import (
    MUSIC_DUCKER_CONFIG_KEY,
    MusicDuckerPlugin,
    MusicDuckerSettingsPage,
)
from app_plugins.builtin.smart_video_editor import SmartVideoEditorPlugin
from app_plugins.builtin.smart_video_editor.engine import (
    SMART_VIDEO_EDITOR_CONFIG_KEY,
    SMART_VIDEO_PENDING_CONFIG_KEY,
)
from app_plugins.host import PluginHost
from app_plugins.host import (
    PLUGIN_MAIN_WIDGET_ORDER_CONFIG_KEY,
    PluginMainWidgetFrame,
    _card_color_indices,
)
from model.AppTheme import apply_ui_theme
from model.GlobalHotkey import (
    CHROME_NEXT_HOTKEY_CONFIG_KEY,
    INVENTORY_MANAGER_HOTKEY_CONFIG_KEY,
)


class FakeMainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.logs = []
        self.config = {}

    def appendLog(self, message, end="", level=None):
        self.logs.append(str(message))

    def load_config(self):
        return dict(self.config)

    def saveCurrentConfig(self):
        return True

    def showDesktopNotification(self, title, message, critical=False):
        pass

    def selectedTaskRows(self):
        return [2]

    def taskTargetsForRows(self, rows, require_loaded=True):
        return [{"row": row} for row in rows]


class DemoSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        self.loaded = None

    def load_config(self, config):
        self.loaded = dict(config)

    def update_config(self, config):
        config["demo_setting"] = 7


class DemoPlugin:
    plugin_id = "demo"
    required_api_version = 1

    def __init__(self):
        self.invocations = []
        self.monitor_enabled = False

    def register(self, context):
        context.register_command(
            PluginCommand(
                "open",
                "演示插件",
                lambda rows: self.invocations.append(("open", rows)),
                frozenset({MAIN_MENU}),
            )
        )
        context.register_command(
            PluginCommand(
                "monitor",
                "演示监听",
                self.toggle_monitor,
                frozenset({MAIN_MENU}),
                order=200,
                checkable=True,
                checked=lambda: self.monitor_enabled,
            )
        )
        context.register_command(
            PluginCommand(
                "task",
                "演示任务命令",
                lambda rows: self.invocations.append(("task", rows)),
                frozenset({TASK_CONTEXT_MENU}),
            )
        )
        context.register_settings_page(
            PluginSettingsPage("settings", "演示设置", DemoSettingsPage)
        )

    def toggle_monitor(self, rows, checked):
        self.monitor_enabled = checked
        self.invocations.append(("monitor", rows, checked))


class PluginHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.main = FakeMainWindow()
        self.host = PluginHost(self.main)

    def tearDown(self):
        self.main.deleteLater()

    def test_main_card_colors_are_distinct_stable_and_readable(self):
        ids = [f"plugin_{index}.quick_actions" for index in range(20)]
        first = _card_color_indices(ids)
        self.assertEqual(first, _card_color_indices(reversed(ids)))
        self.assertEqual(len(set(first.values())), len(ids))

        def luminance(color):
            values = [value / 12.92 if value <= 0.04045
                      else ((value + 0.055) / 1.055) ** 2.4
                      for value in (color.redF(), color.greenF(), color.blueF())]
            return sum(channel * weight for channel, weight in zip(
                values, (0.2126, 0.7152, 0.0722)
            ))

        for theme in ("light", "dark"):
            apply_ui_theme(self.app, theme)
            text = self.app.palette().color(QtGui.QPalette.ColorRole.WindowText)
            backgrounds = []
            for full_id in ids:
                card = PluginMainWidgetFrame(
                    full_id, full_id, QtWidgets.QPushButton("测试"),
                    color_index=first[full_id],
                )
                background, outline = card.card_colors()
                backgrounds.append(background.name())
                light, dark = sorted((luminance(text), luminance(background)), reverse=True)
                self.assertGreaterEqual((light + 0.05) / (dark + 0.05), 7.0)
                self.assertNotEqual(background, outline)
                card.deleteLater()
            self.assertEqual(len(set(backgrounds)), len(ids))
        apply_ui_theme(self.app, "light")

    def test_commands_register_in_main_and_task_menus(self):
        plugin = self.host.install(DemoPlugin())
        menu_bar = QtWidgets.QMenuBar(self.main)
        plugin_menu = self.host.attach_main_menu(menu_bar)

        self.assertEqual(plugin_menu.title(), "插件")
        self.assertEqual(plugin_menu.actions()[0].text(), "演示插件")
        self.assertEqual(plugin_menu.actions()[1].text(), "演示监听")
        plugin_menu.actions()[0].trigger()
        plugin_menu.actions()[1].trigger()

        task_menu = QtWidgets.QMenu(self.main)
        actions = self.host.populate_task_context_menu(task_menu, [1, 4])
        self.assertEqual(actions[0].text(), "演示任务命令")
        actions[0].trigger()
        self.assertEqual(
            plugin.invocations,
            [("open", ()), ("monitor", (), True), ("task", (1, 4))],
        )

    def test_settings_page_loads_and_updates_config(self):
        self.host.install(DemoPlugin())
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        controllers = self.host.create_settings_pages(dialog, tabs)
        self.host.load_settings_pages({"source": "loaded"})
        config = {}
        self.host.update_settings_config(config)

        self.assertEqual(tabs.tabText(0), "演示设置")
        self.assertEqual(controllers[0][2].loaded["source"], "loaded")
        self.assertEqual(config["demo_setting"], 7)

    def test_task_commands_support_submenus_and_visibility(self):
        invoked = []

        class GroupedPlugin:
            plugin_id = "grouped"
            required_api_version = 1

            def register(self, context):
                context.register_command(PluginCommand(
                    "visible",
                    "可见命令",
                    lambda rows: invoked.append(rows),
                    frozenset({TASK_CONTEXT_MENU}),
                    submenu="音频与字幕",
                    visible=lambda rows: bool(rows),
                ))
                context.register_command(PluginCommand(
                    "hidden",
                    "隐藏命令",
                    lambda rows: None,
                    frozenset({TASK_CONTEXT_MENU}),
                    submenu="音频与字幕",
                    visible=lambda _rows: False,
                ))

        self.host.install(GroupedPlugin())
        task_menu = QtWidgets.QMenu(self.main)
        actions = self.host.populate_task_context_menu(task_menu, [2, 5])

        self.assertEqual([action.text() for action in actions], ["可见命令"])
        self.assertEqual(len(task_menu.actions()), 1)
        submenu = task_menu.actions()[0].menu()
        self.assertIsNotNone(submenu)
        self.assertEqual(submenu.title(), "音频与字幕")
        self.assertEqual([action.text() for action in submenu.actions()], ["可见命令"])
        actions[0].trigger()
        self.assertEqual(invoked, [(2, 5)])

    def test_main_menu_commands_support_one_plugin_submenu(self):
        class GroupedMainPlugin:
            plugin_id = "grouped_main"

            def register(self, context):
                for command_id, title in (("one", "第一项"), ("two", "第二项")):
                    context.register_command(PluginCommand(
                        command_id,
                        title,
                        lambda _rows: None,
                        frozenset({MAIN_MENU}),
                        submenu="任务交付",
                    ))

        self.host.install(GroupedMainPlugin())
        plugin_menu = self.host.attach_main_menu(QtWidgets.QMenuBar(self.main))

        self.assertEqual(len(plugin_menu.actions()), 1)
        submenu = plugin_menu.actions()[0].menu()
        self.assertIsNotNone(submenu)
        self.assertEqual(submenu.title(), "任务交付")
        self.assertEqual(
            [action.text() for action in submenu.actions()],
            ["第一项", "第二项"],
        )

    def test_plugin_widget_is_mounted_inside_host_controlled_area(self):
        class WidgetPlugin:
            plugin_id = "widget_plugin"

            def __init__(self):
                self.widget = None

            def create_widget(self, parent):
                self.widget = QtWidgets.QPushButton("插件按钮", parent)
                return self.widget

            def register(self, context):
                context.register_main_widget(PluginMainWidget(
                    "quick_actions",
                    self.create_widget,
                    order=20,
                ))

        plugin = self.host.install(WidgetPlugin())
        frame = QtWidgets.QFrame(self.main)
        layout = QtWidgets.QVBoxLayout(frame)
        mounted = self.host.attach_main_widget_area(frame, layout)

        self.assertEqual(len(mounted), 1)
        card = layout.itemAt(0).widget()
        self.assertIsInstance(card, PluginMainWidgetFrame)
        self.assertIs(plugin.widget.parent(), card)
        self.assertIs(card.parent(), frame)
        self.assertIs(
            self.host.main_widget_controller("widget_plugin", "quick_actions"),
            plugin.widget,
        )
        self.assertIs(
            self.host.main_widget_frame("widget_plugin", "quick_actions"),
            card,
        )

    def test_plugin_widget_order_loads_moves_and_persists(self):
        class WidgetPlugin:
            required_api_version = 1

            def __init__(self, plugin_id, title, order):
                self.plugin_id = plugin_id
                self.display_name = title
                self.order = order

            def register(self, context):
                context.register_main_widget(PluginMainWidget(
                    "quick_actions",
                    lambda parent: QtWidgets.QPushButton(
                        self.display_name, parent
                    ),
                    order=self.order,
                ))

        self.main.config[PLUGIN_MAIN_WIDGET_ORDER_CONFIG_KEY] = [
            "second.quick_actions",
            "first.quick_actions",
        ]
        self.host.install(WidgetPlugin("first", "第一项", 10))
        self.host.install(WidgetPlugin("second", "第二项", 20))
        frame = QtWidgets.QFrame(self.main)
        layout = QtWidgets.QVBoxLayout(frame)
        self.host.attach_main_widget_area(frame, layout)
        self.assertEqual(self.host.main_widget_order(), [
            "second.quick_actions",
            "first.quick_actions",
        ])
        self.assertTrue(self.host.move_main_widget(
            "first.quick_actions",
            "second.quick_actions",
        ))
        config = {}
        self.host.update_runtime_config(config)
        self.assertEqual(
            config[PLUGIN_MAIN_WIDGET_ORDER_CONFIG_KEY],
            ["first.quick_actions", "second.quick_actions"],
        )

    def test_rejects_plugins_requiring_newer_api(self):
        plugin = DemoPlugin()
        plugin.required_api_version = self.host.api_version + 1
        with self.assertRaises(RuntimeError):
            self.host.install(plugin)

    def test_inventory_plugin_registers_all_supported_entry_points(self):
        self.host.install(InventoryPlugin())
        menu_bar = QtWidgets.QMenuBar(self.main)
        plugin_menu = self.host.attach_main_menu(menu_bar)
        task_menu = QtWidgets.QMenu(self.main)
        task_actions = self.host.populate_task_context_menu(task_menu, [0])
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        self.host.create_settings_pages(dialog, tabs)

        self.assertEqual(plugin_menu.actions()[0].text(), "库存与素材管理器")
        self.assertEqual(task_actions[0].text(), "分配库存图片…")
        self.assertEqual(tabs.tabText(0), "库存插件")

    def test_chrome_plugin_registers_launcher_next_command_and_settings(self):
        self.host.install(ChromeLauncherPlugin())
        menu_bar = QtWidgets.QMenuBar(self.main)
        plugin_menu = self.host.attach_main_menu(menu_bar)
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        self.host.create_settings_pages(dialog, tabs)

        self.assertEqual(
            [action.text() for action in plugin_menu.actions()],
            ["Chrome 启动器", "启动下一个 Chrome"],
        )
        self.assertEqual(tabs.tabText(0), "Chrome 插件")

    def test_audio_splitter_registers_tools_task_and_settings_entries(self):
        self.host.install(AudioSplitterPlugin())
        tools_menu = QtWidgets.QMenu("工具", self.main)
        core_action = tools_menu.addAction("宿主工具")
        self.host.attach_tools_menu(tools_menu)
        task_menu = QtWidgets.QMenu(self.main)
        task_actions = self.host.populate_task_context_menu(task_menu, [1, 3])
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        self.host.create_settings_pages(dialog, tabs)

        self.assertIs(tools_menu.actions()[0], core_action)
        self.assertEqual(tools_menu.actions()[-1].text(), "批量切分音频…")
        self.assertEqual(task_actions[0].text(), "切分任务音频…")
        self.assertEqual(tabs.tabText(0), "切分音频插件")

    def test_music_ducker_registers_menu_and_preserves_existing_config_key(self):
        self.main.config = {
            MUSIC_DUCKER_CONFIG_KEY: {
                "duck_to_percent": 17,
                "trigger_apps": ["resolve.exe"],
            }
        }
        plugin = self.host.install(MusicDuckerPlugin())
        menu_bar = QtWidgets.QMenuBar(self.main)
        plugin_menu = self.host.attach_main_menu(menu_bar)
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        controllers = self.host.create_settings_pages(dialog, tabs)
        self.host.load_settings_pages(self.main.config)
        saved = {}
        self.host.update_settings_config(saved)
        self.host.update_runtime_config(saved)

        self.assertEqual(plugin_menu.actions()[0].text(), "启用音乐压制")
        self.assertEqual(tabs.tabText(0), "音乐压制")
        self.assertEqual(plugin.settings["duck_to_percent"], 17)
        self.assertEqual(saved[MUSIC_DUCKER_CONFIG_KEY]["duck_to_percent"], 17)
        self.assertIsInstance(controllers[0][2], MusicDuckerSettingsPage)

    def test_smart_video_editor_owns_menu_task_settings_and_pending_state(self):
        self.main.config = {
            SMART_VIDEO_EDITOR_CONFIG_KEY: {"lead_padding_ms": 180},
            SMART_VIDEO_PENDING_CONFIG_KEY: [{
                "record_id": "demo",
                "task_id": "1",
                "task_dir": "C:/demo",
                "findings": [{"kind": "missing_script"}],
            }],
        }
        plugin = self.host.install(SmartVideoEditorPlugin())
        menu_bar = QtWidgets.QMenuBar(self.main)
        plugin_menu = self.host.attach_main_menu(menu_bar)
        tools_menu = QtWidgets.QMenu("工具", self.main)
        self.host.attach_tools_menu(tools_menu)
        self.host.start_all()
        task_menu = QtWidgets.QMenu(self.main)
        task_actions = self.host.populate_task_context_menu(task_menu, [0])
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        controllers = self.host.create_settings_pages(dialog, tabs)
        self.host.load_settings_pages(self.main.config)
        saved = {}
        self.host.update_settings_config(saved)
        self.host.update_runtime_config(saved)

        self.assertEqual(plugin_menu.actions()[0].text(), "智能剪辑并生成 SRT…")
        self.assertEqual(task_actions[0].text(), "智能剪辑并生成 SRT…")
        self.assertEqual(tools_menu.actions()[-1].text(), "待处理智能剪辑（1）")
        self.assertEqual(tabs.tabText(0), "智能剪辑")
        self.assertEqual(
            controllers[0][2].lead_padding_spinbox.value(), 180
        )
        self.assertEqual(saved[SMART_VIDEO_EDITOR_CONFIG_KEY]["lead_padding_ms"], 180)
        self.assertEqual(len(saved[SMART_VIDEO_PENDING_CONFIG_KEY]), 1)


class InventorySettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_preserves_legacy_hotkey_config_key(self):
        page = InventorySettingsPage()
        page.load_config({
            INVENTORY_MANAGER_HOTKEY_CONFIG_KEY: "Ctrl+Shift+I",
            "clipboard_google_drive_monitor_enabled": True,
        })
        config = {}
        page.update_config(config)
        self.assertEqual(
            config[INVENTORY_MANAGER_HOTKEY_CONFIG_KEY],
            "Ctrl+Shift+I",
        )
        self.assertTrue(config["clipboard_google_drive_monitor_enabled"])


class ChromeLauncherSettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_reuses_hotkey_key_without_touching_existing_chrome_data(self):
        original = {
            "chrome_preset_websites": ["https://example.test"],
            "chrome_profile_groups": {
                "version": 2,
                "groups": [{"id": "a", "name": "工作", "profile_directories": ["Profile 1"]}],
            },
            "chrome_profile_iterator": {
                "profile_directories": ["Profile 1"],
                "next_profile_directory": "Profile 1",
            },
        }
        config = dict(original)
        page = ChromeLauncherSettingsPage()
        page.load_config({CHROME_NEXT_HOTKEY_CONFIG_KEY: "Ctrl+Shift+N"})
        page.update_config(config)

        self.assertEqual(config[CHROME_NEXT_HOTKEY_CONFIG_KEY], "Ctrl+Shift+N")
        for key, value in original.items():
            self.assertEqual(config[key], value)


class AudioSplitterSettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_round_trips_all_split_parameters_in_plugin_config(self):
        page = AudioSplitterSettingsPage()
        page.load_config({
            AUDIO_SPLITTER_CONFIG_KEY: {
                "max_length_seconds": 45,
                "tolerance_seconds": 75,
                "min_silence_ms": 850,
                "silence_offset_db": 19.5,
                "extensions": [".wav", ".mp3"],
                "recursive": False,
                "include_source_name": True,
            }
        })
        config = {}
        page.update_config(config)

        self.assertEqual(config[AUDIO_SPLITTER_CONFIG_KEY], {
            "max_length_seconds": 45,
            "tolerance_seconds": 75,
            "min_silence_ms": 850,
            "silence_offset_db": 19.5,
            "extensions": [".wav", ".mp3"],
            "recursive": False,
            "include_source_name": True,
        })


if __name__ == "__main__":
    unittest.main()
