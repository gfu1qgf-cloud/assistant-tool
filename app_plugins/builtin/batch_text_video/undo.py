"""Bounded, session-only editor undo. Files and delivery/rotation ledgers are not undone."""
import copy
import json
import traceback

from qt_compat import QtCore, QtWidgets


class EditHistory:
    def __init__(self, limit=80, byte_limit=8*1024*1024):
        self.limit, self.byte_limit = limit, byte_limit
        self.past, self.future = [], []

    def clear(self):
        self.past.clear()
        self.future.clear()

    def record(self, before, after):
        if before == after:
            return
        item = (copy.deepcopy(before), copy.deepcopy(after), self.size((before, after)))
        self.past.append(item)
        self.future.clear()
        while len(self.past) > self.limit or sum(value[2] for value in self.past) > self.byte_limit:
            self.past.pop(0)

    @staticmethod
    def size(item):
        return len(json.dumps(item, ensure_ascii=False).encode("utf8"))

    def undo(self, apply):
        if self.past and apply(copy.deepcopy(self.past[-1][0])):
            self.future.append(self.past.pop())
            return True
        return False

    def redo(self, apply):
        if self.future and apply(copy.deepcopy(self.future[-1][1])):
            self.past.append(self.future.pop())
            return True
        return False


class UndoKeys(QtCore.QObject):
    def eventFilter(self, watched, event):
        # Ignore destruction/layout events before touching any QWidget state.
        if event.type() not in {QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride}:
            return False
        dialog = self.parent()
        if (dialog is None or not isinstance(watched, QtWidgets.QWidget)
                or watched.window() is not dialog):
            return False
        modifiers = event.modifiers()
        if not modifiers & QtCore.Qt.KeyboardModifier.ControlModifier or modifiers & QtCore.Qt.KeyboardModifier.AltModifier:
            return False
        if event.key() not in {QtCore.Qt.Key.Key_Z, QtCore.Qt.Key.Key_Y}:
            return False
        redo = event.key() == QtCore.Qt.Key.Key_Y or bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        # Nested numeric/name/font editors may have uncommitted input. Let Qt
        # undo that input first, rather than undoing an unrelated layout command.
        if isinstance(watched, QtWidgets.QLineEdit) and watched not in {dialog.name, dialog.output, dialog.ffmpeg}:
            if watched.isRedoAvailable() if redo else watched.isUndoAvailable():
                return False
        event.accept()
        if event.type() == QtCore.QEvent.Type.KeyPress:
            (dialog.redo_edit if redo else dialog.undo_edit)()
        return True


