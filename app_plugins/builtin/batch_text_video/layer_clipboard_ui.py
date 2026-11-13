"""Component clipboard commands, scoped to the layer list rather than text editors."""
import copy
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from . import layer_clipboard as model


def host_for(panel):
    host = panel.window()
    return host if getattr(host, 'layer_panel', None) is panel else None


def context(panel):
    host = host_for(panel)
    state = host.state if host else panel._pool_state
    return state, str(Path(panel.directory).resolve())


def copy_component(panel):
    try:
        host = host_for(panel)
        if host and not host.is_batch_busy() and not host.flush_editor_history(): return False
        state, scope = context(panel)
        payload = model.capture(panel.current(), state, scope)
        data = QtCore.QMimeData()
        data.setData(model.MIME, model.encode(payload))
        data.setText(payload['layer']['name'])
        QtWidgets.QApplication.clipboard().setMimeData(data)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        QtWidgets.QMessageBox.warning(panel, '未复制控件', str(error))
        return False
    panel.message.emit('已复制控件；切换项目后在图层列表按 Ctrl+V 粘贴。')
    return True


def paste_component(panel):
    host = host_for(panel)
    if not panel.isEnabled() or panel.worker is not None or host and host.is_batch_busy():
        QtWidgets.QMessageBox.warning(panel, '未粘贴控件', '当前正在处理，请结束后再粘贴控件。')
        return False
    try:
        data = QtWidgets.QApplication.clipboard().mimeData()
        if data is None or not data.hasFormat(model.MIME):
            raise ValueError('剪贴板中没有控件，请先在图层列表复制。')
        payload = model.decode(bytes(data.data(model.MIME)))
        state, scope = context(panel)
        result = model.prepare(panel.values(), panel.background_values(), payload,
                               profile=state.get('active_profile',''), scope=scope)
        if result['replacements']:
            name = '、'.join(result['replacements'])
            answer = QtWidgets.QMessageBox.question(panel, '替换已有控件',
                '当前项目已有“'+name+'”，是否用复制的控件替换？可用 Ctrl+Z 撤销。',
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes: return False
        if host and not callable(getattr(host.store, 'save_controls', None)):
            raise ValueError('控件复制功能已更新，请正常重启软件后使用；当前设置未修改。')
        if host and (not host.flush_editor_history() or not host.persist()):
            raise ValueError('当前参数尚未保存成功，请处理下方错误后再粘贴。')
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        QtWidgets.QMessageBox.warning(panel, '未粘贴控件', str(error))
        return False
    before_layers, before_background = panel.values(), panel.background_values()
    before_selected = panel.current()['id'] if panel.current() else ''
    before_state = copy.deepcopy(host.state) if host else None
    history = (copy.deepcopy(host.edit_history.__dict__), copy.deepcopy(host._undo_baseline)) if host else None
    try:
        with QtCore.QSignalBlocker(panel):
            panel.set_layers(result['layers'])
            panel.set_background({'background_visual':result['background']})
            panel.select_layer_id(result['selected'])
        if host:
            host._commit_edit()
            host.state = host.store.save_controls(host.state, host.state['revision'])
            host.render_settings_timer.stop()
        else:
            panel.changed.emit()
    except Exception as error:
        if host:
            host.state = before_state
            host.edit_history.__dict__.update(history[0]); host._undo_baseline = history[1]
            host._update_undo_actions()
        with QtCore.QSignalBlocker(panel):
            panel.set_layers(before_layers)
            panel.set_background({'background_visual':before_background})
            panel.select_layer_id(before_selected)
        if host: host._sync_preview_layers()
        QtWidgets.QMessageBox.warning(panel, '未粘贴控件', '粘贴未保存，已撤回本次修改：'+str(error))
        return False
    if host:
        try:
            host.bind_undo_keys(); host._sync_preview_layers()
            host.invalidate_playback(); host.update_preview()
        except Exception as error:
            QtWidgets.QMessageBox.warning(panel, '控件已保存', '控件已粘贴并保存，但预览刷新失败：'+str(error))
            return True
    panel.message.emit('已粘贴控件。'+('；'.join(result['notices'])+'。' if result['notices'] else ''))
    return True


def add_actions(panel, menu):
    menu.addAction('复制控件\tCtrl+C', panel.copy_component).setEnabled(bool(panel.current()))
    data = QtWidgets.QApplication.clipboard().mimeData()
    menu.addAction('粘贴控件\tCtrl+V', panel.paste_component).setEnabled(
        panel.isEnabled() and data is not None and data.hasFormat(model.MIME))


def install(panel):
    panel.list.copyRequested.connect(panel.copy_component)
    panel.list.pasteRequested.connect(panel.paste_component)
    panel.background_button.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
    def background_menu(position):
        panel.select_background()
        menu = QtWidgets.QMenu(panel.background_button)
        add_actions(panel, menu)
        menu.exec(panel.background_button.mapToGlobal(position))
    panel.background_button.customContextMenuRequested.connect(background_menu)
    panel._clipboard_shortcuts = []
    for key, callback in ((QtGui.QKeySequence.StandardKey.Copy, panel.copy_component),
                          (QtGui.QKeySequence.StandardKey.Paste, panel.paste_component)):
        shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), panel.background_button)
        shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(callback); panel._clipboard_shortcuts.append(shortcut)
