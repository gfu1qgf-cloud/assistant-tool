"""Real Qt browser sorting must never change the target of account actions."""
import copy
import time
import unittest
from unittest.mock import MagicMock, patch

from qt_compat import QtCore, QtTest, QtWidgets
import test_flow_setup as fixtures
from app_plugins.builtin.flow_assistant.browser_table import Column as C, HEADERS


class BrowserTableTests(unittest.TestCase):
    setUpClass = fixtures.SetupTests.__dict__['setUpClass']
    setUp = fixtures.SetupTests.setUp
    ui = fixtures.SetupTests.ui

    def table(self):
        dialog, launcher, commands = self.ui()
        launcher.profiles = [{'directory': f'Profile {number}', 'name': f'任务 {number} · 账号'}
                             for number in (10, 2, 30, 4)]
        clients = [{'id': f'browser-{number}', 'profile': f'Profile {number}',
                    'account': f'user{number}@example.test', 'credits': credits,
                    'credits_at': time.time(), 'project': fixtures.PROJECT,
                    'saved_project': fixtures.PROJECT, 'page_ready': True}
                   for number, credits in ((10, 1038), (2, 26), (30, None), (4, 5))]
        dialog.state = {'clients': clients, 'preferences': {'profile_projects': {
            client['profile']: fixtures.PROJECT for client in clients}}}
        dialog.update_browsers()
        return dialog, launcher, commands

    def profiles(self, dialog):
        return [dialog.browsers.item(row, C.PROFILE).text() for row in range(dialog.browsers.rowCount())]

    def row(self, dialog, profile):
        return self.profiles(dialog).index(profile)

    def test_names_come_from_launcher_and_missing_names_do_not_invent_accounts(self):
        dialog, launcher, _ = self.table()
        self.assertEqual([dialog.browsers.horizontalHeaderItem(i).text() for i in range(len(HEADERS))], list(HEADERS))
        self.assertEqual(dialog.browsers.item(0, C.NAME).text(), '任务 2 · 账号')
        launcher.profiles[1]['name'] = '改名后的配置'
        dialog.state['clients'] = []
        dialog.update_browsers()
        row = self.row(dialog, 'Profile 2')
        self.assertEqual(dialog.browsers.item(row, C.NAME).text(), '改名后的配置')
        self.assertEqual(dialog.browsers.item(row, C.ACCOUNT).text(), '未连接')
        launcher.profiles = []
        dialog.update_browsers()
        self.assertEqual(dialog.browsers.item(row, C.NAME).text(), '未读取到名称')

    def test_header_clicks_toggle_natural_profile_and_name_order(self):
        dialog, _, commands = self.table()
        dialog.tabs.setCurrentWidget(dialog.browser_page)
        dialog.show(); self.app.processEvents()
        self.assertEqual(self.profiles(dialog), ['Profile 2', 'Profile 4', 'Profile 10', 'Profile 30'])
        header = dialog.browsers.horizontalHeader()
        point = QtCore.QPoint(header.sectionViewportPosition(C.PROFILE) + 30, header.height() // 2)
        QtTest.QTest.mouseClick(header.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(self.profiles(dialog), ['Profile 30', 'Profile 10', 'Profile 4', 'Profile 2'])
        QtTest.QTest.mouseClick(header.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(self.profiles(dialog), ['Profile 2', 'Profile 4', 'Profile 10', 'Profile 30'])
        dialog.browsers.sortItems(C.NAME, QtCore.Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.profiles(dialog), ['Profile 2', 'Profile 4', 'Profile 10', 'Profile 30'])
        self.assertFalse(commands, 'display sorting must not send scheduler commands')

    def test_credits_sort_numerically_with_unknown_distinct_from_real_fifty(self):
        dialog, _, _ = self.table()
        dialog.browsers.sortItems(C.CREDITS, QtCore.Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.profiles(dialog), ['Profile 4', 'Profile 2', 'Profile 10', 'Profile 30'])
        dialog.state['clients'][1].update(credits=100, credits_estimated=True,
                                         credit_check={'state': 'failed', 'message': '旧检测失败', 'at': time.time()})
        dialog.update_browsers()
        self.assertEqual(self.profiles(dialog), ['Profile 4', 'Profile 2', 'Profile 10', 'Profile 30'])
        self.assertIn('100（估算）', dialog.browsers.item(1, C.CREDITS).text())
        dialog.browsers.sortItems(C.CREDITS, QtCore.Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.profiles(dialog), ['Profile 30', 'Profile 10', 'Profile 2', 'Profile 4'])

    def test_high_credit_marker_disappears_after_balance_drops_below_one_hundred(self):
        dialog, _, _ = self.table()
        rich = dialog.state['clients'][0]
        self.assertIn('千分账号', dialog.browsers.item(self.row(dialog, rich['profile']), C.CREDITS).text())
        from app_plugins.builtin.flow_assistant.high_credit import with_marker
        dialog.state['preferences'] = with_marker(dialog.state['preferences'], rich)
        rich['credits'] = 38
        dialog.update_browsers()
        row = self.row(dialog, rich['profile'])
        self.assertNotIn('千分账号', dialog.browsers.item(row, C.CREDITS).text())
        self.assertNotEqual(dialog.browsers.item(row, C.STATE).text(), '千分账号备用')

    def test_refresh_after_any_sort_keeps_all_cells_editors_and_selection_on_same_profile(self):
        dialog, launcher, commands = self.table()
        combo = dialog.lane_edits[('profile', 'Profile 2')]
        combo.setCurrentIndex(combo.findData(6))
        dialog.browsers.selectRow(self.row(dialog, 'Profile 2'))
        state = copy.deepcopy(dialog.state)
        for column in C:
            dialog.browsers.sortItems(column, QtCore.Qt.SortOrder.DescendingOrder)
            state['clients'].reverse()
            dialog.state = copy.deepcopy(state)
            dialog.update_browsers()
            self.assertEqual([value['profile'] for value in dialog.browser_selections()], ['Profile 2'])
            self.assertEqual(dialog.browsers.horizontalHeader().sortIndicatorSection(), column)
            self.assertEqual(dialog.browsers.horizontalHeader().sortIndicatorOrder(), QtCore.Qt.SortOrder.DescendingOrder)
            for row, profile in enumerate(self.profiles(dialog)):
                item = dialog.browsers.item(row, C.PROFILE)
                expected = next(value for value in launcher.profiles if value['directory'] == profile)
                self.assertEqual(dialog.browsers.item(row, C.NAME).text(), expected['name'])
                self.assertEqual(dialog.browsers.item(row, C.ACCOUNT).text(), 'user' + profile.split()[-1] + '@example.test')
                self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole)['profile'], profile)
                self.assertIs(dialog.browsers.cellWidget(row, C.LANE), dialog.lane_edits[('profile', profile)])
            self.assertIs(dialog.lane_edits[('profile', 'Profile 2')], combo)
            self.assertEqual(combo.currentData(), 6)
        self.assertEqual(commands, [{'action': 'lane', 'scope': 'profile', 'key': 'Profile 2', 'duration': 6}])

    def test_changed_credits_move_selected_row_without_changing_detect_open_or_resume_target(self):
        dialog, _, commands = self.table()
        dialog.browsers.sortItems(C.CREDITS, QtCore.Qt.SortOrder.AscendingOrder)
        dialog.browsers.selectRow(self.row(dialog, 'Profile 2'))
        dialog.state['clients'][1]['credits'] = 2000
        dialog.update_browsers()
        self.assertEqual(self.row(dialog, 'Profile 2'), 2)
        dialog.detect_selected_credits()
        self.assertEqual(commands[-1], {'action': 'credits', 'keys': ['profile:Profile 2'], 'manual': True})
        dialog.resume_selected_browsers()
        self.assertEqual(commands[-1], {'action': 'resume_browser', 'keys': ['profile:Profile 2']})
        with patch.object(dialog, 'open_setup') as open_setup:
            dialog.setup.begin = MagicMock()
            dialog.open_selected_browser()
            open_setup.assert_called_once_with('Profile 2')
            dialog.setup.begin.assert_called_once_with()

    def test_insert_remove_reconnect_and_group_reorder_preserve_existing_editor(self):
        dialog, _, _ = self.table()
        dialog.browsers.sortItems(C.CREDITS, QtCore.Qt.SortOrder.DescendingOrder)
        row = self.row(dialog, 'Profile 2')
        dialog.browsers.setCurrentCell(row, C.NAME); dialog.browsers.selectRow(row)
        combo = dialog.lane_edits[('profile', 'Profile 2')]
        dialog.state['clients'] = [dialog.state['clients'][1], {
            'id': 'browser-new', 'profile': 'Profile 1', 'credits': 12}]
        dialog.state['clients'][0]['id'] = 'browser-2-reconnected'
        dialog.state['preferences']['profile_projects'] = {'Profile 2': fixtures.PROJECT}
        dialog.update_browsers()
        self.assertEqual(set(self.profiles(dialog)), {'Profile 1', 'Profile 2'})
        self.assertIs(dialog.browsers.cellWidget(self.row(dialog, 'Profile 2'), C.LANE), combo)
        self.assertEqual(dialog.browser_selections()[0]['browser'], 'browser-2-reconnected')
        self.assertEqual(dialog.browsers.currentItem().row(), self.row(dialog, 'Profile 2'))
        self.assertNotIn(('profile', 'Profile 10'), dialog.lane_edits)

    def test_lane_sort_uses_seconds_and_edit_targets_the_sorted_row(self):
        dialog, _, commands = self.table()
        dialog.state['preferences'].update(profile_lanes={'Profile 2': 10, 'Profile 4': 6, 'Profile 10': 8})
        dialog.update_browsers()
        dialog.browsers.sortItems(C.LANE, QtCore.Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.profiles(dialog), ['Profile 4', 'Profile 10', 'Profile 2', 'Profile 30'])
        combo = dialog.browsers.cellWidget(0, C.LANE)
        combo.setCurrentIndex(combo.findData(10))
        self.assertEqual(commands[-1], {'action': 'lane', 'scope': 'profile', 'key': 'Profile 4', 'duration': 10})

    def test_context_menu_after_sort_targets_clicked_profile_not_previous_selection(self):
        dialog, _, commands = self.table()
        dialog.browsers.sortItems(C.NAME, QtCore.Qt.SortOrder.DescendingOrder)
        dialog.browsers.selectRow(self.row(dialog, 'Profile 10'))
        def choose(menu, *_args):
            menu.actions()[0].trigger()
        point = dialog.browsers.visualItemRect(dialog.browsers.item(self.row(dialog, 'Profile 4'), C.PROFILE)).center()
        with patch.object(QtWidgets.QMenu, 'exec', choose):
            dialog.browser_menu(point)
        self.assertEqual(commands[-1]['keys'], ['profile:Profile 4'])


if __name__ == '__main__': unittest.main()
