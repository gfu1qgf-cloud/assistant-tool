"""Retired duplicate choices must disappear before Qt's deferred deletion."""
import os
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.smart_video_editor.timeline_review import SmartVideoTimelineReview


class DuplicateDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.review = SmartVideoTimelineReview({"tasks": [], "settings": {}})
        self.review.bundle["tasks"] = [{"clips": [
            {"file_name": f"[{index:02d}] Agent_0220535ac940_c3078edb1bc1_任务103_第04段_版本{index}.mp4",
             "original_duration": 10., "similarity": .93,
             "duplicate_group_id": "take-4", "duplicate_selected": index == 0}
            for index in range(2)]}]
        # Display the real panel alone, without opening media or a task report.
        self.panel = self.review.duplicate_group_panel
        self.panel.setParent(None)
        self.panel.resize(900, 130)
        self.review._update_duplicate_group_controls(0)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.review.close_player()
        self.review.close()
        self.review.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_refresh_hides_old_choices_before_deferred_delete(self):
        old = list(self.review.duplicate_button_group.buttons())
        self.assertTrue(all(button.isVisible() for button in old))
        self.review._update_duplicate_group_controls(1)
        # No processEvents/sendPostedEvents here: this is the faulty interval.
        self.assertTrue(all(button.isHidden() for button in old))
        self.assertEqual(len(self.review.duplicate_button_group.buttons()), 2)

    def test_retired_choice_cannot_trigger_selection(self):
        old = self.review.duplicate_button_group.button(1)
        self.review._update_duplicate_group_controls(0)
        with patch.object(self.review, "_choose_duplicate_clip") as choose:
            old.click()
            choose.assert_not_called()
            self.review.duplicate_button_group.button(1).click()
            choose.assert_called_once_with(1)

    def test_repeated_refresh_has_no_visible_retired_rows(self):
        retired = []
        for index in range(25):
            retired.extend(self.review.duplicate_button_group.buttons())
            self.review._update_duplicate_group_controls(index % 2)
        self.assertTrue(all(button.isHidden() for button in retired))
        self.app.processEvents()
        current = self.review.duplicate_button_group.buttons()
        visible = [button for button in self.panel.findChildren(QtWidgets.QRadioButton)
                   if button.isVisible()]
        self.assertEqual(set(visible), set(current))
        self.assertFalse(current[0].geometry().intersects(current[1].geometry()))
        screenshot = os.environ.get("LZX_DUPLICATE_SCREENSHOT")
        if screenshot:
            self.assertTrue(self.panel.grab().save(screenshot))

    def test_group_hidden_without_leaving_visible_candidates(self):
        old = self.review.duplicate_button_group.buttons()
        self.review.bundle["tasks"][0]["clips"][0]["duplicate_group_id"] = "other"
        self.review._update_duplicate_group_controls(0)
        self.assertTrue(self.panel.isHidden())
        self.assertTrue(all(button.isHidden() for button in old))

    def test_clearing_twice_is_safe(self):
        self.review._clear_duplicate_choices()
        self.review._clear_duplicate_choices()
        self.assertEqual(self.review.duplicate_choices_layout.count(), 0)
        self.assertEqual(self.review.duplicate_button_group.buttons(), [])

    def test_many_candidates_keep_non_overlapping_rows_in_inspector_scroll(self):
        clips = self.review.bundle["tasks"][0]["clips"]
        clips.extend(dict(clips[1], file_name=f"版本{index}_任务103_第04段.mp4")
                     for index in range(2, 14))
        self.review._update_duplicate_group_controls(0)
        self.panel.adjustSize()
        self.app.processEvents()
        buttons = self.review.duplicate_button_group.buttons()
        self.assertEqual(len(buttons), 14)
        for first, second in zip(buttons, buttons[1:]):
            self.assertGreaterEqual(first.height(), first.minimumSizeHint().height())
            self.assertLessEqual(first.geometry().bottom(), second.geometry().top())
        self.assertTrue(self.panel.rect().contains(buttons[-1].mapTo(self.panel, buttons[-1].rect().bottomRight())))
