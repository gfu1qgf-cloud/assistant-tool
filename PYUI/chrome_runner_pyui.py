import copy
import json
import os
import random
import subprocess
import time
import uuid

from qt_compat import QtWidgets, QtGui
from qt_compat import Qt
from qt_compat import QListWidgetItem, QMessageBox, QInputDialog

from QTUI.chrome_runner_ui import Ui_ChromeRunnerDialog

class ChromeRunnerDialog(QtWidgets.QDialog,Ui_ChromeRunnerDialog):
    iterator_config_key = 'chrome_profile_iterator'
    websites_config_key = 'chrome_preset_websites'
    profile_groups_config_key = 'chrome_profile_groups'
    default_websites = []

    def __init__(self, parent=None, config_path='config.json'):
        super(ChromeRunnerDialog, self).__init__(parent)
        self.setupUi(self)

        self.profiles = []
        self.config_path = os.path.abspath(config_path)
        self.next_profile_index = 0
        self.iterator_profile_directories = []
        self._saved_next_profile_directory = None
        self.active_iterator_group_id = None
        self.profile_groups = []
        self.selected_group_id = None
        self._build_profile_group_controls()
        self.chrome_path = self.find_chrome_path()
        self.user_data_dir = self.get_chrome_user_data_dir()
        self.load_iterator_state()
        self.load_profile_groups()
        self.load_websites()

        self.run_btn.clicked.connect(self.launch_selected_profiles)
        self.run_next_btn.clicked.connect(self.start_selected_iterator)
        self.reset_iterator_btn.clicked.connect(self.reset_iterator)
        self.add_web_btn.clicked.connect(self.add_website)
        self.edit_web_btn.clicked.connect(self.edit_website)
        self.delete_web_btn.clicked.connect(self.delete_websites)
        self.web_list_widget.itemDoubleClicked.connect(self.edit_website)

        self.load_profiles()
        self.refresh_btn.clicked.connect(self.load_profiles)

    def _build_profile_group_controls(self):
        group_picker_layout = QtWidgets.QHBoxLayout()
        group_picker_layout.addWidget(QtWidgets.QLabel('配置分组：', self))
        self.profile_group_combo = QtWidgets.QComboBox(self)
        self.profile_group_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.profile_group_combo.setMinimumContentsLength(12)
        group_picker_layout.addWidget(self.profile_group_combo, 1)
        self.add_profile_group_btn = QtWidgets.QPushButton('新建', self)
        self.rename_profile_group_btn = QtWidgets.QPushButton('改名', self)
        self.delete_profile_group_btn = QtWidgets.QPushButton('删除', self)
        group_picker_layout.addWidget(self.add_profile_group_btn)
        group_picker_layout.addWidget(self.rename_profile_group_btn)
        group_picker_layout.addWidget(self.delete_profile_group_btn)

        group_action_layout = QtWidgets.QHBoxLayout()
        self.save_profile_group_members_btn = QtWidgets.QPushButton(
            '管理成员 / 子分组…',
            self,
        )
        self.start_profile_group_iterator_btn = QtWidgets.QPushButton(
            '按此分组迭代启动',
            self,
        )
        self.save_profile_group_members_btn.setToolTip(
            '管理分组直接包含的 Chrome Profile，也可以加入其他分组组成组合分组'
        )
        self.start_profile_group_iterator_btn.setToolTip(
            '按直接成员和子分组的顺序展开、自动去重，并从保存位置继续迭代'
        )
        group_action_layout.addWidget(self.save_profile_group_members_btn)
        group_action_layout.addWidget(self.start_profile_group_iterator_btn)

        self.verticalLayout.insertLayout(1, group_picker_layout)
        self.verticalLayout.insertLayout(2, group_action_layout)

        self.profile_group_combo.currentIndexChanged.connect(
            self.on_profile_group_changed
        )
        self.add_profile_group_btn.clicked.connect(self.add_profile_group)
        self.rename_profile_group_btn.clicked.connect(self.rename_profile_group)
        self.delete_profile_group_btn.clicked.connect(self.delete_profile_group)
        self.save_profile_group_members_btn.clicked.connect(
            self.edit_profile_group_members
        )
        self.start_profile_group_iterator_btn.clicked.connect(
            self.start_group_iterator
        )


    def find_chrome_path(self):
        """查找 Chrome 可执行文件路径"""
        possible_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]

        for path in possible_paths:
            if os.path.exists(path):
                return path
        return None

    def get_chrome_user_data_dir(self):
        """获取 Chrome 用户数据目录"""
        return os.path.join(os.environ['LOCALAPPDATA'], 'Google', 'Chrome', 'User Data')

    def get_profile_sort_key(self, profile_dir):
        """Local State 不可用时，按 Chrome Profile 目录顺序排序。"""
        if profile_dir == 'Default':
            return (0, 0)
        if profile_dir.startswith('Profile '):
            try:
                return (1, int(profile_dir.split(' ')[1]))
            except (ValueError, IndexError):
                pass
        return (2, profile_dir.lower())

    def load_profiles(self):
        """加载所有 Chrome profiles"""
        self.chrome_list_widget.clear()
        self.profiles = []

        if not os.path.exists(self.user_data_dir):
            self.status_label.setText('错误: Chrome 用户数据目录不存在')
            return

        # 读取 Local State 文件获取 profile 信息
        local_state_path = os.path.join(self.user_data_dir, 'Local State')

        if os.path.exists(local_state_path):
            try:
                with open(local_state_path, 'r', encoding='utf-8') as f:
                    local_state = json.load(f)

                profile_info = local_state.get('profile', {}).get('info_cache', {})

                for profile_dir, info in profile_info.items():
                    profile_name = info.get('name', profile_dir)
                    profile_path = os.path.join(self.user_data_dir, profile_dir)

                    if os.path.exists(profile_path):
                        self.profiles.append({
                            'name': profile_name,
                            'directory': profile_dir,
                            'path': profile_path
                        })

                # 按 Profile 名称排序（字母顺序）
                self.profiles.sort(key=lambda p: p['name'].lower())

                # 添加到列表
                for profile in self.profiles:
                    item = QListWidgetItem(f"{profile['name']} ({profile['directory']})")
                    item.setData(Qt.UserRole, profile['directory'])
                    self.chrome_list_widget.addItem(item)

                self.populate_profile_group_combo()
                self.restore_iterator_position()
                self.update_iterator_status(f'已加载 {len(self.profiles)} 个 profiles')

            except Exception as e:
                self.status_label.setText(f'错误: {str(e)}')
        else:
            # 如果 Local State 不存在，尝试扫描目录
            self.scan_profile_directories()

    def scan_profile_directories(self):
        """扫描 Chrome 用户数据目录查找 profiles"""
        for item in os.listdir(self.user_data_dir):
            item_path = os.path.join(self.user_data_dir, item)

            # 查找 Default 和 Profile * 目录
            if os.path.isdir(item_path) and (item == 'Default' or item.startswith('Profile ')):
                self.profiles.append({
                    'name': item,
                    'directory': item,
                    'path': item_path
                })

        # 按 Profile ID 排序
        self.profiles.sort(key=lambda p: self.get_profile_sort_key(p['directory']))

        # 添加到列表
        for profile in self.profiles:
            list_item = QListWidgetItem(profile['name'])
            list_item.setData(Qt.UserRole, profile['directory'])
            self.chrome_list_widget.addItem(list_item)

        self.populate_profile_group_combo()
        self.restore_iterator_position()
        self.update_iterator_status(f'已扫描 {len(self.profiles)} 个 profiles')

    def read_config(self):
        if not os.path.exists(self.config_path):
            return {}
        try:
            with open(self.config_path, 'r', encoding='utf-8') as config_file:
                config = json.load(config_file)
                return config if isinstance(config, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def write_config(self, config):
        """原子写入配置，避免中途退出产生半个 JSON 文件。"""
        temp_path = f'{self.config_path}.chrome-runner.tmp'
        try:
            with open(temp_path, 'w', encoding='utf-8') as config_file:
                json.dump(config, config_file, indent=4, ensure_ascii=False)
            os.replace(temp_path, self.config_path)
            return True
        except OSError as error:
            print(f'保存 Chrome 配置失败: {error}')
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            return False

    @staticmethod
    def normalize_profile_group_name(name):
        return ' '.join(str(name or '').strip().split())

    def get_profile_group(self, group_id=None):
        group_id = group_id or self.selected_group_id
        return next(
            (
                group
                for group in self.profile_groups
                if group.get('id') == group_id
            ),
            None,
        )

    def group_contains_group(self, group_id, target_group_id, visited=None):
        """Return whether one group recursively contains another group."""
        group_id = str(group_id or '')
        target_group_id = str(target_group_id or '')
        if not group_id or not target_group_id:
            return False
        if group_id == target_group_id:
            return True
        visited = set(visited or ())
        if group_id in visited:
            return False
        visited.add(group_id)
        group = self.get_profile_group(group_id)
        if group is None:
            return False
        return any(
            self.group_contains_group(child_id, target_group_id, visited)
            for child_id in group.get('child_group_ids', [])
        )

    def would_create_group_cycle(self, parent_group_id, child_group_id):
        parent_group_id = str(parent_group_id or '')
        child_group_id = str(child_group_id or '')
        return (
            not parent_group_id
            or not child_group_id
            or parent_group_id == child_group_id
            or self.group_contains_group(child_group_id, parent_group_id)
        )

    def load_profile_groups(self):
        config = self.read_config()
        raw_section = config.get(self.profile_groups_config_key, {})
        if isinstance(raw_section, list):
            raw_groups = raw_section
            selected_group_id = None
        elif isinstance(raw_section, dict):
            raw_groups = raw_section.get('groups', [])
            selected_group_id = raw_section.get('selected_group_id')
        else:
            raw_groups = []
            selected_group_id = None
        if not isinstance(raw_groups, list):
            raw_groups = []

        groups = []
        raw_children_by_id = {}
        seen_ids = set()
        seen_names = set()
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                continue
            name = self.normalize_profile_group_name(raw_group.get('name'))
            if not name or name.casefold() in seen_names:
                continue
            group_id = str(raw_group.get('id') or uuid.uuid4().hex)
            if group_id in seen_ids:
                group_id = uuid.uuid4().hex
            raw_directories = raw_group.get('profile_directories', [])
            if not isinstance(raw_directories, list):
                raw_directories = []
            directories = list(dict.fromkeys(
                str(directory).strip()
                for directory in raw_directories
                if str(directory).strip()
            ))
            next_directory = str(
                raw_group.get('next_profile_directory') or ''
            ).strip()
            if not next_directory and group_id == self.active_iterator_group_id:
                # 从旧版的“全局分组进度”无感迁移为每组独立进度。
                candidate = str(self._saved_next_profile_directory or '').strip()
                if candidate in directories:
                    next_directory = candidate
            # A combination group's next Profile may come from a child group,
            # so it must not be validated against direct members only.
            if not next_directory:
                next_directory = directories[0] if directories else ''
            raw_children = raw_group.get('child_group_ids', [])
            if not isinstance(raw_children, list):
                raw_children = []
            raw_children_by_id[group_id] = list(dict.fromkeys(
                str(child_id).strip()
                for child_id in raw_children
                if str(child_id).strip()
            ))
            group = dict(raw_group)
            group.update({
                'id': group_id,
                'name': name,
                'profile_directories': directories,
                'child_group_ids': [],
                'next_profile_directory': next_directory,
            })
            groups.append(group)
            seen_ids.add(group_id)
            seen_names.add(name.casefold())

        # Rebuild references in file order.  Invalid/self references are
        # discarded, and only the edge that would close a cycle is rejected.
        self.profile_groups = groups
        valid_group_ids = {group['id'] for group in groups}
        for group in groups:
            for child_id in raw_children_by_id.get(group['id'], []):
                if child_id not in valid_group_ids:
                    continue
                if self.would_create_group_cycle(group['id'], child_id):
                    continue
                group['child_group_ids'].append(child_id)

        selected_group_id = (
            str(selected_group_id) if selected_group_id else None
        )
        if selected_group_id not in seen_ids:
            selected_group_id = None
        self.selected_group_id = selected_group_id
        if self.active_iterator_group_id not in seen_ids:
            self.active_iterator_group_id = None
        self.populate_profile_group_combo()

    def profile_groups_config(self):
        return {
            'version': 3,
            'selected_group_id': self.selected_group_id,
            'groups': self.profile_groups,
        }

    def save_profile_groups(self, show_error=True):
        config = self.read_config()
        config[self.profile_groups_config_key] = self.profile_groups_config()
        if self.write_config(config):
            return True
        if show_error:
            QMessageBox.critical(self, '错误', 'Chrome 配置分组写入配置文件失败！')
        return False

    def populate_profile_group_combo(self):
        self.profile_group_combo.blockSignals(True)
        self.profile_group_combo.clear()
        self.profile_group_combo.addItem('不使用分组（手动选择）', None)
        selected_index = 0
        for group in self.profile_groups:
            member_count = len(self.get_group_profile_directories(group['id']))
            child_count = len(group.get('child_group_ids', []))
            child_text = f" / {child_count} 子组" if child_count else ''
            self.profile_group_combo.addItem(
                f"{group['name']}（{member_count} Profile{child_text}）",
                group['id'],
            )
            if group['id'] == self.selected_group_id:
                selected_index = self.profile_group_combo.count() - 1
        self.profile_group_combo.setCurrentIndex(selected_index)
        self.profile_group_combo.blockSignals(False)
        self.select_current_group_members()
        self.update_group_controls()

    def update_group_controls(self):
        has_group = self.get_profile_group() is not None
        self.rename_profile_group_btn.setEnabled(has_group)
        self.delete_profile_group_btn.setEnabled(has_group)
        self.save_profile_group_members_btn.setEnabled(has_group)
        self.start_profile_group_iterator_btn.setEnabled(has_group)

    def get_group_profile_directories(self, group_id=None):
        return self._resolve_group_profile_directories(group_id, set())

    def _resolve_group_profile_directories(self, group_id, ancestry):
        group = self.get_profile_group(group_id)
        if group is None:
            return []
        group_id = str(group.get('id') or '')
        if group_id in ancestry:
            return []
        ancestry = set(ancestry)
        ancestry.add(group_id)

        direct_members = set(group.get('profile_directories', []))
        directories = [
            profile['directory']
            for profile in self.profiles
            if profile['directory'] in direct_members
        ]
        seen = set(directories)
        for child_id in group.get('child_group_ids', []):
            for directory in self._resolve_group_profile_directories(
                child_id, ancestry
            ):
                if directory not in seen:
                    directories.append(directory)
                    seen.add(directory)
        return directories

    def select_current_group_members(self):
        group = self.get_profile_group()
        if group is None or not self.profiles:
            return
        member_set = set(group.get('profile_directories', []))
        self.chrome_list_widget.clearSelection()
        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            item.setSelected(item.data(Qt.UserRole) in member_set)

    def on_profile_group_changed(self):
        group_id = self.profile_group_combo.currentData()
        self.selected_group_id = str(group_id) if group_id else None
        self.select_current_group_members()
        self.update_group_controls()
        self.save_profile_groups(show_error=False)
        group = self.get_profile_group()
        if group is not None:
            self.update_iterator_status(self.group_progress_text(group, '已显示'))

    def add_profile_group(self):
        name, accepted = QInputDialog.getText(
            self,
            '新建 Chrome 配置分组',
            '分组名称：',
        )
        if not accepted:
            return
        name = self.normalize_profile_group_name(name)
        if not name:
            QMessageBox.warning(self, '警告', '分组名称不能为空！')
            return
        if any(group['name'].casefold() == name.casefold() for group in self.profile_groups):
            QMessageBox.information(self, '提示', '已经存在同名分组。')
            return
        group = {
            'id': uuid.uuid4().hex,
            'name': name,
            'profile_directories': self.get_selected_profile_directories(),
            'child_group_ids': [],
            'next_profile_directory': '',
        }
        if group['profile_directories']:
            group['next_profile_directory'] = group['profile_directories'][0]
        old_selected_group_id = self.selected_group_id
        self.profile_groups.append(group)
        self.selected_group_id = group['id']
        if not self.save_profile_groups():
            self.profile_groups.pop()
            self.selected_group_id = old_selected_group_id
            return
        self.populate_profile_group_combo()
        self.update_iterator_status(
            f"已新建分组“{name}”，包含 {len(group['profile_directories'])} 个 Profile"
        )

    def rename_profile_group(self):
        group = self.get_profile_group()
        if group is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            '修改 Chrome 配置分组名称',
            '分组名称：',
            QtWidgets.QLineEdit.Normal,
            group['name'],
        )
        if not accepted:
            return
        name = self.normalize_profile_group_name(name)
        if not name:
            QMessageBox.warning(self, '警告', '分组名称不能为空！')
            return
        if any(
            item['id'] != group['id'] and item['name'].casefold() == name.casefold()
            for item in self.profile_groups
        ):
            QMessageBox.information(self, '提示', '已经存在同名分组。')
            return
        old_name = group['name']
        group['name'] = name
        if not self.save_profile_groups():
            group['name'] = old_name
            return
        self.populate_profile_group_combo()
        self.update_iterator_status(f"分组已改名为“{name}”")

    def delete_profile_group(self):
        group = self.get_profile_group()
        if group is None:
            return
        answer = QMessageBox.question(
            self,
            '删除 Chrome 配置分组',
            f"确定删除分组“{group['name']}”吗？\n"
            '只删除分组记录，不会删除 Chrome 配置。',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        old_groups = copy.deepcopy(self.profile_groups)
        old_selected_group_id = self.selected_group_id
        old_active_group_id = self.active_iterator_group_id
        self.profile_groups = [
            item for item in self.profile_groups if item['id'] != group['id']
        ]
        for remaining_group in self.profile_groups:
            remaining_group['child_group_ids'] = [
                child_id
                for child_id in remaining_group.get('child_group_ids', [])
                if child_id != group['id']
            ]
        self.selected_group_id = None
        if self.active_iterator_group_id == group['id']:
            self.active_iterator_group_id = None
        if not self.save_profile_groups():
            self.profile_groups = old_groups
            self.selected_group_id = old_selected_group_id
            self.active_iterator_group_id = old_active_group_id
            return
        self.refresh_active_group_iterator()
        self.populate_profile_group_combo()
        self.update_iterator_status(f"已删除分组“{group['name']}”")

    def update_profile_group_members(
        self,
        group,
        selected_directories,
        child_group_ids=None,
    ):
        """Update direct Profiles/subgroups while preserving this cursor."""
        if group is None:
            return False
        available_order = [profile['directory'] for profile in self.profiles]
        selected_set = {
            str(directory).strip()
            for directory in selected_directories
            if str(directory).strip()
        }
        selected_directories = [
            directory for directory in available_order if directory in selected_set
        ]
        if child_group_ids is None:
            child_group_ids = list(group.get('child_group_ids', []))
        valid_ids = {item['id'] for item in self.profile_groups}
        normalized_child_ids = []
        for child_id in child_group_ids:
            child_id = str(child_id or '').strip()
            if not child_id or child_id not in valid_ids:
                continue
            if child_id in normalized_child_ids:
                continue
            if self.would_create_group_cycle(group['id'], child_id):
                self.update_iterator_status('不能保存：子分组之间形成了循环引用')
                return False
            normalized_child_ids.append(child_id)

        old_directories = list(group.get('profile_directories', []))
        old_child_group_ids = list(group.get('child_group_ids', []))
        old_next_directory = str(group.get('next_profile_directory') or '')
        group['profile_directories'] = selected_directories
        group['child_group_ids'] = normalized_child_ids
        effective_directories = self.get_group_profile_directories(group['id'])
        if old_next_directory in effective_directories:
            group['next_profile_directory'] = old_next_directory
        else:
            group['next_profile_directory'] = (
                effective_directories[0] if effective_directories else ''
            )
        if not self.save_profile_groups():
            group['profile_directories'] = old_directories
            group['child_group_ids'] = old_child_group_ids
            group['next_profile_directory'] = old_next_directory
            return False
        self.refresh_active_group_iterator()
        self.populate_profile_group_combo()
        self.update_iterator_status(
            self.group_progress_text(group, '已更新')
        )
        return True

    def refresh_active_group_iterator(self):
        """Re-expand an active hierarchy after any member group changes."""
        active_group = self.get_profile_group(self.active_iterator_group_id)
        if active_group is None:
            if self.active_iterator_group_id:
                self.active_iterator_group_id = None
            self.save_iterator_state()
            return
        directories = self.get_group_profile_directories(active_group['id'])
        next_directory = str(
            active_group.get('next_profile_directory')
            or self._saved_next_profile_directory
            or ''
        )
        if next_directory not in directories:
            next_directory = directories[0] if directories else ''
        active_group['next_profile_directory'] = next_directory
        self.iterator_profile_directories = directories
        self.next_profile_index = (
            directories.index(next_directory)
            if next_directory in directories else 0
        )
        self._saved_next_profile_directory = next_directory or None
        self.save_iterator_state()

    def edit_profile_group_members(self):
        group = self.get_profile_group()
        if group is None:
            QMessageBox.warning(self, '警告', '请先选择一个配置分组！')
            return

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"管理分组成员 - {group['name']}")
        dialog.resize(590, 650)
        layout = QtWidgets.QVBoxLayout(dialog)
        hint = QtWidgets.QLabel(
            '可以直接勾选 Profile，也可以加入多个子分组。启动顺序为：'
            '直接 Profile → 各子分组；重复的 Profile 只启动一次。',
            dialog,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        tabs = QtWidgets.QTabWidget(dialog)
        profile_page = QtWidgets.QWidget(tabs)
        profile_layout = QtWidgets.QVBoxLayout(profile_page)
        profile_layout.addWidget(QtWidgets.QLabel(
            '这个分组直接包含的 Chrome Profile：', profile_page
        ))
        member_list = QtWidgets.QListWidget(profile_page)
        member_list.setAlternatingRowColors(True)
        member_set = set(group.get('profile_directories', []))
        for profile in self.profiles:
            item = QListWidgetItem(
                f"{profile['name']}  [{profile['directory']}]",
                member_list,
            )
            item.setData(Qt.UserRole, profile['directory'])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if profile['directory'] in member_set else Qt.Unchecked
            )
        profile_layout.addWidget(member_list, 1)

        quick_layout = QtWidgets.QHBoxLayout()
        select_all_button = QtWidgets.QPushButton('全选', profile_page)
        clear_button = QtWidgets.QPushButton('全不选', profile_page)

        def set_all_members(check_state):
            for index in range(member_list.count()):
                member_list.item(index).setCheckState(check_state)

        select_all_button.clicked.connect(lambda: set_all_members(Qt.Checked))
        clear_button.clicked.connect(lambda: set_all_members(Qt.Unchecked))
        quick_layout.addWidget(select_all_button)
        quick_layout.addWidget(clear_button)
        quick_layout.addStretch(1)
        profile_layout.addLayout(quick_layout)
        tabs.addTab(profile_page, '直接 Profile')

        child_page = QtWidgets.QWidget(tabs)
        child_layout = QtWidgets.QVBoxLayout(child_page)
        child_layout.addWidget(QtWidgets.QLabel(
            '勾选要包含的子分组；可用上移/下移决定各组的迭代顺序：',
            child_page,
        ))
        child_group_list = QtWidgets.QListWidget(child_page)
        child_group_list.setAlternatingRowColors(True)
        selected_child_ids = list(group.get('child_group_ids', []))
        selected_child_set = set(selected_child_ids)
        groups_by_id = {item['id']: item for item in self.profile_groups}
        ordered_group_ids = selected_child_ids + [
            item['id'] for item in self.profile_groups
            if item['id'] not in selected_child_set
        ]
        for candidate_id in ordered_group_ids:
            candidate = groups_by_id.get(candidate_id)
            if candidate is None or candidate_id == group['id']:
                continue
            effective_count = len(
                self.get_group_profile_directories(candidate_id)
            )
            item = QListWidgetItem(
                f"{candidate['name']}（{effective_count} 个 Profile）",
                child_group_list,
            )
            item.setData(Qt.UserRole, candidate_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if candidate_id in selected_child_set else Qt.Unchecked
            )
            if (
                candidate_id not in selected_child_set
                and self.would_create_group_cycle(group['id'], candidate_id)
            ):
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setToolTip('不能加入：这会形成分组循环引用')
        child_layout.addWidget(child_group_list, 1)

        child_buttons = QtWidgets.QHBoxLayout()
        child_up_button = QtWidgets.QPushButton('上移', child_page)
        child_down_button = QtWidgets.QPushButton('下移', child_page)

        def move_child_group(offset):
            row = child_group_list.currentRow()
            target = row + offset
            if row < 0 or target < 0 or target >= child_group_list.count():
                return
            item = child_group_list.takeItem(row)
            child_group_list.insertItem(target, item)
            child_group_list.setCurrentItem(item)

        child_up_button.clicked.connect(lambda: move_child_group(-1))
        child_down_button.clicked.connect(lambda: move_child_group(1))
        child_buttons.addWidget(child_up_button)
        child_buttons.addWidget(child_down_button)
        child_buttons.addStretch(1)
        child_layout.addLayout(child_buttons)
        tabs.addTab(child_page, '包含的子分组')
        layout.addWidget(tabs, 1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel,
            parent=dialog,
        )
        buttons.button(QtWidgets.QDialogButtonBox.Save).setText('保存成员')
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        selected_directories = [
            member_list.item(index).data(Qt.UserRole)
            for index in range(member_list.count())
            if member_list.item(index).checkState() == Qt.Checked
        ]
        selected_child_ids = [
            child_group_list.item(index).data(Qt.UserRole)
            for index in range(child_group_list.count())
            if child_group_list.item(index).checkState() == Qt.Checked
        ]
        self.update_profile_group_members(
            group,
            selected_directories,
            selected_child_ids,
        )

    def save_selected_profiles_to_group(self):
        """保留给旧入口调用：将当前列表选中项作为全部成员。"""
        group = self.get_profile_group()
        if group is None:
            QMessageBox.warning(self, '警告', '请先选择一个配置分组！')
            return False
        return self.update_profile_group_members(
            group, self.get_selected_profile_directories()
        )

    def start_group_iterator(self):
        group = self.get_profile_group()
        if group is None:
            QMessageBox.warning(self, '警告', '请先选择一个配置分组！')
            return None
        group_directories = self.get_group_profile_directories(group['id'])
        if not group_directories:
            QMessageBox.warning(
                self,
                '警告',
                f"分组“{group['name']}”中没有当前可用的 Chrome Profile！",
            )
            return None
        if (
            group_directories != self.iterator_profile_directories
            or self.active_iterator_group_id != group['id']
        ):
            self.iterator_profile_directories = group_directories
            self.active_iterator_group_id = group['id']
            next_directory = str(group.get('next_profile_directory') or '')
            if next_directory not in group_directories:
                next_directory = group_directories[0]
                group['next_profile_directory'] = next_directory
            self.next_profile_index = group_directories.index(next_directory)
            self._saved_next_profile_directory = next_directory
            self.save_iterator_state()
        return self.launch_next_profile()

    @staticmethod
    def normalize_website_url(url):
        url = str(url or '').strip()
        if url and '://' not in url:
            url = f'https://{url}'
        return url

    def set_websites(self, websites):
        self.web_list_widget.clear()
        seen = set()
        for website in websites:
            website = self.normalize_website_url(website)
            if website and website not in seen:
                self.web_list_widget.addItem(website)
                seen.add(website)

    def load_websites(self):
        config = self.read_config()
        websites = config.get(self.websites_config_key)
        should_initialize_config = (
            self.websites_config_key not in config
            or not isinstance(websites, list)
        )
        if should_initialize_config:
            websites = self.default_websites
        self.set_websites(websites)
        if should_initialize_config:
            config[self.websites_config_key] = self.get_web_list()
            if not self.write_config(config):
                QMessageBox.critical(self, '错误', '初始化预设网站配置失败！')

    def save_websites(self):
        config = self.read_config()
        config[self.websites_config_key] = self.get_web_list()
        saved = self.write_config(config)
        if not saved:
            QMessageBox.critical(self, '错误', '预设网站写入配置文件失败！')
        return saved

    def add_website(self):
        website, accepted = QInputDialog.getText(
            self,
            '添加预设网站',
            '网站地址：',
        )
        if not accepted:
            return
        website = self.normalize_website_url(website)
        if not website:
            return
        if website in self.get_web_list():
            QMessageBox.information(self, '提示', '这个网址已经在列表中。')
            return
        self.web_list_widget.addItem(website)
        if self.save_websites():
            self.update_iterator_status('已添加预设网站')

    def edit_website(self, item=None):
        if item is None:
            item = self.web_list_widget.currentItem()
        if item is None:
            QMessageBox.warning(self, '警告', '请先选择一个要修改的网址！')
            return

        old_website = item.data(Qt.DisplayRole)
        website, accepted = QInputDialog.getText(
            self,
            '修改预设网站',
            '网站地址：',
            QtWidgets.QLineEdit.Normal,
            old_website,
        )
        if not accepted:
            return
        website = self.normalize_website_url(website)
        if not website:
            QMessageBox.warning(self, '警告', '网址不能为空！')
            return
        if website != old_website and website in self.get_web_list():
            QMessageBox.information(self, '提示', '这个网址已经在列表中。')
            return
        item.setText(website)
        if self.save_websites():
            self.update_iterator_status('已修改预设网站')

    def delete_websites(self):
        selected_items = self.web_list_widget.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, '警告', '请先选择要删除的网址！')
            return
        answer = QMessageBox.question(
            self,
            '删除预设网站',
            f'确定删除选中的 {len(selected_items)} 个网址吗？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        rows = sorted(
            (self.web_list_widget.row(item) for item in selected_items),
            reverse=True,
        )
        for row in rows:
            self.web_list_widget.takeItem(row)
        if self.save_websites():
            self.update_iterator_status(f'已删除 {len(rows)} 个预设网站')

    def load_iterator_state(self):
        state = self.read_config().get(self.iterator_config_key, {})
        if not isinstance(state, dict):
            state = {}
        selected_directories = state.get('selected_profile_directories', [])
        if not isinstance(selected_directories, list):
            selected_directories = []
        self.iterator_profile_directories = list(dict.fromkeys(
            str(directory)
            for directory in selected_directories
            if directory
        ))
        try:
            self.next_profile_index = max(0, int(state.get('next_index', 0)))
        except (TypeError, ValueError):
            self.next_profile_index = 0
        next_directory = state.get('next_profile_directory')
        self._saved_next_profile_directory = (
            str(next_directory) if next_directory else None
        )
        active_group_id = state.get('source_group_id')
        self.active_iterator_group_id = (
            str(active_group_id) if active_group_id else None
        )

    def save_iterator_state(self):
        config = self.read_config()
        next_directory = None
        iterator_profiles = self.get_iterator_profiles()
        if iterator_profiles:
            self.next_profile_index %= len(iterator_profiles)
            next_directory = iterator_profiles[self.next_profile_index]['directory']

        config[self.iterator_config_key] = {
            'next_index': self.next_profile_index,
            'next_profile_directory': next_directory,
            'selected_profile_directories': self.iterator_profile_directories,
            'source_group_id': self.active_iterator_group_id,
        }
        active_group = (
            self.get_profile_group(self.active_iterator_group_id)
            if self.active_iterator_group_id else None
        )
        if active_group is not None:
            active_group['next_profile_directory'] = next_directory or ''
            # 迭代进度和分组成员同一次原子写入，避免突然退出后两处不一致。
            config[self.profile_groups_config_key] = self.profile_groups_config()
        if self.write_config(config):
            self._saved_next_profile_directory = next_directory

    def group_progress_text(self, group, prefix=''):
        directories = self.get_group_profile_directories(group.get('id'))
        next_directory = str(group.get('next_profile_directory') or '')
        profile_names = {
            profile['directory']: profile['name'] for profile in self.profiles
        }
        if not directories:
            progress = '组内暂无可用 Profile'
        else:
            if next_directory not in directories:
                next_directory = directories[0]
            progress = (
                f"下一个：{profile_names.get(next_directory, next_directory)} "
                f"({directories.index(next_directory) + 1}/{len(directories)})"
            )
        label = f"分组“{group.get('name', '')}”"
        return f'{prefix}{label}；{progress}' if prefix else f'{label}；{progress}'

    def restore_iterator_position(self):
        available_directories = {
            profile['directory'] for profile in self.profiles
        }
        active_group = self.get_profile_group(self.active_iterator_group_id)
        if active_group is not None:
            # Re-expand the hierarchy after restart so child-group edits and
            # their configured order are reflected without losing the cursor.
            self.iterator_profile_directories = (
                self.get_group_profile_directories(active_group['id'])
            )
        else:
            selected_set = {
                directory for directory in self.iterator_profile_directories
                if directory in available_directories
            }
            # Manual selection remains ordered like the visible Profile list.
            self.iterator_profile_directories = [
                profile['directory']
                for profile in self.profiles
                if profile['directory'] in selected_set
            ]

        display_selected_set = set(self.iterator_profile_directories)
        selected_group = self.get_profile_group()
        if selected_group is not None:
            display_selected_set = set(
                selected_group.get('profile_directories', [])
            )
        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            item.setSelected(item.data(Qt.UserRole) in display_selected_set)

        iterator_profiles = self.get_iterator_profiles()
        if not iterator_profiles:
            self.next_profile_index = 0
            return

        if self._saved_next_profile_directory:
            for index, profile in enumerate(iterator_profiles):
                if profile['directory'] == self._saved_next_profile_directory:
                    self.next_profile_index = index
                    break
            else:
                self.next_profile_index %= len(iterator_profiles)
        else:
            self.next_profile_index %= len(iterator_profiles)

    def get_selected_profile_directories(self):
        """按列表显示顺序返回当前选中的 Profile 目录。"""
        selected_directories = []
        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            if item.isSelected():
                selected_directories.append(item.data(Qt.UserRole))
        return selected_directories

    def get_iterator_profiles(self):
        profiles_by_directory = {
            profile['directory']: profile for profile in self.profiles
        }
        return [
            profiles_by_directory[directory]
            for directory in self.iterator_profile_directories
            if directory in profiles_by_directory
        ]

    def update_iterator_status(self, message=None):
        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            item.setBackground(QtGui.QBrush())
            item.setToolTip(self.profile_group_tooltip(item.data(Qt.UserRole)))

        iterator_profiles = self.get_iterator_profiles()
        if not iterator_profiles:
            prompt = (
                '未设置迭代列表：请选择分组并点击“按此分组迭代启动”，'
                '或选择 Profile 后点击“按选中项迭代启动”'
            )
            self.status_label.setText(f'{message}；{prompt}' if message else prompt)
            return

        self.next_profile_index %= len(iterator_profiles)
        next_profile = iterator_profiles[self.next_profile_index]
        next_item = None
        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            if item.data(Qt.UserRole) == next_profile['directory']:
                next_item = item
                break
        if next_item is not None:
            next_item.setBackground(QtGui.QBrush(QtGui.QColor('#D9F2D9')))
            tooltip = next_item.toolTip()
            next_item.setToolTip(
                f'{tooltip}\n下一个将启动此 Profile'
                if tooltip
                else '下一个将启动此 Profile'
            )
            self.chrome_list_widget.scrollToItem(next_item)

        active_group = (
            self.get_profile_group(self.active_iterator_group_id)
            if self.active_iterator_group_id
            else None
        )
        scope_text = (
            f"分组“{active_group['name']}”"
            if active_group is not None
            else '选中队列'
        )
        next_text = (
            f"{scope_text}共 {len(iterator_profiles)} 个；下一个：{next_profile['name']} "
            f"({self.next_profile_index + 1}/{len(iterator_profiles)})"
        )
        self.status_label.setText(f'{message}；{next_text}' if message else next_text)

    def profile_group_tooltip(self, profile_directory):
        group_names = [
            group['name']
            for group in self.profile_groups
            if profile_directory in self.get_group_profile_directories(group['id'])
        ]
        return f"所属分组：{'、'.join(group_names)}" if group_names else ''

    def reset_iterator(self):
        if not self.get_iterator_profiles():
            QMessageBox.warning(
                self,
                '警告',
                '尚未设置迭代列表，请先选择 Profile 并点击“按选中项迭代启动”！'
            )
            return
        self.next_profile_index = 0
        self.save_iterator_state()
        self.update_iterator_status('迭代进度已重置')

    def select_all(self):
        """全选所有 profiles"""
        for i in range(self.chrome_list_widget.count()):
            self.chrome_list_widget.item(i).setSelected(True)

    def deselect_all(self):
        """取消全选"""
        self.chrome_list_widget.clearSelection()

    def launch_selected_profiles(self):
        """启动选中的 profiles"""
        if not self.chrome_path:
            QMessageBox.warning(self, '错误', 'Chrome 可执行文件未找到！')
            return

        selected_items = self.chrome_list_widget.selectedItems()

        if not selected_items:
            QMessageBox.warning(self, '警告', '请至少选择一个 profile！')
            return

        launched_count = 0

        for item in selected_items:
            profile_dir = item.data(Qt.UserRole)
            profile = next(
                (profile for profile in self.profiles
                 if profile['directory'] == profile_dir),
                {'name': profile_dir, 'directory': profile_dir},
            )
            success, error = self.launch_profile(profile)
            if success:
                launched_count += 1
                time.sleep(random.random())
            else:
                QMessageBox.critical(self, '错误', f'启动失败: {error}')

        self.update_iterator_status(f'已批量启动 {launched_count} 个 profiles')

    def launch_profile(self, profile):
        if not self.chrome_path:
            return False, 'Chrome 可执行文件未找到'
        try:
            cmd = [
                self.chrome_path,
                f"--profile-directory={profile['directory']}",
                '--new-window',
                '--start-maximized',
                *self.get_web_list(),
            ]
            subprocess.Popen(cmd)
            return True, None
        except Exception as error:
            return False, str(error)

    def start_selected_iterator(self):
        """用窗口中当前选中项建立/更新迭代队列，并启动队列下一项。"""
        selected_directories = self.get_selected_profile_directories()
        if not selected_directories:
            QMessageBox.warning(
                self,
                '警告',
                '请先在 Chrome 列表中选择要迭代启动的 Profile！'
            )
            return None

        if (
            selected_directories != self.iterator_profile_directories
            or self.active_iterator_group_id is not None
        ):
            self.iterator_profile_directories = selected_directories
            self.active_iterator_group_id = None
            self.next_profile_index = 0
            self._saved_next_profile_directory = selected_directories[0]
            self.save_iterator_state()
        return self.launch_next_profile()

    def launch_next_profile(self):
        """只在已保存的选中队列中启动下一项，并持久化位置。"""
        iterator_profiles = self.get_iterator_profiles()
        if not iterator_profiles:
            QMessageBox.warning(
                self,
                '警告',
                '没有已保存的迭代列表。请选择一个分组并点击“按此分组迭代启动”，'
                '或选择 Profile 后点击“按选中项迭代启动”！'
            )
            return None

        self.next_profile_index %= len(iterator_profiles)
        current_index = self.next_profile_index
        profile = iterator_profiles[current_index]
        success, error = self.launch_profile(profile)
        if not success:
            QMessageBox.critical(self, '错误', f'启动失败: {error}')
            self.update_iterator_status('启动失败，迭代位置未推进')
            return None

        self.next_profile_index = (current_index + 1) % len(iterator_profiles)
        wrapped = self.next_profile_index == 0
        self.save_iterator_state()
        message = f"已启动：{profile['name']}"
        if wrapped:
            message += '；已到列表末尾，将从头继续'
        self.update_iterator_status(message)
        return profile


    def get_web_list(self):
        result = []
        for row in range(self.web_list_widget.count()):
            item = self.web_list_widget.item(row)
            result.append(item.data(Qt.DisplayRole))
        return result
