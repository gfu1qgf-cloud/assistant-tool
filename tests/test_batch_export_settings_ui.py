import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.store import Store
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class ExportSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_global_settings_persist_and_remain_editable_for_future_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
                dialog=BatchTextVideoDialog(store=store)
                try:
                    self.assertEqual(dialog.controls['video_encoder'].currentData(),'cpu')
                    self.assertEqual(dialog.controls['export_workers'].value(),2)
                    self.assertEqual(dialog.controls['export_workers'].maximum(),64)
                    quality=dialog.controls['export_quality']
                    self.assertEqual(quality.currentData(),'high')
                    self.assertEqual([quality.itemData(i) for i in range(quality.count())],['high','medium','low'])
                    quality.setCurrentIndex(quality.findData('medium'))
                    combo=dialog.controls['video_encoder']
                    combo.setCurrentIndex(combo.findData('nvenc'))
                    dialog.controls['export_workers'].setValue(4)
                    dialog.controls['encoder_fallback'].setChecked(False)
                    dialog.edit_timer.stop()
                    self.assertTrue(dialog.persist())
                    saved=store.load()['settings']
                    self.assertEqual(saved['video_encoder'],'nvenc')
                    self.assertEqual(saved['export_workers'],4)
                    self.assertFalse(saved['encoder_fallback'])
                    self.assertEqual(saved['export_quality'],'medium')
                    dialog._set_work_controls(True)
                    self.assertTrue(combo.isEnabled())
                    self.assertTrue(quality.isEnabled())
                    dialog._set_work_controls(False)
                    self.assertTrue(combo.isEnabled())
                    self.assertTrue(quality.isEnabled())
                    dialog._batch_percentages={'a':0,'b':0}
                    dialog._progress('b',100)
                    self.assertEqual(dialog.progress_bar.value(),50)
                    dialog._progress('a',40)
                    self.assertEqual(dialog.progress_bar.value(),70)
                finally:
                    dialog.edit_timer.stop();dialog.close();dialog.deleteLater()


if __name__=='__main__':unittest.main()
