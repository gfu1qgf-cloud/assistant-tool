import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets
from app_plugins.builtin.smart_video_editor.timeline_review import SmartVideoTimelineReview


class SmartVideoLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_full_task_script_receives_extra_height(self):
        review = SmartVideoTimelineReview({"settings": {}, "tasks": []})
        try:
            review.resize(1500, 1050)
            review.show()
            self.app.processEvents()
            self.assertGreaterEqual(review.task_script_text.height(), 110)
            self.assertGreater(review.comparison_panel.height(), 190)
            self.assertLess(review.subtitle_settings_panel.height(), 160)
        finally:
            review.close_player()
            review.close()


if __name__ == "__main__":
    unittest.main()
