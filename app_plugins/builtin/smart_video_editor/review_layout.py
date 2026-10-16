"""Presentation only: reuse existing controls and slots, without editing rules."""
from qt_compat import QtCore, QtGui, QtWidgets


class ButtonAction(QtGui.QAction):
    """Menus follow the real button's enabled state and invoke its existing slot."""
    def __init__(self, menu, button, title=None):
        super().__init__(title or button.text(), menu)
        self.button = button
        self.setEnabled(button.isEnabled())
        self.setToolTip(button.toolTip())
        button.installEventFilter(self)
        button.hide()
        self.triggered.connect(lambda _checked=False: button.click())
        menu.addAction(self)

    def eventFilter(self, watched, event):
        if watched is self.button and event.type() == QtCore.QEvent.Type.EnabledChange:
            self.setEnabled(self.button.isEnabled())
        return False


def menu_button(title, parent):
    button = QtWidgets.QToolButton(parent)
    button.setText(title)
    button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QtWidgets.QMenu(button)
    button.setMenu(menu)
    return button, menu


def fresh_layout(panel):
    # Transfer Qt ownership before replacing a layout; retain all controls.
    holder = QtWidgets.QWidget(panel)
    holder.setLayout(panel.layout())
    while holder.layout().count():
        item = holder.layout().takeAt(0)
        widget = item.widget()
        if widget:
            widget.setParent(panel)
            widget.hide()
    holder.deleteLater()
    layout = QtWidgets.QVBoxLayout(panel)
    layout.setContentsMargins(8, 8, 8, 8)
    return layout


def form_group(title, rows, parent):
    group = QtWidgets.QGroupBox(title, parent)
    form = QtWidgets.QFormLayout(group)
    form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
    form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    for label, widget in rows:
        form.addRow(label, widget)
        widget.show()
    return group


def scroll_page(widget, parent):
    scroll = QtWidgets.QScrollArea(parent)
    scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    widget.show()
    return scroll


