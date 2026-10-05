import copy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.layers import componentize_layers,normalize_layers
from app_plugins.builtin.batch_text_video.layout import render_overlay,ensure_fonts
from app_plugins.builtin.batch_text_video.store import DEFAULTS,Store,fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog,BatchWorker
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.task_texts import split_title_body,record_from_task,entry_from_record,merge_sheet_entries,read_sheet_records
from app_plugins.builtin.batch_text_video.task_text_ui import TaskTextImportDialog


class PropertyAndTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_migration_transfers_settings_once_and_preserves_explicit_component_values(self):
        settings = {**DEFAULTS,"font":"Arial","title_max":70,"body_color":"#123456",
                    "vertical":"bottom","body_align":"right"}
        layers = componentize_layers(None,settings)
        title,body = next(l for l in layers if l["kind"] == "title"),next(l for l in layers if l["kind"] == "body")
        self.assertEqual(title["font_max"],70)
        self.assertEqual(body["color"],"#123456")
        self.assertEqual(body["vertical"],"bottom")
        self.assertEqual(body["align"],"right")
        body.update(x=20,y=30,width=35,height=40,font_max=50,align="left")
        self.assertEqual(componentize_layers(layers,DEFAULTS),normalize_layers(layers))

    def test_every_text_component_has_accessible_alignment_and_independent_geometry(self):
        panel = LayerPanel(normalize_layers(),".")
        for identifier in ("title","body"):
            panel.rebuild(identifier)
            self.assertTrue(panel.text_properties.isEnabled())
            panel.text_properties.controls["vertical"].setCurrentIndex(2)
            panel.text_properties.controls["align"].setCurrentIndex(2)
            panel.properties["x"].setValue(35)
            self.assertEqual(panel.current()["vertical"],"bottom")
            self.assertEqual(panel.current()["align"],"right")
            self.assertEqual(panel.current()["x"],35)
        self.assertEqual(panel.property_tabs.tabText(0),"排版")
        self.assertEqual(panel.property_tabs.tabText(1),"位置")
        self.assertEqual(panel.property_tabs.tabText(2),"内容")

    def test_nine_alignments_really_change_rendering_and_legacy_fields_no_longer_override(self):
        layers = componentize_layers(None,DEFAULTS)
        body = next(l for l in layers if l["kind"] == "body")
        body["enabled"] = False
        title = next(l for l in layers if l["kind"] == "title")
        title.update(x=50,y=50,width=90,height=80,font_min=50,font_max=50,color="#ffffff")
        images = set()
        for vertical in ("top","center","bottom"):
            for align in ("left","center","right"):
                title.update(vertical=vertical,align=align)
                image,_ = render_overlay("Test","",360,640,{**DEFAULTS,"layers":layers,"darkness":0})
                images.add(bytes(image.constBits().asstring(image.sizeInBytes())))
        self.assertEqual(len(images),9)
        reference,_ = render_overlay("Test","",360,640,{**DEFAULTS,"layers":layers})
        changed,_ = render_overlay("Test","",360,640,{**DEFAULTS,"layers":layers,"font":"nonexistent",
            "title_max":1,"margin_x":99,"vertical":"invalid","title_color":"red"})
        self.assertEqual(reference,changed)

    def test_basic_has_no_duplicate_text_controls_and_component_edits_survive_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            with patch.object(BatchTextVideoDialog,"request_background"):
                dialog = BatchTextVideoDialog(store=store)
                for key in ("font","vertical","title_align","body_align","title_min","body_max","margin_x","gap","line_spacing"):
                    self.assertNotIn(key,dialog.controls)
                dialog.layer_panel.rebuild("title")
                dialog.layer_panel.text_properties.controls["vertical"].setCurrentIndex(2)
                dialog.layer_panel.text_properties.controls["font_max"].setValue(75)
                self.assertTrue(dialog.persist())
                dialog.edit_timer.stop()
                dialog.close()
                loaded = next(l for l in store.load()["settings"]["layers"] if l["id"] == "title")
                self.assertEqual((loaded["vertical"],loaded["font_max"]),("bottom",75))
                reopened = BatchTextVideoDialog(store=store)
                reopened.layer_panel.rebuild("title")
                self.assertEqual(reopened.layer_panel.text_properties.controls["vertical"].currentData(),"bottom")
                reopened.close()

    def test_first_line_is_title_and_blank_or_single_line_fails(self):
        self.assertEqual(split_title_body("标题\r\n正文一\r\n\r\n正文二"),("标题","正文一\n\n正文二"))
        for value in ("", "\nBody", "only one line"):
            with self.assertRaises(ValueError):
                split_title_body(value)

    def records(self,folder):
        return [{"task_id":str(1000+i),"task_type":"reels","task_name":"中文标题\n中文正文",
            "task_audio_text":"Original title\nOriginal body","target_dir":str(Path(folder)/"reels"/str(1000+i)),
            "source_sheet":"table.ods","source_row":i+2} for i in range(3)]

    def test_copy_jobs_work_without_audio_and_refresh_without_duplicate_or_sequence_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            records = self.records(folder)
            entries = [entry_from_record(r) for r in records]
            state = fresh_state()
            state["backgrounds"] = ["one.mp4","two.mp4"]
            self.assertEqual(merge_sheet_entries(state,entries),(3,0))
            self.assertEqual([j["path"] for j in state["jobs"]],["one.mp4","two.mp4","one.mp4"])
            self.assertTrue(all(not j["voice_path"] and j["requires_title"] for j in state["jobs"]))
            self.assertEqual(merge_sheet_entries(state,entries),(0,0))
            entries[0]["body"] = "Changed body"
            state["jobs"][0]["status"] = "已完成"
            self.assertEqual(merge_sheet_entries(state,entries),(0,1))
            self.assertEqual(state["jobs"][0]["status"],"待生成")
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                merge_sheet_entries(state,[entries[0],entries[0]])
            self.assertEqual(state,before)

    def test_selector_filters_reels_and_number_switches_original_chinese_and_previews_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            records = self.records(folder)
            records.append({**records[0],"task_id":"15","task_type":"口播"})
            dialog = TaskTextImportDialog(records=records)
            dialog.minimum.setText("1001")
            dialog.kind.setCurrentIndex(dialog.kind.findData("reels"))
            self.assertEqual(dialog.list.rowCount(),2)
            self.assertEqual(dialog.list.item(0,2).text(),"Original title")
            dialog.source.setCurrentIndex(dialog.source.findData("task_name"))
            self.assertEqual(dialog.list.item(0,2).text(),"中文标题")
            self.assertEqual(dialog.preview.toPlainText(),"中文标题\n中文正文")
            dialog.accept_selection()
            self.assertEqual(len(dialog.entries),2)
            self.assertTrue(all(e["title"] == "中文标题" for e in dialog.entries))

    def test_invalid_title_is_reported_and_blocks_selected_import(self):
        with tempfile.TemporaryDirectory() as folder:
            record = self.records(folder)[0]
            record["task_audio_text"] = "\nMissing title"
            dialog = TaskTextImportDialog(records=[record])
            self.assertIn("没有标题",dialog.list.item(0,3).text())
            dialog.check_all(True)
            with patch.object(QtWidgets.QMessageBox,"warning") as warning:
                dialog.accept_selection()
            self.assertTrue(warning.called)
            self.assertFalse(dialog.entries)

    def test_required_title_checked_again_before_export(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state["jobs"] = [{"id":"x","path":"bg.mp4","title":"","body":"Body","requires_title":True,"status":"待生成"}]
            state = store.save(state,0)
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",{"static_text":lambda *_:self.fail("invalid title exported")}):
                BatchWorker(store,state,["x"]).run()
            job = store.load()["jobs"][0]
            self.assertEqual(job["status"],"需处理")
            self.assertIn("没有标题",job["error"])

    def test_ods_soft_linebreaks_and_repeated_paragraphs_survive_column_reordering(self):
        from odf.opendocument import OpenDocumentSpreadsheet
        from odf.table import Table,TableRow,TableCell
        from odf.text import P,LineBreak
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"fixture.ods"
            doc = OpenDocumentSpreadsheet()
            sheet = Table(name="Sheet1")
            doc.spreadsheet.addElement(sheet)
            headers = ["任务名称","类型","任务语音","序号"]
            header = TableRow()
            for value in headers:
                cell = TableCell(valuetype="string")
                cell.addElement(P(text=value))
                header.addElement(cell)
            sheet.addElement(header)
            row = TableRow()
            for value in ("中文标题\n中文正文","reels","Title\nBody\nBody","1000"):
                cell = TableCell(valuetype="string")
                paragraph = P()
                for i,line in enumerate(value.split("\n")):
                    if i:
                        paragraph.addElement(LineBreak())
                    paragraph.addText(line)
                cell.addElement(paragraph)
                row.addElement(cell)
            sheet.addElement(row)
            doc.save(str(path))
            before = path.read_bytes()
            task = SimpleNamespace(task_id="1000",task_type="reels",source_row=2,
                                   _full_task_name="flattened",task_audio_text="flattened")
            report = {"sheet_name":"Sheet1","headers":headers,
                      "matched_headers":{"task_name":["任务名称"],"task_audio_text":["任务语音"]}}
            with patch("model.OdsHelper.ReadTaskOds2",return_value=([task],report)):
                records,_ = read_sheet_records(path)
            self.assertEqual(records[0]["task_audio_text"],"Title\nBody\nBody")
            self.assertEqual(entry_from_record(records[0])["body"],"Body\nBody")
            self.assertEqual(entry_from_record(records[0],"task_name")["title"],"中文标题")
            self.assertEqual(path.read_bytes(),before)

    def test_refresh_current_task_does_not_write_old_editor_text_back_over_new_table_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            records = self.records(folder)[:1]
            entry = entry_from_record(records[0])
            state = fresh_state()
            from app_plugins.builtin.batch_text_video.pools import merge_copies
            merge_copies(state,[entry],sheet=True)
            store.save(state,0)
            with patch.object(BatchTextVideoDialog,"request_background"):
                dialog = BatchTextVideoDialog(store=store)
                dialog.copy_pool.setCurrentCell(0,0)
                changed = {**entry,"title":"Updated title","body":"Updated body"}
                imported = Mock(entries=[changed])
                imported.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
                with patch("app_plugins.builtin.batch_text_video.task_text_ui.TaskTextImportDialog",return_value=imported):
                    self.assertTrue(dialog.import_task_copy(records=records))
                self.assertEqual(store.load()["copy_pool"][0]["title"],"Updated title")
                self.assertEqual(store.load()["copy_pool"][0]["body"],"Updated body")
                self.assertEqual(len(store.load()["copy_pool"]),1)
                self.assertEqual(len(store.load()["jobs"]),0)
                dialog.close()


if __name__ == "__main__":
    unittest.main()
