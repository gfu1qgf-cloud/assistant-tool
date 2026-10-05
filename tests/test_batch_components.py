import copy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.components import (
    new_text_component,resolve_sources,commit_sequences,
)
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import render_overlay,ensure_fonts,TextDoesNotFit
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.component_ui import SequenceDialog
from app_plugins.builtin.batch_text_video.store import DEFAULTS,Store,fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchWorker,BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.engine import Cancelled


def texts(items=("A","B"),cursor=0):
    layer = new_text_component()
    layer.update(id="custom",source="sequence",sequence_cursor=cursor,
                 sequence_items=[{"id":str(i),"text":text} for i,text in enumerate(items)])
    return layer


class ComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_preview_is_pure_and_multiple_sequences_cycle_independently(self):
        one = texts(("A","B"),1)
        two = texts(("1","2","3"),2)
        two["id"] = "other"
        layers = normalize_layers([one,two])
        before = copy.deepcopy(layers)
        for _ in range(3):
            resolved,plan = resolve_sources(layers,"title","body")
            self.assertEqual([item["text"] for item in resolved[:2]],["B","3"])
        self.assertEqual(before,layers)
        advanced = commit_sequences(layers,plan)
        self.assertEqual([item["sequence_cursor"] for item in advanced[:2]],[0,0])
        self.assertEqual(before,layers)

    def test_hidden_sequence_does_not_consume_and_empty_active_sequence_fails(self):
        layer = texts(())
        layer["enabled"] = False
        _,plan = resolve_sources(normalize_layers([layer]))
        self.assertFalse(plan)
        layer["enabled"] = True
        with self.assertRaisesRegex(ValueError,"序列为空"):
            resolve_sources(normalize_layers([layer]))

    def test_changed_sequence_cannot_commit_stale_plan(self):
        layers = normalize_layers([texts()])
        _,plan = resolve_sources(layers)
        changed = copy.deepcopy(layers)
        changed[0]["sequence_items"][0]["text"] = "changed"
        with self.assertRaises(ValueError):
            commit_sequences(changed,plan)
        self.assertEqual(changed[0]["sequence_cursor"],0)

    def test_arbitrary_text_boxes_have_independent_style_and_positions(self):
        one,two = new_text_component(),new_text_component()
        one.update(text="Top text",y=20,color="#ff0000")
        two.update(text="Bottom text",y=80,color="#0000ff")
        layers = normalize_layers([one,two])
        for layer in layers:
            if layer["kind"] in {"title","body"}:
                layer["enabled"] = False
        image,info = render_overlay("ignored","ignored",360,640,{**DEFAULTS,"darkness":0,"layers":layers})
        self.assertEqual(len(info["component_sizes"]),2)
        self.assertGreater(sum(image.pixelColor(x,y).red()>200 for y in range(50,190) for x in range(360)),10)
        self.assertGreater(sum(image.pixelColor(x,y).blue()>200 for y in range(450,570) for x in range(360)),10)
        one.update(text="far too much text "*1000,height=5,font_min=50,font_max=60)
        with self.assertRaises(TextDoesNotFit):
            render_overlay("","",360,640,{**DEFAULTS,"layers":normalize_layers([one])})

    def test_legacy_title_can_use_sequence_without_changing_original_job_text(self):
        layers = normalize_layers()
        title = next(item for item in layers if item["kind"] == "title")
        title.update(source="sequence",sequence_items=[{"id":"first","text":"SEQUENCE TITLE"}])
        rendered,_ = render_overlay("Job title","Body",360,640,{**DEFAULTS,"layers":layers})
        reference,_ = render_overlay("SEQUENCE TITLE","Body",360,640,DEFAULTS)
        self.assertEqual(rendered,reference)

    def test_sequence_progress_persists_with_completed_output_and_music(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([texts()])
            state["jobs"] = [{"id":"j","path":"bg.mp4","status":"待生成"}]
            state["music"] = [{"id":"song","cursor":0}]
            state = store.save(state,0)
            _,plan = resolve_sources(state["settings"]["layers"])
            with self.assertRaises(FileNotFoundError):
                store.completed(state,"j",Path(folder)/"missing.mp4",None,sequence_plan=plan)
            self.assertEqual(store.load()["settings"]["layers"][0]["sequence_cursor"],0)
            output = Path(folder)/"done.mp4"
            output.touch()
            done = store.completed(state,"j",output,{"id":"song","cursor":4,"audio_key":"ok"},sequence_plan=plan)
            self.assertEqual(done["settings"]["layers"][0]["sequence_cursor"],1)
            self.assertEqual(Store(folder).load()["music"][0]["cursor"],4)

    def test_batch_failure_does_not_consume_and_next_job_continues_from_same_item(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([texts()])
            state["jobs"] = [{"id":str(i),"path":"bg.mp4","status":"待生成"} for i in range(4)]
            state = store.save(state,0)
            received = []
            def render(job,state,*_args):
                resolved,_ = resolve_sources(state["settings"]["layers"])
                received.append(resolved[0]["text"])
                if job["id"] == "1":
                    raise ValueError("simulated failure")
                output = Path(folder)/(job["id"]+".mp4")
                output.touch()
                return output,None
            worker = BatchWorker(store,state,["0","1","2","3"])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",{"static_text":render}):
                worker.run()
            self.assertEqual(received,["A","B","B","A"])
            self.assertEqual(store.load()["settings"]["layers"][0]["sequence_cursor"],1)
            self.assertEqual(store.load()["jobs"][1]["status"],"需处理")

    def test_fifty_job_batch_rotates_and_resumes_from_saved_position(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([texts(("A","B","C"))])
            state["jobs"] = [{"id":str(i),"path":"bg.mp4","status":"待生成"} for i in range(50)]
            state = store.save(state,0)
            received = []
            def render(job,state,*_args):
                resolved,_ = resolve_sources(state["settings"]["layers"])
                received.append(resolved[0]["text"])
                output = Path(folder)/(job["id"]+".mp4")
                output.touch()
                return output,None
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",{"static_text":render}):
                BatchWorker(store,state,[str(i) for i in range(17)]).run()
                BatchWorker(Store(folder),store.load(),[str(i) for i in range(17,50)]).run()
            self.assertEqual(received,[("A","B","C")[i%3] for i in range(50)])
            self.assertEqual(store.load()["settings"]["layers"][0]["sequence_cursor"],2)

    def test_cancel_and_invalid_configuration_leave_store_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([texts()])
            state["jobs"] = [{"id":"j","path":"bg.mp4","status":"待生成"}]
            state = store.save(state,0)
            worker = BatchWorker(store,state,["j"])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",{"static_text":lambda *_args:(_ for _ in ()).throw(Cancelled())}):
                worker.run()
            self.assertEqual(store.load(),state)
            broken = copy.deepcopy(state)
            broken["settings"]["layers"][0]["sequence_items"][0]["text"] = "x"*40001
            before = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.save(broken,state["revision"])
            self.assertEqual(before,store.path.read_bytes())

    def test_panel_adds_text_box_and_switches_content_sources(self):
        panel = LayerPanel(normalize_layers(),".")
        panel.add_text()
        self.assertEqual(panel.current()["kind"],"text")
        self.assertTrue(panel.remove_button.isEnabled())
        panel.content_editor.text.setPlainText("Fixed text")
        self.assertEqual(panel.current()["text"],"Fixed text")
        combo = panel.content_editor.source
        combo.setCurrentIndex(combo.findData("sequence"))
        self.assertEqual(panel.current()["sequence_items"][0]["text"],"Fixed text")
        panel.content_editor.reset_cursor()
        panel.remove_image()
        self.assertEqual(len(panel.values()),2)

    def test_picture_sequence_import_is_one_component_and_uses_original_proportions(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for color in ("red","blue"):
                path = Path(folder)/(color+".png")
                image = QtGui.QImage(80,120,QtGui.QImage.Format.Format_ARGB32)
                image.fill(QtGui.QColor(color))
                self.assertTrue(image.save(str(path),"PNG"))
                paths.append(path)
            panel = LayerPanel(normalize_layers(),folder)
            self.assertTrue(panel.add_images(paths,as_sequence=True))
            loop = QtCore.QEventLoop()
            timer = QtCore.QTimer()
            timer.setInterval(10)
            timer.timeout.connect(lambda:loop.quit() if panel.worker is None else None)
            timer.start()
            QtCore.QTimer.singleShot(10000,loop.quit)
            loop.exec()
            timer.stop()
            self.assertIsNone(panel.worker)
            layers = panel.values()
            self.assertEqual(len(layers),3)
            image = next(layer for layer in layers if layer["kind"] == "image")
            self.assertEqual(len(image["sequence_items"]),2)
            self.assertEqual(image["source"],"sequence")
            red,_ = render_overlay("","",360,640,{**DEFAULTS,"layers":layers,"darkness":0})
            _,plan = resolve_sources(layers)
            advanced = commit_sequences(layers,plan)
            blue,_ = render_overlay("","",360,640,{**DEFAULTS,"layers":advanced,"darkness":0})
            self.assertGreater(red.pixelColor(180,320).red(),250)
            self.assertGreater(blue.pixelColor(180,320).blue(),250)
            for path in paths:
                path.unlink()
            # Imported assets survive the original files being moved/deleted.
            render_overlay("","",360,640,{**DEFAULTS,"layers":advanced})

    def test_sequence_dialog_keeps_multiline_text_as_one_item(self):
        dialog = SequenceDialog(normalize_layers([texts()])[0])
        dialog.list.setCurrentRow(0)
        dialog.edit.setPlainText("line one\nline two")
        self.assertEqual(len(dialog.items),2)
        self.assertEqual(dialog.items[0]["text"],"line one\nline two")
        dialog.add_item()
        self.assertEqual(len(dialog.items),3)
        dialog.remove_item()
        self.assertEqual(len(dialog.items),2)

    def test_batch_reload_updates_visible_sequence_cursor_before_next_save(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(folder)
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([texts()])
            state["jobs"] = [{"id":"j","path":"bg.mp4","status":"待生成","title":"Title","body":"Body","name":"j"}]
            state = store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            try:
                _,plan = resolve_sources(state["settings"]["layers"])
                output = Path(folder)/"done.mp4"
                output.touch()
                store.completed(state,"j",output,None,sequence_plan=plan)
                class Worker:
                    def deleteLater(self):
                        pass
                dialog.worker = Worker()
                dialog._batch_finished()
                dialog.edit_timer.stop()
                self.assertTrue(dialog.persist())
                self.assertEqual(store.load()["settings"]["layers"][0]["sequence_cursor"],1)
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
