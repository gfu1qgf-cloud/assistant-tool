"""Visible task actions; navigation never reads media or changes pool cursors."""
from qt_compat import QtCore, QtWidgets
from .component_ui import Combo


class WorkspaceNavigation:
    def init_workspace_navigation(self, layout):
        self.navigation = QtWidgets.QWidget(self)
        row = QtWidgets.QHBoxLayout(self.navigation)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QtWidgets.QLabel('项目'))
        self.project_selector = Combo()
        self.project_selector.setAccessibleName('切换项目')
        self.project_selector.setMinimumWidth(140)
        self.project_selector.setMaximumWidth(240)
        self.project_selector.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.project_selector.setMinimumContentsLength(12)
        self.project_selector.activated.connect(self._navigate_project)
        row.addWidget(self.project_selector)
        self.save_project_button = QtWidgets.QPushButton('保存项目')
        self.save_project_button.setAutoDefault(False)
        self.save_project_button.setToolTip('Ctrl+S：保存当前项目的素材、控件及任务配置。')
        self.save_project_button.clicked.connect(self._save_from_navigation)
        row.addWidget(self.save_project_button)
        row.addSpacing(12)
        self.workflow_buttons = {}
        for key, text, callback in (
                ('resources', '资源与素材', self.show_resources),
                ('design', '编辑画面', self.show_design),
                ('pairing', '批量搭配…', self.compose_queue),
                ('queue', '渲染队列', self.open_global_queue)):
            button = self.global_queue_button if key == 'queue' else QtWidgets.QPushButton(text)
            button.setAutoDefault(False)
            if key != 'queue': button.clicked.connect(callback)
            row.addWidget(button)
            self.workflow_buttons[key] = button
        self.workflow_buttons['pairing'].setToolTip('选择数量基准、文案、背景及人声来源，核对后加入渲染队列。')
        self.workflow_buttons['queue'].setToolTip('查看全部项目进度；设置并发、编码质量及覆盖同名文件，然后开始导出。')
        row.addStretch()
        layout.insertWidget(1, self.navigation)

        self.pool_tools = QtWidgets.QWidget()
        tools = QtWidgets.QVBoxLayout(self.pool_tools)
        tools.setContentsMargins(0, 0, 0, 0)
        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(4)
        self.pool_add_button = QtWidgets.QToolButton()
        self.pool_add_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.pool_add_menu = QtWidgets.QMenu(self.pool_add_button)
        self.pool_add_button.setMenu(self.pool_add_menu)
        self.pool_add_menu.aboutToShow.connect(self._populate_pool_add_menu)
        self.pool_add_button.clicked.connect(self.add_to_visible_pool)
        self.pool_use_button = QtWidgets.QToolButton()
        self.pool_use_button.setText('使用素材')
        self.pool_use_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.pool_use_menu = QtWidgets.QMenu(self.pool_use_button)
        self.pool_use_button.setMenu(self.pool_use_menu)
        self.pool_use_menu.aboutToShow.connect(self._populate_pool_use_menu)
        self.resources.setCornerWidget(None)
        self.new_pool_button.setText('新建池…')
        for button in (self.pool_add_button, self.pool_use_button, self.new_pool_button):
            actions.addWidget(button)
        self.new_pool_button.show()
        actions.addStretch()
        tools.addLayout(actions)
        self.pool_count_label = QtWidgets.QLabel()
        self.pool_count_label.setWordWrap(True)
        tools.addWidget(self.pool_count_label)
        self.resource_pane_layout.insertWidget(0, self.pool_tools)
        self.resources.currentChanged.connect(self.refresh_resource_actions)
        self.tabs.currentChanged.connect(self.refresh_resource_actions)
        self.refresh_navigation()

    def show_resources(self):
        self.tabs.setCurrentWidget(self.resources)
        key = self.visible_pool_key()
        if key: self._remember_resource(key)
        self.refresh_resource_actions()

    def show_design(self):
        self.tabs.setCurrentIndex(self.layer_tab_index)
        self.right_tabs.setCurrentIndex(0)

    def _navigate_project(self, *_):
        identifier = self.project_selector.currentData()
        if identifier: self.switch_profile(identifier)
        self.refresh_navigation()

    def _save_from_navigation(self):
        if not self.is_batch_busy() and self.persist():
            self.status.setText('当前项目已保存。')

    def refresh_navigation(self):
        if not hasattr(self, 'project_selector'): return
        with QtCore.QSignalBlocker(self.project_selector):
            self.project_selector.clear()
            for profile in self.state['profiles']:
                self.project_selector.addItem(profile['name'], profile['id'])
            self.project_selector.setCurrentIndex(max(0, self.project_selector.findData(self.state['active_profile'])))
        self.refresh_resource_actions()
        self.update_navigation_busy(self.is_batch_busy())

    def update_navigation_busy(self, busy):
        if not hasattr(self, 'project_selector'): return
        self.project_selector.setEnabled(not busy or self.state['active_profile'] in self._host_plans)
        self.save_project_button.setEnabled(not busy)
        self.canvas_settings_action.setEnabled(not busy)
        self.workflow_buttons['pairing'].setEnabled(not busy)
        self.render_queue_panel.tools.set_editable(not busy)

    def visible_pool_key(self):
        page = self.resources.currentWidget()
        return page.property('resource_key') if page is not None else None

    def refresh_resource_actions(self, *_):
        if not hasattr(self, 'pool_tools'): return
        self.pool_tools.setVisible(self.tabs.currentWidget() is self.resources)
        key = self.visible_pool_key()
        if not key: return
        table = self.table_for(key)
        if not table.property('navigation_connected'):
            table.setProperty('navigation_connected', True)
            table.itemSelectionChanged.connect(self.refresh_resource_actions)
        label = {'backgrounds': '添加背景…', 'copy_pool': '新增文案',
                 'image_pool': '添加图片…', 'music': '添加音乐…'}.get(key, '添加文件…')
        self.pool_add_button.setText(label)
        self.pool_add_button.setAccessibleName(label)
        count, selected = table.rowCount(), len(table.identifiers())
        self.pool_count_label.setText(f'{count} 项 · 已选 {selected} 项' if count else '此池为空，点击上方按钮添加素材。')
        self.pool_use_button.setEnabled(count > 0)

    def _pool_command(self, key, callback):
        if self.is_batch_busy(): return
        self.show_resources()
        self._active_resource_key = key
        self.table_for(key).setFocus()
        callback()

    def add_to_visible_pool(self):
        key = self.visible_pool_key()
        callback = {'backgrounds': self.add_videos, 'copy_pool': self.add_manual_copy,
                    'image_pool': self.choose_pool_images, 'music': self.add_music}.get(key, self.choose_custom_files)
        if key: self._pool_command(key, callback)

    def _populate_pool_add_menu(self):
        self.pool_add_menu.clear()
        self.pool_add_menu.addAction(self.pool_add_button.text(), self.add_to_visible_pool)
        if self.visible_pool_key() == 'copy_pool':
            self.pool_add_menu.addAction('导入当前任务文案、人声及字幕…', self.import_sheet_text)
            self.pool_add_menu.addAction('导入任务文件夹…', self.add_task_folder)

    def _populate_pool_use_menu(self):
        from .custom_pools import get_pool
        self.pool_use_menu.clear()
        key = self.visible_pool_key()
        if not key: return
        pool = get_pool(self.state, key[5:]) if key.startswith('pool:') else None
        kind = pool['kind'] if pool else {'image_pool': 'image', 'backgrounds': 'background',
                                         'copy_pool': 'text', 'music': 'music'}[key]
        selected = bool(self.table_for(key).identifiers())
        def add(text, callback, enabled=True):
            action = self.pool_use_menu.addAction(text, lambda _=False: self._pool_command(key, callback))
            action.setEnabled(enabled)
        if kind in {'image', 'video', 'visual'}:
            add('所选素材 → 固定图层', lambda: self.use_pool_images(False, key), selected)
            add('绑定此池 → 逐个成品循环', lambda: self.use_pool_images(True, key))
            add('所选素材 → 抠像工作台…', lambda: self.matte_pool_selection(key=key), selected)
        if kind == 'music':
            add('用此池作为背景音乐', lambda: self.use_custom_music(pool['id'] if pool else ''))
        elif kind == 'voice':
            add('所选人声 → 当前任务／所选队列', lambda: self.use_custom_voices(pool['id']), selected)
        if kind in {'background', 'video', 'visual', 'text', 'voice'}:
            add('批量搭配，加入渲染队列…', self.compose_queue)