def arrange_timeline(review, toolbar, refresh_button):
    """Task navigation above; video/timeline/settings get independent columns."""
    while toolbar.count():
        item = toolbar.takeAt(0)
        if item.widget(): item.widget().hide()
    toolbar.addWidget(QtWidgets.QLabel("任务", review))
    review.task_combo.setMinimumContentsLength(12)
    review.task_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    toolbar.addWidget(review.task_combo, 1); review.task_combo.show()
    toolbar.addWidget(review.task_refresh_button)
    review.task_refresh_button.setVisible(review._task_refresh_available and not review.breath_only)
    filter_label = QtWidgets.QLabel("筛选", review)
    toolbar.addWidget(filter_label)
    toolbar.addWidget(review.task_filter_combo); review.task_filter_combo.show()
    review.missing_task_count_label.setWordWrap(True)
    review.missing_task_count_label.setMinimumWidth(150)
    review.missing_task_count_label.setMaximumWidth(180)
    toolbar.addWidget(review.missing_task_count_label); review.missing_task_count_label.show()
    review.task_filter_widget.hide()
    toolbar.addWidget(QtWidgets.QLabel("画面", review))
    toolbar.addWidget(review.preview_aspect_combo); review.preview_aspect_combo.show()
    edit, edit_menu = menu_button("片段操作", review)
    view, view_menu = menu_button("视图", review)
    review.clip_actions_menu, review.view_actions_menu = edit_menu, view_menu
    for button, title in ((review.move_clip_left_button, "前移当前片段"),
                          (review.move_clip_right_button, "后移当前片段")):
        ButtonAction(edit_menu, button, title)
    edit_menu.addSeparator()
    for button in (review.mark_delete_start_button, review.mark_delete_end_button,
                   review.restore_removed_button, review.clear_manual_button):
        ButtonAction(edit_menu, button)
    for button in (review.zoom_fit_button, review.zoom_in_button, review.zoom_out_button, refresh_button):
        ButtonAction(view_menu, button)
    view_menu.addSeparator()
    ButtonAction(view_menu, review.previous_missing_task_button)
    ButtonAction(view_menu, review.next_missing_task_button)
    toolbar.addWidget(edit); toolbar.addWidget(view)

    breath = review.breath_panel
    breath.setTitle("气口与内部停顿")
    layout = fresh_layout(breath)
    layout.addWidget(form_group("裁切策略", [
        ("判断方式", review.breath_detection_mode_combo),
        ("", review.silence_detection_checkbox), ("", review.voice_detection_checkbox),
        ("", review.compress_pauses_checkbox),
        ("内部停顿 ≥", review.pause_threshold_spinbox),
        ("向前保留", review.pause_keep_before_spinbox),
        ("向后保留", review.pause_keep_after_spinbox)], breath))
    layout.addWidget(form_group("分贝检测参数", [
        ("静音阈值", review.silence_db_spinbox), ("最短静音", review.min_silence_spinbox)], breath))
    layout.addWidget(form_group("人声检测参数", [
        ("开始阈值", review.vad_threshold_spinbox), ("结束阈值", review.vad_neg_threshold_spinbox),
        ("最短人声", review.vad_min_speech_spinbox), ("人声保护", review.vad_speech_pad_spinbox)], breath))
    actions = QtWidgets.QHBoxLayout()
    for button in (review.apply_breath_button, review.save_breath_defaults_button):
        actions.addWidget(button); button.show()
    layout.addLayout(actions)
    review.breath_status_label.setWordWrap(True)
    layout.addWidget(review.breath_status_label); review.breath_status_label.show()
    layout.addStretch(1)

    subtitles = review.subtitle_settings_panel
    subtitles.setTitle("字幕设置")
    for control in (review.subtitle_max_words_spinbox, review.subtitle_max_chars_spinbox, review.subtitle_gap_spinbox):
        control.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Fixed)
    layout = fresh_layout(subtitles)
    layout.addWidget(form_group("分块与间隔（本次即时生效）", [
        ("每块最多单词", review.subtitle_max_words_spinbox),
        ("每块最多字符", review.subtitle_max_chars_spinbox),
        ("字幕块间隔", review.subtitle_gap_spinbox),
        ("", review.subtitle_line_break_checkbox)], subtitles))
    layout.addWidget(form_group("识别模型（保存并重启后生效）", [("模型", review.subtitle_model_combo)], subtitles))
    review.subtitle_settings_status.setWordWrap(True)
    layout.addWidget(review.subtitle_settings_status); review.subtitle_settings_status.show()
    layout.addWidget(review.save_subtitle_defaults_button); review.save_subtitle_defaults_button.show()
    layout.addStretch(1)

    if not review.breath_only:
        comparison = review.comparison_panel
        comparison.setTitle("任务语音核对")
        layout = fresh_layout(comparison)
        layout.setContentsMargins(6, 6, 6, 6)
        tabs = QtWidgets.QTabWidget(comparison)
        full_text = QtWidgets.QWidget(tabs)
        full_layout = QtWidgets.QVBoxLayout(full_text)
        full_layout.setContentsMargins(0, 0, 0, 0)
        full_layout.addWidget(review.task_script_text); review.task_script_text.show()
        tabs.addTab(full_text, "完整任务语音（点击片段定位原文）")
        current = QtWidgets.QWidget(tabs)
        current_layout = QtWidgets.QGridLayout(current)
        current_layout.addWidget(QtWidgets.QLabel("正确原文", current), 0, 0)
        current_layout.addWidget(QtWidgets.QLabel("视频识别", current), 0, 1)
        current_layout.addWidget(review.expected_text, 1, 0); review.expected_text.show()
        current_layout.addWidget(review.recognized_text, 1, 1); review.recognized_text.show()
        review.expected_text.setMaximumHeight(16777215)
        review.recognized_text.setMaximumHeight(16777215)
        tabs.addTab(current, "当前片段对照")
        layout.addWidget(tabs)
        review.script_review_tabs = tabs

    # Preserve the original issue panel, but give its long forms their own scroll.
    right = review.issue_panel
    right.setMinimumSize(0, 0)
    review.issue_tree.setMinimumHeight(140)
    review.issue_detail.setMinimumHeight(90)
    review.advanced_button.setText("表格精调…")
    decision = review.task_decision_panel.layout().itemAt(1).layout()
    widgets = []
    while decision.count():
        item = decision.takeAt(0)
        if item.widget(): widgets.append(item.widget())
    vertical = QtWidgets.QVBoxLayout()
    for button in widgets: vertical.addWidget(button)
    review.task_decision_panel.layout().removeItem(decision)
    review.task_decision_panel.layout().addLayout(vertical)
    right.setParent(review)  # Remove the old third splitter child.
    inspector = QtWidgets.QTabWidget(review.body_splitter)
    inspector.setMinimumWidth(285)
    review.inspector_tabs = inspector
    inspector.addTab(scroll_page(right, inspector), "问题处理")
    inspector.addTab(scroll_page(breath, inspector), "气口裁切")
    inspector.addTab(scroll_page(subtitles, inspector), "字幕设置")
    if review.breath_only:
        inspector.setTabVisible(0, False); inspector.setTabVisible(2, False)
        inspector.setCurrentIndex(1)
        right.hide(); subtitles.hide()
        filter_label.hide(); review.task_filter_combo.hide(); review.missing_task_count_label.hide()
        edit_menu.actions()[0].setVisible(False); edit_menu.actions()[1].setVisible(False)
        view_menu.actions()[-2].setVisible(False); view_menu.actions()[-1].setVisible(False)
    review.body_splitter.addWidget(inspector)
    review.body_splitter.setStretchFactor(0, 1)
    review.body_splitter.setStretchFactor(1, 3)
    review.body_splitter.setStretchFactor(2, 1)
    review.body_splitter.setSizes([310, 740, 360])
    # These live on a hidden settings tab until first opened. Clamp their
    # initial geometry too, rather than relying on a hidden form's layout pass.
    for control in (review.subtitle_max_words_spinbox, review.subtitle_max_chars_spinbox, review.subtitle_gap_spinbox):
        control.ensurePolished()
        control.setMaximumWidth(control.sizeHint().width() + 12)
    # The canvas is 260 px high; leave room for its horizontal scrollbar.
    review.timeline_scroll.setMinimumHeight(278)
    review.timeline_scroll.setMaximumHeight(290)


