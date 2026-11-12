"""Ordinary Ctrl+C/Ctrl+V between pools and projects via the system clipboard."""
from qt_compat import QtCore, QtWidgets
from . import pool_copy


def copy_selected(host, key):
    try:
        if key not in pool_copy.libraries(host.state):
            raise ValueError('请在资源池中选择素材。')
        if not host.is_batch_busy():
            host._commit_edit()
        payload = pool_copy.capture(host.state, key, host.table_for(key).identifiers())
        data = QtCore.QMimeData()
        data.setData(pool_copy.MIME, pool_copy.encode(payload))
        data.setText('\n'.join(item.get('path') or item.get('title') or item.get('body', '')
                               for item in payload['records']))
        QtWidgets.QApplication.clipboard().setMimeData(data)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        QtWidgets.QMessageBox.warning(host, '未复制素材', str(error))
        return False
    host.status.setText(f"已复制 {len(payload['records'])} 项；切换资源池或项目后按 Ctrl+V 粘贴。")
    return True


def add_actions(host, menu, key):
    if key not in pool_copy.libraries(host.state):
        return
    menu.addAction('复制\tCtrl+C', lambda: copy_selected(host, key)).setEnabled(bool(host.table_for(key).identifiers()))
    data = QtWidgets.QApplication.clipboard().mimeData()
    menu.addAction('粘贴\tCtrl+V', lambda: paste(host, key)).setEnabled(
        not host.is_batch_busy() and data is not None and data.hasFormat(pool_copy.MIME))


def install_menu(host, menu):
    menu.addAction('复制素材\tCtrl+C', lambda: copy_selected(host, host.visible_pool_key()))
    menu.addAction('粘贴素材\tCtrl+V', lambda: paste(host, host.visible_pool_key()))


def attach(host, table, key):
    table.copyRequested.connect(lambda: copy_selected(host, key))
    table.pasteRequested.connect(lambda: paste(host, key))


def paste(host, target):
    if host.is_batch_busy():
        QtWidgets.QMessageBox.warning(host, '未粘贴素材', '当前项目正在处理，请结束后再粘贴。')
        return False
    try:
        data = QtWidgets.QApplication.clipboard().mimeData()
        if data is None or not data.hasFormat(pool_copy.MIME):
            raise ValueError('剪贴板中没有素材记录，请先在资源池中复制。')
        payload = pool_copy.decode(bytes(data.data(pool_copy.MIME)))
        pool_copy.validate_target(host.state, target, payload)
    except (ValueError, TypeError, KeyError) as error:
        QtWidgets.QMessageBox.warning(host, '未粘贴素材', str(error))
        return False
    host.edit_timer.stop()
    if not host.persist():
        QtWidgets.QMessageBox.warning(host, '未粘贴素材', '当前修改保存失败，请检查下方错误提示后重试。')
        return False
    try:
        identifier = host.state['active_profile']
        profile = next(p for p in host.state['profiles'] if p['id'] == identifier)
        updated, result = host.store.paste_pool_records(identifier, target, payload,
            expected_profile_revision=profile.get('revision', 0))
        if not result['added']:
            host.status.setText(f"目标池已包含所选记录，跳过 {result['skipped']} 项。")
            return True
    except Exception as error:
        host.status.setText('粘贴失败：'+str(error))
        QtWidgets.QMessageBox.warning(host, '未粘贴素材', str(error))
        return False
    host.state = updated
    with QtCore.QSignalBlocker(host.layer_panel):
        host.layer_panel.set_layers(updated['settings']['layers'])
    host.refresh_tables()
    host.invalidate_playback()
    host.update_preview()
    host.refresh_editor_baseline()
    host.status.setText(f"已粘贴 {result['added']} 项，跳过重复 {result['skipped']} 项。")
    return True
