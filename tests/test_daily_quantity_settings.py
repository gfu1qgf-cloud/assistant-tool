import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PYUI.main_pyui import MainDialog
from PYUI.main_setting_pyui import MainSettingDialog
from globalValue import globalValue


class DailyQuantitySettingsTests(unittest.TestCase):
    def test_only_sheet_link_change_does_not_reapply_unrelated_qt_services(self):
        previous = {
            "daily_quantity_sheet_url": "https://docs.google.com/spreadsheets/d/old/edit",
            "smart_video_editor": {"whisper_model_size": "large-v3"},
        }
        updated = dict(previous, daily_quantity_sheet_url=(
            "https://docs.google.com/spreadsheets/d/new/edit"
        ))
        window = mock.Mock()
        window.load_config.side_effect = [previous, updated]
        with mock.patch.object(
            MainSettingDialog, "get_settings", return_value=updated
        ) as editor, mock.patch.object(
            globalValue, "loaded_whisper_model_name", return_value="large-v3"
        ):
            MainDialog.openSettings(window, focus_daily_quantity=True)
        editor.assert_called_once_with(
            window, plugin_host=window.plugin_host, focus_daily_quantity=True
        )
        window.plugin_host.apply_settings.assert_not_called()
        window.registerLoadTaskGlobalHotkey.assert_not_called()
        window.restartFlowParameterGuard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