class UndoController:
    def init_editor_history(self):
        self.edit_history = EditHistory()
        self._undo_applying = False
        menu = QtWidgets.QMenu("编辑", self.menu_bar)
        self.menu_bar.insertMenu(self.menu_bar.actions()[0], menu)
        self.undo_action = menu.addAction("撤销", self.undo_edit)
        self.undo_action.setShortcut("Ctrl+Z")
        self.redo_action = menu.addAction("重做", self.redo_edit)
        self.redo_action.setShortcuts(["Ctrl+Shift+Z", "Ctrl+Y"])
        note = "当前预览的文字、图层和参数；导出、资源库变更及切换任务/配置后重新开始记录。"
        self.undo_action.setToolTip(note)
        self.redo_action.setToolTip(note)
        menu.aboutToShow.connect(self.flush_editor_history)
        self.undo_keys = UndoKeys(self)
        # Scope interception to this editor, not QApplication's native/global
        # event stream (including other plugins and their dying windows).
        for widget in [self, *self.findChildren(QtWidgets.QWidget)]:
            widget.installEventFilter(self.undo_keys)
        self.reset_editor_history()

    def _editor_context(self):
        return (self.current_copy_id, self.current_id, self.preview_video, self.active_entry().get("path", ""))

    def _editor_guard(self):
        # Ignore editable names/text and revisions; include resources and real rotations.
        values = [self.state["active_profile"], self.state["backgrounds"], self.state["next_music_id"], self.state["music"]]
        for key in ("jobs", "copy_pool", "image_pool"):
            values.append([(item.get("id"), item.get("path"), item.get("task_dir"), item.get("voice_path"))
                           for item in self.state[key]])
        return copy.deepcopy(values)

    def _editor_snapshot(self):
        return {"settings": self.read_settings(), "title": self.title.toPlainText(),
                "body": self.body.toPlainText(), "name": self.name.text()}

    def _update_undo_actions(self, pending=False):
        self.undo_action.setEnabled(bool(self.edit_history.past) or pending)
        self.redo_action.setEnabled(bool(self.edit_history.future) and not pending)

    def reset_editor_history(self):
        if not hasattr(self, "edit_history") or self._undo_applying:
            return
        self.edit_history.clear()
        self._undo_context, self._undo_guard = self._editor_context(), self._editor_guard()
        self._undo_baseline = copy.deepcopy(self._editor_snapshot())
        self._update_undo_actions()

    def refresh_editor_baseline(self):
        if not hasattr(self, "edit_history") or self._undo_applying:
            return
        if self._editor_context() != self._undo_context or self._editor_guard() != self._undo_guard:
            self.reset_editor_history()
        else:
            self._undo_baseline = copy.deepcopy(self._editor_snapshot())

    def mark_editor_pending(self):
        if hasattr(self, "edit_history") and not self._undo_applying:
            self._update_undo_actions(pending=True)

    def capture_editor_change(self):
        if not hasattr(self, "edit_history") or self._undo_applying or self._loading:
            return
        if self._editor_context() != self._undo_context or self._editor_guard() != self._undo_guard:
            self.reset_editor_history()
            return
        snapshot = self._editor_snapshot()
        self.edit_history.record(self._undo_baseline, snapshot)
        self._undo_baseline = copy.deepcopy(snapshot)
        self._update_undo_actions()

    def undo_edit(self):
        return self._move_edit_history(False)

    def redo_edit(self):
        return self._move_edit_history(True)

    def _move_edit_history(self, redo):
        if self.is_batch_busy() or self._undo_applying:
            return False
        if not self.flush_editor_history():
            return False
        try:
            moved = (self.edit_history.redo if redo else self.edit_history.undo)(self._restore_editor_snapshot)
        except Exception:
            self._log("撤销/重做失败，未继续移动编辑历史：\n" + traceback.format_exc())
            return False
        self._update_undo_actions()
        if moved and not getattr(self, "_undo_refresh_failed", False):
            self.status.setText("已重做编辑。" if redo else "已撤销编辑。")
        return moved

    def flush_editor_history(self):
        try:
            # Cancel an unfinished drag rather than saving mouse-move steps.
            self.preview.cancel_drag()
            self.edit_timer.stop()
            self.geometry_preview_timer.stop()
            self._commit_edit()
            return True
        except Exception:
            self._log("暂时不能撤销/重做，请先完成当前参数输入：\n" + traceback.format_exc())
            return False

    def _restore_editor_snapshot(self, snapshot):
        self._undo_refresh_failed = False
        context = self._editor_context()
        selected = self.layer_panel.current()
        selected_id = selected["id"] if selected else ""
        focus = QtWidgets.QApplication.focusWidget()
        cursor = focus.textCursor().position() if isinstance(focus, QtWidgets.QPlainTextEdit) else (
            focus.cursorPosition() if isinstance(focus, QtWidgets.QLineEdit) else None)
        tab, resource_tab = self.tabs.currentIndex(), self.resources.currentIndex()
        target = copy.deepcopy(self.state)
        target["settings"] = copy.deepcopy(snapshot["settings"])
        key, identifier = ("copy_pool", self.current_copy_id) if self.current_copy_id else ("jobs", self.current_id)
        item = next((value for value in target[key] if value["id"] == identifier), None)
        if item:
            text_changed = any(item.get(field, "") != snapshot[field] for field in ("title", "body", "name"))
            item.update({field:snapshot[field] for field in ("title", "body", "name")})
            if key == "jobs" and text_changed:
                item.update(status="待生成", error="")
            elif not item.get("task_id"):
                item["label"] = item["name"] or "未命名文案"
        # Do not move the history pointer if saving is refused (e.g. another process).
        try:
            saved = self.store.save(target, target["revision"])
        except Exception:
            self._log("撤销/重做保存失败，原记录和编辑历史未覆盖：\n" + traceback.format_exc())
            return False
        self._undo_applying = True
        try:
            self.state = saved
            self.apply_profile_widgets()
            self.current_copy_id, self.current_id, self.preview_video, _path = context
            entry = self.active_entry()
            if identifier:
                self._show_job(entry)
            else:
                self.request_background(entry.get("path", ""))
                self.update_preview()
            self.layer_panel.select_layer_id(selected_id)
            self.tabs.setCurrentIndex(tab)
            self.resources.setCurrentIndex(resource_tab)
            self._undo_baseline = copy.deepcopy(self._editor_snapshot())
            self._undo_guard = self._editor_guard()
            if focus:
                focus.setFocus()
                if isinstance(focus, QtWidgets.QPlainTextEdit):
                    caret = focus.textCursor()
                    caret.setPosition(min(cursor, max(0, focus.document().characterCount()-1)))
                    focus.setTextCursor(caret)
                elif isinstance(focus, QtWidgets.QLineEdit):
                    focus.setCursorPosition(min(cursor, len(focus.text())))
            return True
        except Exception:
            # The record is saved already; never let a Python slot crash Qt.
            self._undo_refresh_failed = True
            self._log("撤销/重做已保存，但界面刷新失败，请重启软件：\n" + traceback.format_exc())
            return True
        finally:
            self._undo_applying = False
