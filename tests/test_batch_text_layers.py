import copy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layers import (
    DEFAULT_LAYERS, MAX_IMAGES, ImageCache, compose_layers, image_rect, import_images,
    normalize_layers, read_image,
)
from app_plugins.builtin.batch_text_video.layout import TextDoesNotFit, ensure_fonts, render_overlay
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, BatchWorker


def picture(path, width=100, height=50, color="red"):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(color))
    if not image.save(str(path), "PNG"):
        raise OSError("test image failed")
    return image


def image_layer(path, **values):
    return {"id": "pic", "kind": "image", "name": "测试图片", "path": str(path),
            "enabled": True, "opacity": 100, "x": 50, "y": 50, "width": 100, "height": 100, **values}


def wait_until(check):
    if check():
        return
    loop = QtCore.QEventLoop()
    timer = QtCore.QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: loop.quit() if check() else None)
    timer.start()
    QtCore.QTimer.singleShot(10000, loop.quit)
    loop.exec()
    timer.stop()
    if not check():
        raise AssertionError("Qt worker did not finish")


class BatchTextLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_old_state_gets_independent_default_layers_without_writing_or_losing_music(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            state = fresh_state()
            state["settings"].pop("layers")
            state["music"] = [{"id": "song", "cursor": 42}]
            store.save(state, 0)
            before = store.path.read_bytes()
            loaded = store.load()
            self.assertEqual(loaded["settings"]["layers"], normalize_layers(DEFAULT_LAYERS))
            self.assertEqual(loaded["music"][0]["cursor"], 42)
            loaded["settings"]["layers"][0]["enabled"] = False
            self.assertTrue(store.load()["settings"]["layers"][0]["enabled"])
            self.assertTrue(DEFAULT_LAYERS[0]["enabled"])
            self.assertEqual(store.path.read_bytes(), before)

    def test_text_layers_can_be_above_or_below_picture_without_changing_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"red.png"
            picture(source)
            layers = normalize_layers()
            settings = {**DEFAULTS, "darkness": 0, "title_color": "#ffffff", "body_color": "#ffffff"}
            below, info = render_overlay("TITLE", "Body", 360, 640,
                {**settings, "layers": [image_layer(source), *layers]})
            above, other = render_overlay("TITLE", "Body", 360, 640,
                {**settings, "layers": [*layers, image_layer(source)]})
            self.assertEqual(info, other)
            white = [(x, y) for y in range(200, 400) for x in range(30, 330)
                     if below.pixelColor(x,y).red() > 240 and below.pixelColor(x,y).green() > 240]
            self.assertGreater(len(white), 30)
            for x,y in white:
                self.assertLess(above.pixelColor(x,y).green(), 5)

    def test_transparency_opacity_and_aspect_ratio_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"alpha.png"
            picture(source, color=QtGui.QColor(255,0,0,128))
            blank = QtGui.QImage(200,200,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            blank.fill(QtCore.Qt.GlobalColor.transparent)
            layers = normalize_layers([image_layer(source, opacity=50, width=50, height=50)])
            result = compose_layers({"title":blank,"body":blank}, layers, 200,200,0)
            self.assertAlmostEqual(result.pixelColor(100,100).alpha(), 64, delta=1)
            self.assertEqual(result.pixelColor(100,60).alpha(), 0)
            self.assertGreater(result.pixelColor(100,100).red(), 250)
            rectangle = image_rect(layers[0], read_image(source), 200,200)
            self.assertEqual((rectangle.width(),rectangle.height()), (100,50))

    def test_disabled_missing_picture_is_ignored_but_visible_missing_picture_blocks(self):
        settings = {**DEFAULTS, "layers": [image_layer("missing.png",enabled=False), *normalize_layers()]}
        result,_ = render_overlay("Title", "Body", 360,640,settings)
        self.assertFalse(result.isNull())
        settings["layers"][0]["enabled"] = True
        with self.assertRaises(FileNotFoundError):
            render_overlay("Title", "Body", 360,640,settings)

    def test_picture_only_composition_is_allowed_and_hidden_long_text_does_not_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"one.png"
            picture(source)
            layers = normalize_layers([image_layer(source)])
            for layer in layers:
                if layer["kind"] != "image":
                    layer["enabled"] = False
            result,_ = render_overlay("x"*50000,"",360,640,{**DEFAULTS,"layers":layers})
            self.assertFalse(result.isNull())
            layers[0]["enabled"] = False
            with self.assertRaises(TextDoesNotFit):
                render_overlay("Title","Body",360,640,{**DEFAULTS,"layers":layers})

    def test_image_import_copies_without_upscaling_deduplicates_and_survives_source_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/"用户's image.png"
            picture(source,80,120)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            result = import_images([source,source],root/"private")
            self.assertEqual(len(result["layers"]),2)
            self.assertEqual(result["layers"][0]["path"],result["layers"][1]["path"])
            self.assertNotEqual(result["layers"][0]["id"],result["layers"][1]["id"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
            copied = read_image(result["layers"][0]["path"])
            self.assertEqual((copied.width(),copied.height()),(80,120))
            self.assertTrue(Path(result["layers"][0]["thumbnail"]).is_file())
            source.unlink()
            image,_ = render_overlay("","",360,640,{**DEFAULTS,"layers":result["layers"]})
            self.assertFalse(image.isNull())

    def test_import_reports_bad_images_and_cancellation_does_not_modify_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good,bad = root/"good.png",root/"bad.png"
            picture(good)
            bad.write_bytes(b"not an image")
            result = import_images([bad,good],root/"private")
            self.assertEqual(len(result["layers"]),1)
            self.assertEqual(len(result["errors"]),1)
            event = threading.Event()
            event.set()
            cancelled = import_images([good],root/"cancelled",event)
            self.assertTrue(cancelled["cancelled"])
            self.assertEqual(cancelled["layers"],[])
            self.assertTrue(good.is_file())

    def test_photo_orientation_metadata_is_applied_to_copied_asset(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/"rotated.jpg"
            photo = Image.new("RGB", (80,120), "red")
            exif = photo.getexif()
            exif[274] = 6
            photo.save(source,exif=exif)
            result = import_images([source],root/"private")
            self.assertFalse(result["errors"])
            copied = read_image(result["layers"][0]["path"])
            self.assertEqual((copied.width(),copied.height()),(120,80))

    def test_layer_validation_rejects_unsafe_counts_unknown_types_and_invalid_values(self):
        with self.assertRaises(ValueError):
            normalize_layers([{"kind":"url"}])
        with self.assertRaises(ValueError):
            normalize_layers([image_layer("a.png",opacity=float("nan"))])
        with self.assertRaises(ValueError):
            normalize_layers([image_layer("a.png",enabled="false")])
        with self.assertRaises(ValueError):
            normalize_layers([image_layer("a.png",id=str(index)) for index in range(MAX_IMAGES+1)])
        with self.assertRaises(ValueError):
            normalize_layers([image_layer("a.png"),image_layer("b.png")])

    def test_preview_cache_is_bounded_and_file_changes_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"one.png"
            picture(source)
            cache = ImageCache(limit=10)
            result = cache.get(source,QtCore.QSize(50,50))
            self.assertFalse(result.isNull())
            self.assertEqual(len(cache.items),0)
            cache = ImageCache()
            first = cache.get(source,QtCore.QSize(50,50))
            picture(source,color="blue")
            second = cache.get(source,QtCore.QSize(50,50))
            self.assertEqual(first.pixelColor(0,0).red(),255)
            self.assertEqual(second.pixelColor(0,0).blue(),255)

    def test_panel_arrow_order_visibility_properties_and_text_to_top(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"one.png"
            picture(source)
            panel = LayerPanel([image_layer(source),*normalize_layers()],directory)
            panel.list.setCurrentRow(2)
            self.assertEqual(panel.current()["id"],"pic")
            panel.opacity.setValue(55)
            panel.properties["x"].setValue(25)
            self.assertEqual(panel.current()["opacity"],55)
            self.assertEqual(panel.current()["x"],25)
            panel.move_layer(-1)
            panel.move_layer(-1)
            self.assertEqual(panel.values()[-1]["id"],"pic")
            panel.list.currentItem().setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.assertFalse(panel.current()["enabled"])
            panel.text_to_top()
            self.assertEqual(panel.values()[-1]["kind"],"title")
            panel.remove_image()
            self.assertEqual(len(panel.values()),2)

    def test_native_list_row_move_updates_persistent_stacking_order(self):
        panel = LayerPanel(normalize_layers(),".")
        moved = panel.list.model().moveRow(QtCore.QModelIndex(),1,QtCore.QModelIndex(),0)
        self.assertTrue(moved)
        self.assertEqual([layer["kind"] for layer in panel.values()],["title","body"])

    def test_batch_dialog_import_and_preview_use_workers_and_persist_shared_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/"photo.png"
            picture(source)
            store = Store(root/"private")
            state = fresh_state()
            state["jobs"] = [{"id":str(index),"path":"background.mp4","title":"Title","body":"Body",
                              "name":str(index),"status":"待生成","error":""} for index in range(50)]
            store.save(state,0)
            threads = []
            original = ImageCache.get
            def get(cache,*args):
                threads.append(threading.get_ident())
                return original(cache,*args)
            with patch.object(BatchTextVideoDialog,"request_background"),patch.object(ImageCache,"get",get):
                dialog = BatchTextVideoDialog(store=store)
                try:
                    self.assertTrue(dialog.layer_panel.add_images([source]))
                    self.assertFalse(dialog.generate.isEnabled())
                    wait_until(lambda: dialog.layer_panel.worker is None)
                    dialog.edit_timer.stop()
                    self.assertTrue(dialog.persist())
                    dialog.update_preview()
                    wait_until(lambda: not dialog.is_busy())
                    self.assertTrue(threads)
                    self.assertTrue(all(identifier != threading.get_ident() for identifier in threads))
                    self.assertFalse(dialog.preview.overlay.isNull())
                    loaded = store.load()
                    self.assertEqual(len(loaded["jobs"]),50)
                    self.assertEqual(len(loaded["settings"]["layers"]),3)
                    self.assertEqual(loaded["settings"]["layers"][-1]["kind"],"title")
                    source.unlink()
                    self.assertTrue(Path(loaded["settings"]["layers"][0]["path"]).is_file())
                finally:
                    dialog.cancel_work()
                    wait_until(lambda: not dialog.is_busy())
                    dialog.close()

    def test_every_job_in_fifty_job_batch_receives_identical_layer_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/"state")
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([image_layer(root/"asset.png")])
            state["jobs"] = [{"id":str(index),"path":"bg.mp4","status":"待生成"} for index in range(50)]
            state = store.save(state,0)
            received = []
            def render(job,state,*_args):
                received.append(copy.deepcopy(state["settings"]["layers"]))
                output = root/(job["id"]+".mp4")
                output.touch()
                return output,None
            worker = BatchWorker(store,state,[job["id"] for job in state["jobs"]])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",{"static_text":render}):
                worker.run()
            self.assertEqual(len(received),50)
            self.assertTrue(all(layers == state["settings"]["layers"] for layers in received))
            self.assertTrue(all(job["status"] == "已完成" for job in store.load()["jobs"]))


if __name__ == "__main__":
    unittest.main()
