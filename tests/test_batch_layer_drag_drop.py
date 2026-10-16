"""Layer-list drops must survive catalog reparenting and preserve stacking data."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.components import new_text_component


class LayerDragDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def panel(self, directory='.'):
        layers = []
        for index in range(3):
            layer = new_text_component()
            layer.update(id=f'custom-{index}', name=f'Text {index}', text=f'Body {index}', x=20+index)
            layers.append(layer)
        panel = LayerPanel(normalize_layers(layers), directory)
        # The real dialog moves this out of LayerPanel into its left-hand tab.
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel.catalog)
        scroll.resize(440, 360)
        scroll.show()
        self.app.processEvents()
        self.addCleanup(panel.deleteLater)
        self.addCleanup(scroll.deleteLater)
        return panel, scroll

    def drop(self, widget, mime, position=None, action=QtCore.Qt.DropAction.CopyAction):
        position = position or QtCore.QPoint(50, 50)
        buttons = QtCore.Qt.MouseButton.LeftButton
        modifiers = QtCore.Qt.KeyboardModifier.NoModifier
        enter = QtGui.QDragEnterEvent(position, action, mime, buttons, modifiers)
        self.app.sendEvent(widget, enter)
        move = QtGui.QDragMoveEvent(position, action, mime, buttons, modifiers)
        self.app.sendEvent(widget, move)
        event = QtGui.QDropEvent(QtCore.QPointF(position), action, mime, buttons, modifiers)
        self.app.sendEvent(widget, event)
        return enter, move, event

    def test_image_drop_on_reparented_list_creates_layers(self):
        panel, _ = self.panel()
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile('C:/fixtures/overlay.png')])
        with patch.object(panel.list, 'import_files', return_value=True) as add:
            events = self.drop(panel.list.viewport(), mime)
        self.assertTrue(all(event.isAccepted() for event in events))
        add.assert_called_once_with(['C:/fixtures/overlay.png'])

    def test_internal_drop_updates_data_once_without_losing_properties(self):
        panel, _ = self.panel()
        before = {layer['id']: layer for layer in panel.values()}
        first = panel.list.item(0)
        identifier = first.data(QtCore.Qt.ItemDataRole.UserRole)
        panel.list.setCurrentItem(first)
        changes = []
        panel.changed.connect(lambda: changes.append(panel.values()))
        mime = panel.list.mimeData([first])
        position = panel.list.visualItemRect(panel.list.item(3)).bottomLeft()+QtCore.QPoint(30, 0)
        events = self.drop(panel.list.viewport(), mime, position, QtCore.Qt.DropAction.MoveAction)
        self.assertTrue(all(event.isAccepted() for event in events))
        self.assertEqual(panel.list.row(first), 3)
        self.assertEqual(panel.current()['id'], identifier)
        self.assertEqual(len(changes), 1)
        self.assertEqual({layer['id']: layer for layer in panel.values()}, before)
        self.assertEqual([layer['id'] for layer in panel.values()],
            [panel.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in reversed(range(panel.list.count()))])

    def test_native_model_move_keeps_panel_in_sync(self):
        panel, _ = self.panel()
        before = [layer['id'] for layer in panel.values()]
        result = panel.list.model().moveRows(QtCore.QModelIndex(), 0, 1, QtCore.QModelIndex(), 3)
        after = [layer['id'] for layer in panel.values()]
        self.assertTrue(result)
        self.assertNotEqual(before, after)

    def test_noop_name_focus_out_never_resets_dragging_list(self):
        panel, _ = self.panel()
        with patch.object(panel, 'rebuild') as rebuild:
            panel._name_changed()
            panel.name.setText('A renamed layer')
            panel._name_changed()
        rebuild.assert_not_called()
        self.assertEqual(panel.current()['name'], 'A renamed layer')

    def test_catalog_empty_area_accepts_gif_and_video_as_copy(self):
        panel, _ = self.panel()
        mime = QtCore.QMimeData()
        paths = ['C:/fixtures/animated.gif', 'C:/fixtures/overlay.mov']
        mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
        with patch.object(panel.catalog, 'import_files', return_value=True) as add:
            events = self.drop(panel.catalog, mime, action=QtCore.Qt.DropAction.CopyAction|QtCore.Qt.DropAction.MoveAction)
        self.assertTrue(all(event.isAccepted() for event in events))
        self.assertEqual(events[-1].dropAction(), QtCore.Qt.DropAction.CopyAction)
        add.assert_called_once_with(paths)

    def test_other_list_token_and_unsupported_files_are_rejected(self):
        panel, _ = self.panel()
        other, _ = self.panel()
        mime = other.list.mimeData([other.list.item(0)])
        self.assertFalse(self.drop(panel.list.viewport(), mime, action=QtCore.Qt.DropAction.MoveAction)[0].isAccepted())
        for urls in ([], [QtCore.QUrl('https://example.com/overlay.png')],
                     [QtCore.QUrl.fromLocalFile('C:/fixtures/image.png'), QtCore.QUrl.fromLocalFile('C:/fixtures/script.exe')]):
            mime = QtCore.QMimeData(); mime.setUrls(urls)
            with patch.object(panel.list, 'import_files') as add:
                self.assertFalse(self.drop(panel.list.viewport(), mime)[0].isAccepted())
            add.assert_not_called()

    def test_disabled_catalog_or_active_import_rejects_files(self):
        panel, _ = self.panel()
        mime = QtCore.QMimeData(); mime.setUrls([QtCore.QUrl.fromLocalFile('C:/fixtures/overlay.png')])
        panel.worker = object()
        with patch.object(panel.list, 'import_files') as add:
            self.assertFalse(self.drop(panel.list.viewport(), mime)[0].isAccepted())
        add.assert_not_called(); panel.worker = None
        panel.catalog.setEnabled(False)
        self.assertFalse(self.drop(panel.list.viewport(), mime)[0].isAccepted())

    def test_drop_imports_real_image_without_touching_original(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'original.png'
            image = QtGui.QImage(80, 120, QtGui.QImage.Format.Format_ARGB32)
            image.fill(QtGui.QColor('#23aabb')); self.assertTrue(image.save(str(path)))
            before = path.read_bytes()
            panel, _ = self.panel(str(Path(root)/'project'))
            mime = QtCore.QMimeData(); mime.setUrls([QtCore.QUrl.fromLocalFile(str(path))])
            events = self.drop(panel.list.viewport(), mime)
            self.assertTrue(events[-1].isAccepted())
            from PyQt6.QtTest import QTest
            for _ in range(1000):
                if panel.worker is None: break
                QTest.qWait(10)
            self.assertIsNone(panel.worker)
            layers = [layer for layer in panel.values() if layer['kind'] == 'image']
            self.assertEqual(len(layers), 1)
            self.assertNotEqual(Path(layers[0]['path']), path)
            self.assertTrue(Path(layers[0]['path']).is_file())
            self.assertEqual(path.read_bytes(), before)

    def test_dialog_drop_order_reaches_preview_save_undo_and_redo(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store
        with tempfile.TemporaryDirectory() as root, \
             patch.object(BatchTextVideoDialog, 'request_background'), \
             patch.object(BatchTextVideoDialog, '_start_overlay'):
            dialog = BatchTextVideoDialog(store=Store(root))
            self.addCleanup(dialog.deleteLater)
            dialog.layer_panel.add_text()
            self.assertTrue(dialog.persist())
            panel = dialog.layer_panel
            dialog.tabs.setCurrentIndex(dialog.layer_tab_index)
            dialog.show(); self.app.processEvents()
            original = panel.values()
            first = panel.list.item(0)
            panel.list.setCurrentItem(first)
            mime = panel.list.mimeData([first])
            position = panel.list.visualItemRect(panel.list.item(panel.list.count()-1)).bottomLeft()+QtCore.QPoint(35, 0)
            self.assertTrue(self.drop(panel.list.viewport(), mime, position, QtCore.Qt.DropAction.MoveAction)[-1].isAccepted())
            changed = panel.values()
            self.assertNotEqual(changed, original)
            self.assertEqual(dialog.preview.layers, changed)
            self.assertTrue(dialog.persist())
            self.assertEqual(dialog.store.load()['settings']['layers'], changed)
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(panel.values(), original)
            self.assertTrue(dialog.redo_edit())
            self.assertEqual(panel.values(), changed)
            dialog.close()
