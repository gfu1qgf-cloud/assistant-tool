import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.host import PluginContext
from app_plugins.builtin.batch_text_video.plugin import BatchTextVideoPlugin
from app_plugins.builtin.batch_text_video.store import Store,DEFAULTS,fresh_state
from app_plugins.builtin.batch_text_video.layout import ensure_fonts,render_overlay,render_preview_overlay,TextDoesNotFit
from app_plugins.builtin.batch_text_video.layers import componentize_layers
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog,Preview


class PreviewLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def context(self,root,name="任务登记表格.ods"):
        window = SimpleNamespace(loaded_project_dir=root,load_config=lambda:{"task_table_file_name":name})
        return PluginContext(SimpleNamespace(main_window=window),"batch_text_video")

    def settings(self):
        settings = copy.deepcopy(DEFAULTS)
        settings["layers"] = componentize_layers(None,settings)
        return settings

    def test_current_table_service_uses_loaded_root_and_custom_basename(self):
        with tempfile.TemporaryDirectory() as folder:
            context = self.context(Path(folder)/"1002")
            context.parent_widget.getTodayDir = lambda:Path(folder)/"1003"
            self.assertEqual(context.current_task_table_path(),Path(folder)/"1002"/"任务登记表格.ods")
            context.parent_widget.loaded_project_dir = None
            self.assertIsNone(context.current_task_table_path())

    def test_table_service_rejects_path_traversal(self):
        for name in ("..","../secret.ods",r"..\secret.ods",r"C:\secret.ods"):
            with self.assertRaises(ValueError):
                self.context(".",name).current_task_table_path()

    def test_button_reads_current_table_without_a_file_picker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"任务登记表格.ods"
            path.touch()
            dialog = BatchTextVideoDialog(context=self.context(folder),store=Store(Path(folder)/"private"))
            with patch.object(QtWidgets.QFileDialog,"getOpenFileName",side_effect=AssertionError("must not choose again")),patch.object(dialog,"import_task_copy",return_value=True) as importer:
                self.assertTrue(dialog.import_sheet_text())
                importer.assert_called_once_with(path=path)
            dialog.close()

    def test_unloaded_or_missing_table_is_explained_without_standalone_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            context = self.context(None)
            context._host.main_window.appendLog = Mock()
            dialog = BatchTextVideoDialog(context=context,store=Store(Path(folder)/"private"))
            with patch.object(dialog,"import_task_copy") as importer:
                self.assertFalse(dialog.import_sheet_text())
                self.assertIn("主程序加载任务",dialog.status.text())
                context.parent_widget.loaded_project_dir = folder
                self.assertFalse(dialog.import_sheet_text())
                self.assertIn("不存在",dialog.status.text())
                importer.assert_not_called()
            dialog.close()

    def test_context_menu_reuses_host_table_not_a_guessed_parent_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"任务登记表格.ods"
            path.touch()
            context = Mock()
            context.current_task_table_path.return_value = path
            context.task_targets.return_value = [{"task":SimpleNamespace(task_id="1000",task_type="reels",task_audio_text="Title\nBody",task_name="Name"),"target_dir":str(Path(folder)/"nested"/"reels"/"1000")}]
            plugin = BatchTextVideoPlugin()
            plugin.register(context)
            with patch.object(plugin,"open_dialog") as opener:
                plugin.import_tasks([0])
            call = opener.return_value.import_task_copy.call_args.kwargs
            self.assertEqual(call["path"],path)
            self.assertEqual(call["selection_keys"],[("reels","1000")])

    def test_valid_preview_is_pixel_identical_to_export_overlay(self):
        settings = self.settings()
        exported,expected = render_overlay("Title","Body text",360,640,settings)
        preview,info = render_preview_overlay("Title","Body text",360,640,settings)
        self.assertEqual(preview,exported)
        self.assertEqual(info,expected)

    def test_title_overflow_preserves_body_and_remains_export_blocking(self):
        settings = self.settings()
        title = next(layer for layer in settings["layers"] if layer["id"] == "title")
        title.update(height=4,font_min=64,font_max=72)
        text = "Long title "*30
        before = copy.deepcopy(settings)
        preview,info = render_preview_overlay(text,"Body text",360,640,settings)
        self.assertFalse(preview.isNull())
        self.assertTrue(info["export_blocked"])
        self.assertIn("body",info["component_sizes"])
        self.assertNotIn("title",info["component_sizes"])
        self.assertTrue(any("标题" in warning for warning in info["warnings"]))
        self.assertEqual(settings,before)
        with self.assertRaises(TextDoesNotFit):
            render_overlay(text,"Body text",360,640,settings)

    def test_background_paints_even_if_text_overlay_fails(self):
        preview = Preview()
        preview.resize(300,400)
        preview.background = QtGui.QImage(180,320,QtGui.QImage.Format.Format_RGB32)
        preview.background.fill(QtGui.QColor("#129234"))
        preview.show()
        self.app.processEvents()
        image = preview.grab().toImage()
        self.assertEqual(image.pixelColor(150,200).name(),"#129234")
        preview.close()

    def test_editing_text_automatically_updates_and_does_not_hide_background_on_overflow(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state["jobs"] = [{"id":"a","path":"background.mp4","name":"a","title":"Title","body":"Body","status":"待生成","output":"","error":""}]
            store.save(state,0)
            with patch.object(BatchTextVideoDialog,"request_background"):
                dialog = BatchTextVideoDialog(store=store)
                dialog.preview.background = QtGui.QImage(180,320,QtGui.QImage.Format.Format_RGB32)
                dialog.preview.background.fill(QtGui.QColor("green"))
                dialog.title.setPlainText("A very long title "*80)
                dialog.body.setPlainText("Updated body")
                loop = QtCore.QEventLoop()
                QtCore.QTimer.singleShot(850,loop.quit)
                loop.exec()
                self.assertEqual(store.load()["jobs"][0]["body"],"Updated body")
                self.assertFalse(dialog.preview.overlay.isNull())
                self.assertFalse(dialog.preview.background.isNull())
                self.assertIn("需调整",dialog.preview_status.text())
                dialog.close()

    def test_stale_overlay_result_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = BatchTextVideoDialog(store=Store(folder))
            dialog._overlay_request = ("new",)
            dialog._overlay_ready({"request":("old",),"error":"old error"})
            self.assertNotIn("old error",dialog.preview_status.text())
            dialog.close()

    def test_refresh_explicitly_retries_current_background(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state["jobs"] = [{"id":"a","path":"background.mp4","name":"a","title":"Title","body":"Body","status":"待生成","output":"","error":""}]
            store.save(state,0)
            with patch.object(BatchTextVideoDialog,"request_background") as request:
                dialog = BatchTextVideoDialog(store=store)
                request.reset_mock()
                dialog.refresh_preview_button.click()
                request.assert_called_once_with("background.mp4",force=True)
                dialog.close()

    def test_layer_fields_and_labels_fit_without_horizontal_scrolling(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = BatchTextVideoDialog(store=Store(folder))
            dialog.tabs.setCurrentIndex(dialog.layer_tab_index)
            dialog.show()
            try:
                for size in (9,11,13):
                    font = QtGui.QFont(dialog.font())
                    font.setPointSize(size)
                    dialog.setFont(font)
                    dialog.resize(1250,850)
                    dialog.splitter.setSizes([320,450,420])
                    panel = dialog.layer_panel
                    for tab in (0,1,2):
                        panel.property_tabs.setCurrentIndex(tab)
                        self.app.processEvents()
                        field = panel.text_properties.controls["font"] if tab == 0 else panel.properties["width"] if tab == 1 else panel.content_editor.source
                        field.setFocus()
                        self.app.processEvents()
                        viewport = dialog.layer_scroll.viewport()
                        self.assertEqual(dialog.layer_scroll.horizontalScrollBar().maximum(),0)
                        for label in panel.findChildren(QtWidgets.QLabel):
                            if label.isVisible() and label.text() in {"水平对齐","垂直对齐","最小字号","最大字号","横向中心","图层名"}:
                                rect = QtCore.QRect(label.mapTo(viewport,QtCore.QPoint()),label.size())
                                self.assertGreaterEqual(rect.left(),0)
                                self.assertLessEqual(rect.right(),viewport.width())
                        self.assertLessEqual(field.mapTo(viewport,QtCore.QPoint(field.width(),0)).x(),viewport.width())
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