def arrange_review_footer(dialog, layout, buttons, hint):
    """Three primary actions, the remainder grouped under explicit menus."""
    open_button, report, copy_report, save, close = buttons
    bar = QtWidgets.QMenuBar(dialog)
    bar.setNativeMenuBar(False)
    file_menu = bar.addMenu("文件")
    ButtonAction(file_menu, open_button)
    file_menu.addSeparator()
    # Save stays visible below, so connect its existing button without hiding it.
    action = file_menu.addAction("保存核对结果")
    action.triggered.connect(lambda _checked=False: save.click())
    action.setShortcut(QtGui.QKeySequence("Ctrl+S"))
    tools = bar.addMenu("视频文件")
    tools.addAction("打开所选视频文件位置", dialog.open_selected_video_location)
    tools.addSeparator()
    ButtonAction(tools, dialog.rename_videos_button, "按顺序重命名原视频…（修改磁盘文件名）")
    reports = bar.addMenu("问题报告")
    ButtonAction(reports, report)
    ButtonAction(reports, copy_report)
    help_menu = bar.addMenu("使用帮助")
    action = help_menu.addAction("核对与导出说明")
    action.triggered.connect(lambda: QtWidgets.QMessageBox.information(dialog, "核对流程", hint.text()))
    hint.hide()
    close.setText("关闭（保留分析结果）")
    save.setText("保存核对")
    dialog.export_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Fixed)
    dialog.export_button.setStyleSheet("QPushButton { padding:6px 16px; border-radius:4px; }"
                                      "QPushButton:enabled { background:#2463a4; color:white; }")
    layout.insertWidget(0, bar)
    dialog.review_menu_bar = bar
