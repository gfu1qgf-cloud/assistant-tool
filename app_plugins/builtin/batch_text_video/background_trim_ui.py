"""Right-click background range editor; no duplicate trim implementation."""
import copy
from .background_trim import KEY, selected, identity
from .visual_loop_region_ui import LoopRegionDialog
from .visual_media import is_animated
from qt_compat import QtWidgets


def source(editor, key):
    identifiers = editor.table_for(key).identifiers()
    if len(identifiers) != 1: return ''
    if key == 'backgrounds': return identifiers[0]
    if key.startswith('pool:'):
        return next((item['path'] for item in editor.resource_items(key) if item['id'] == identifiers[0]), '')
    return ''


def add_actions(editor, menu, key):
    from .custom_pools import get_pool
    if key != 'backgrounds' and not (key.startswith('pool:') and
            get_pool(editor.state, key[5:])['kind'] in {'video','visual'}): return
    path = source(editor, key)
    usable = bool(path and is_animated(path))
    menu.addAction('裁切视频…', lambda: edit(editor, key)).setEnabled(usable)
    menu.addAction('恢复整段视频', lambda: edit(editor, key, restore=True)).setEnabled(usable and bool(selected(editor.state['settings'], path)))
    menu.addSeparator()


def edit(editor, key, restore=False):
    if editor.is_batch_busy(): return
    path = source(editor, key)
    if not path or not is_animated(path): return
    if not editor.persist(): return
    rule = selected(editor.state['settings'], path)
    rules = [r for r in editor.state['settings'].get(KEY, []) if identity(r['path']) != identity(path)]
    if not restore:
        layer = dict(kind='image', source='fixed', path=path, media_loop_regions=[rule] if rule else [])
        dialog = LoopRegionDialog(layer, editor, trim_mode=True)
        try:
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted: return
            rules += copy.deepcopy(dialog.rules)
        finally: dialog.deleteLater()
    before = copy.deepcopy(editor.state['settings'].get(KEY, []))
    editor.state['settings'][KEY] = rules
    if not editor.persist(): editor.state['settings'][KEY] = before; return
    editor.refresh_pools()
    editor._preview_path = None
    editor.request_background(editor.active_entry().get('path', ''), force=True)
    editor.refresh_preview()
    editor.status.setText('已恢复整段背景视频。' if restore else '背景裁切已保存到当前项目。')
