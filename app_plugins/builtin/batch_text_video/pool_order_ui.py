"""Shared discoverable order menu for default and named resource pools."""
import copy
from qt_compat import QtCore, QtWidgets
from . import pool_order


def add_actions(host, menu, key):
    if key not in pool_order.libraries(host.state):
        return
    from .pool_exclusion_ui import add_action
    add_action(host, menu, key)
    busy = host.is_batch_busy()
    randomized = pool_order.active(host.state, key)
    action = menu.addAction('重新随机使用顺序' if randomized else '随机使用顺序',
                            lambda: change(host, key))
    action.setEnabled(not busy and len(host.resource_items(key)) > 1)
    action.setToolTip('保存一次随机排列；后续新分配按序号取用，已固定的任务不变。')
    action = menu.addAction('恢复原顺序', lambda: change(host, key, restore=True))
    action.setEnabled(not busy and randomized)


def install(host):
    host.pool_order_button = QtWidgets.QToolButton()
    host.pool_order_button.setText('顺序')
    host.pool_order_button.setToolTip('随机使用顺序／恢复原顺序')
    host.pool_order_button.setAccessibleName('资源池使用顺序')
    host.pool_order_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QtWidgets.QMenu(host.pool_order_button)
    def rebuild():
        menu.clear()
        add_actions(host, menu, host.visible_pool_key())
    menu.aboutToShow.connect(rebuild)
    host.pool_order_button.setMenu(menu)


def refresh(host):
    for key in pool_order.libraries(host.state):
        table = host.table_for(key)
        randomized = pool_order.active(host.state, key)
        signature = (randomized, table.rowCount())
        if getattr(table, '_usage_header_state', None) == signature:
            continue
        table._usage_header_state = signature
        labels = [('随机 ' if randomized else '')+str(i+1) for i in range(table.rowCount())]
        table.setVerticalHeaderLabels(labels)
        for row in range(table.rowCount()):
            table.verticalHeaderItem(row).setToolTip('使用序号；'+('已随机，可从“使用顺序”恢复原顺序。' if randomized else '按当前列表顺序使用。'))


def change(host, key, *, restore=False):
    if host.is_batch_busy() or key not in pool_order.libraries(host.state):
        return False
    host.edit_timer.stop()
    if not host.persist():
        return False
    before = copy.deepcopy(host.state)
    layers = host.layer_panel.values()
    try:
        if not pool_order.reorder(host.state, key, restore=restore):
            return False
        from .pools import bind_pool_layers
        bound = bind_pool_layers(pool_order.restart_layers(layers, key), host.state)
        with QtCore.QSignalBlocker(host.layer_panel):
            host.layer_panel.set_layers(bound)
        if not host.persist():
            host.state = before
            with QtCore.QSignalBlocker(host.layer_panel):
                host.layer_panel.set_layers(layers)
            host.refresh_tables()
            return False
    except Exception as error:
        host.state = before
        with QtCore.QSignalBlocker(host.layer_panel):
            host.layer_panel.set_layers(layers)
        host.refresh_tables()
        host.status.setText('顺序修改失败：'+str(error))
        return False
    host.refresh_tables()
    host.invalidate_playback()
    host.update_preview()
    host.refresh_editor_baseline()
    host.status.setText('已恢复原顺序。' if restore else '已随机分配使用序号。')
    return True
