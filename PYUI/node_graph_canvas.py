"""Qt node canvas. Single ports: circle/triangle; multi-inputs: long capsules."""
import math
from qt_compat import QtCore, QtGui, QtWidgets
from model.node_workflow.reroute import ROUTE_KINDS, insert_reroute, remove_reroute
from .node_connection_paths import connection_path, closest_point
from model.node_workflow.typing import resolve_port_types
from model.node_workflow.preview import is_preview
from model.node_workflow.workflow_types import output_kinds
from model.node_workflow.loop_regions import loop_regions
from .node_loop_regions import LoopRegionItem
from model.node_workflow.port_colors import TYPE_COLORS as COLORS, color_for
from .node_annotations import AnnotationItem


LOOP_PORT_LABELS = {
    'core.loop_begin': {'initial': '初始数据', 'items': '遍历列表', 'block': '循环块 → End',
        'value': '当前数据', 'item': '当前项', 'iteration': '迭代索引 (0)',
        'index': '第几次 (1)', 'number': '步进数值'},
    'core.loop_end': {'block': 'Begin 配对', 'result': '本轮结果', 'stop': '停止条件',
        'count': '执行次数', 'last': '最终结果', 'results': '结果集合', 'iterations': '实际次数'},
}


class PortItem(QtWidgets.QGraphicsObject):
    def __init__(self, node, port, output, index):
        super().__init__(node)
        self.node, self.port, self.output = node, port, output
        if getattr(node, 'orientation', 'horizontal') == 'vertical':
            self.setPos(60 + index * 100, node.height if output else 0)
        else:
            self.setPos(node.WIDTH if output else 0, 55 + index * getattr(node, "row_spacing", 27))
        self.setToolTip(f"{port.title} · {port.data_type} · " +
                        self.connection_hint())
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setZValue(4)
        self.setAcceptHoverEvents(True)
        self.hovered = False
        self.snap_state = ''

    def connected(self):
        scene = self.scene()
        if scene is None:
            return False
        return any((edge[0] == self.node.data['id'] and edge[1] == self.port.key) if self.output else
                   (edge[2] == self.node.data['id'] and edge[3] == self.port.key)
                   for edge in scene.graph['connections'])

    def hoverEnterEvent(self, event):
        self.hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def effective_type(self):
        scene = self.scene()
        return (scene.port_types.get((self.node.data["id"], self.output, self.port.key)) if scene else None) or self.port.data_type

    def connection_hint(self):
        if self.output:
            return "输出"
        return ("必须输入" if self.is_required() else "可选输入") + (" · 可连接多个（按接线顺序）" if self.port.multiple else "") + (
            " · 已连接" if self.connected() else " · 未连接")

    def is_required(self):
        if self.port.required:
            return True
        scene = self.scene()
        if self.output or scene is None:
            return False
        data = self.node.data
        if data["kind"] == "core.loop_begin":
            if self.port.key == "items":
                return data["params"].get("mode", "count") == "foreach"
            if self.port.key == "initial":
                return any(edge[0] == data["id"] and edge[1] == "value" for edge in scene.graph["connections"])
        if data["kind"] == "core.loop_end" and self.port.key == "stop":
            from model.node_workflow.loops import paired_blocks
            pairs, _ends, _errors = paired_blocks(scene.graph)
            begin = scene.nodes.get(pairs.get(data["id"]))
            return begin is not None and begin.data["params"].get("mode", "count") == "while"
        return False

    def refresh_type(self):
        dtype = self.effective_type()
        self.setToolTip(f"{self.port.title} · {dtype if dtype != 'any' else '接线后确定类型'} · " +
            self.connection_hint())
        self.update()

    def boundingRect(self):
        if not self.output and self.port.multiple:
            return QtCore.QRectF(-10, -19, 20, 38)
        return QtCore.QRectF(-10, -10, 20, 20)

    def shape(self):
        path = QtGui.QPainterPath()
        if not self.output and self.port.multiple:
            path.addRoundedRect(self.boundingRect(), 10, 10)
        else:
            path.addEllipse(self.boundingRect())
        return path

    def paint(self, painter, _option, _widget=None):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        color = QtGui.QColor(COLORS.get(self.effective_type(), "#cccccc"))
        if self.snap_state == 'invalid': color = QtGui.QColor('#ff626e')
        elif self.snap_state == 'valid': color = color.lighter(145)
        painter.setPen(QtGui.QPen(color.lighter(130) if self.hovered else color, 3 if self.snap_state else 2))
        painter.setBrush(color if self.output or self.connected() else QtGui.QColor("#192331"))
        if not self.output and self.port.multiple:
            pen = painter.pen()
            if not self.is_required():
                pen.setStyle(QtCore.Qt.PenStyle.DashLine)
                painter.setPen(pen)
            painter.drawRoundedRect(QtCore.QRectF(-7, -17, 14, 34), 7, 7)
            painter.setPen(QtGui.QPen(QtGui.QColor("#263343"), 1.5))
            painter.drawLine(QtCore.QPointF(-3, 0), QtCore.QPointF(3, 0))
            painter.drawLine(QtCore.QPointF(0, -3), QtCore.QPointF(0, 3))
        elif not self.output and not self.is_required():
            points = [QtCore.QPointF(-6,-6), QtCore.QPointF(7,0), QtCore.QPointF(-6,6)]
            if getattr(self.node, 'orientation', 'horizontal') == 'vertical':
                points = [QtCore.QPointF(-point.y(), point.x()) for point in points]
            painter.drawPolygon(QtGui.QPolygonF(points))
        else:
            painter.drawEllipse(QtCore.QRectF(-7,-7,14,14))

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._pressed_screen = event.screenPos()
            self.scene().port_pressed(self)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.scene().move_wire(event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event):
        moved = event.screenPos() - getattr(self, "_pressed_screen", event.screenPos())
        dragged = moved.manhattanLength() >= QtWidgets.QApplication.startDragDistance()
        if dragged:
            self.scene().finish_wire(event.scenePos())
        event.accept()


class NodeItem(QtWidgets.QGraphicsObject):
    WIDTH = 245

    @staticmethod
    def layout_metrics(definition, params):
        inputs = definition.input_ports(params)
        spacing = 40 if any(port.multiple for port in inputs) else 27
        return inputs, spacing, 86 + max(len(inputs), len(definition.output_ports(params)), 1) * spacing

    def __init__(self, scene, data, definition):
        super().__init__()
        self.data, self.definition = data, definition
        appearance = data.get('appearance', {})
        order = appearance.get('stack_order', 0) if isinstance(appearance, dict) else 0
        self.setZValue(order if type(order) in (int, float) and math.isfinite(order) and abs(order) <= 1000000 else 0)
        self.errors = []
        self.ports = {}
        self.inputs, self.row_spacing, self.height = self.layout_metrics(definition, data['params'])
        self.outputs = definition.output_ports(data['params'])
        self.orientation = scene.graph.get('view', {}).get('orientation', 'horizontal')
        if self.orientation == 'vertical':
            self.WIDTH, self.height = max(245, max(len(self.inputs), len(self.outputs))*100+20), 190
        self.setFlags(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        for output, ports in ((False, self.inputs), (True, self.outputs)):
            for index, port in enumerate(ports):
                self.ports[(output, port.key)] = PortItem(self, port, output, index)
        scene.addItem(self)
        self.setPos(*data.get("position", [0,0]))
        from model.node_workflow.loops import BEGIN, END, paired_blocks
        tooltip = definition.description
        if data["kind"] in {BEGIN, END}:
            by_end, by_begin, _errors = paired_blocks(scene.graph)
            paired = by_begin.get(data["id"]) if data["kind"] == BEGIN else by_end.get(data["id"])
            tooltip += "\n配对节点：" + (paired[:8] if paired else "尚未连接")
        self.base_tooltip = tooltip
        self.setToolTip(tooltip)

    def boundingRect(self):
        return QtCore.QRectF(-2, -2, self.WIDTH+4, self.height+4)

    def paint(self, painter, _option, _widget=None):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        border = "#ff7a89" if self.errors else "#83b5ff" if self.isSelected() else "#62748a"
        painter.setPen(QtGui.QPen(QtGui.QColor(border), 2))
        region = self.scene().region_for_node(self.data['id'])
        fill = QtGui.QColor(region.region.color).darker(270) if region is not None and self.data["kind"] in {
            "core.loop_begin", "core.loop_end"} else QtGui.QColor("#273446")
        painter.setBrush(fill)
        painter.drawRoundedRect(QtCore.QRectF(0,0,self.WIDTH,self.height), 9,9)
        painter.setPen(QtGui.QColor("#f4f7ff"))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QtCore.QRectF(12,5,self.WIDTH-24,30), QtCore.Qt.AlignmentFlag.AlignVCenter,
                         self.definition.title)
        font.setBold(False)
        painter.setFont(font)
        for output, ports in ((False,self.inputs),(True,self.outputs)):
            for index, port in enumerate(ports):
                flags = QtCore.Qt.AlignmentFlag.AlignRight if output else QtCore.Qt.AlignmentFlag.AlignLeft
                # Port tooltips carry full names; keep overlapping columns short.
                text = LOOP_PORT_LABELS.get(self.data['kind'], {}).get(port.key, port.title.split("（")[0])
                only_outputs = output and not self.inputs
                rect = QtCore.QRectF(14 if not output or only_outputs else self.WIDTH/2, 43+index*self.row_spacing,
                                    self.WIDTH-28 if only_outputs else self.WIDTH/2-20,24)
                if self.orientation == 'vertical':
                    rect = QtCore.QRectF(15+index*100, self.height-65 if output else 42, 90, 24)
                    flags = QtCore.Qt.AlignmentFlag.AlignHCenter
                text = painter.fontMetrics().elidedText(text,QtCore.Qt.TextElideMode.ElideRight,int(rect.width()))
                painter.drawText(rect, flags | QtCore.Qt.AlignmentFlag.AlignVCenter, text)
        painter.setPen(QtGui.QColor("#ff9caa" if self.errors else "#b3c6de"))
        loop_caption = ("循环方式：" + {"count": "按次数", "foreach": "逐项遍历", "while": "条件循环"}.get(self.data["params"].get("mode", "count"), "无效")
                        if self.data["kind"] == "core.loop_begin" else
                        "上限：" + str(self.data["params"].get("limit", 1000)) + " 次 · 支持取消"
                        if self.data["kind"] == "core.loop_end" else "")
        painter.drawText(QtCore.QRectF(12,self.height-28,self.WIDTH-24,20),
                         QtCore.Qt.AlignmentFlag.AlignLeft,
                         "双击打开预览内容" if self.can_preview() else
                         "需修正 · 详见右侧" if self.errors else loop_caption or "拖动节点 · 点击端口连线")

    def can_preview(self):
        from model.node_workflow.workflow_types import can_preview_operations
        scene = self.scene()
        return is_preview(self.data['kind']) or bool(scene and can_preview_operations(scene.graph, scene.registry, self.data))

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.data['kind'] == 'core.subgraph':
            self.scene().subgraphRequested.emit(self.data['id']); event.accept(); return
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.can_preview():
            self.scene().previewRequested.emit(self.data["id"])
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and self.scene():
            self.scene().update_edges()
            self.scene().preview_insertion(self)
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self._drag_origin = QtCore.QPointF(self.pos())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if (self.pos() - getattr(self, '_drag_origin', self.pos())).manhattanLength() > 2:
            self.scene().complete_insertion(self, event.modifiers())
        changed = False
        for item in self.scene().selectedItems() + [self]:
            if not isinstance(item, (NodeItem, AnnotationItem)):
                continue
            position = [round(item.pos().x(),2),round(item.pos().y(),2)]
            if item.data.get("position") != position:
                item.data["position"] = position
                changed = True
        if changed:
            self.scene().setSceneRect(self.scene().itemsBoundingRect().adjusted(-150,-150,150,150))
            self.scene().graphChanged.emit()

    def contextMenuEvent(self, event):
        if not self.isSelected():
            self.scene().clearSelection()
            self.setSelected(True)
        menu = QtWidgets.QMenu()
        copy_action = menu.addAction("复制选中节点（Ctrl+C）")
        cut_action = menu.addAction("剪切选中节点（Ctrl+X）")
        duplicate_action = menu.addAction("复制一份（Ctrl+D）")
        menu.addSeparator()
        preview = menu.addAction("打开预览内容" if is_preview(self.data['kind']) else '纯色模拟此效果') if self.can_preview() else None
        remove = menu.addAction("删除节点")
        try:
            valid_outputs = output_kinds(self.scene().graph, self.scene().registry)
        except ValueError:
            valid_outputs = set()
        output = menu.addAction("设为输出节点") if self.data["kind"] in valid_outputs else None
        region = self.scene().region_for_node(self.data['id'])
        pair = menu.addAction("定位配对 Begin / End") if region is not None and self.data['kind'] in {
            'core.loop_begin', 'core.loop_end'} else None
        frame = menu.addAction("查看整个循环范围") if region is not None else None
        color = menu.addAction("循环区域颜色…") if region is not None else None
        selected = menu.exec(event.screenPos())
        clipboard_actions = {copy_action: 'copy', cut_action: 'cut', duplicate_action: 'duplicate'}
        if selected in clipboard_actions:
            scene, operation = self.scene(), clipboard_actions[selected]
            QtCore.QTimer.singleShot(0, lambda: scene.clipboardRequested.emit(operation, None))
        elif preview is not None and selected == preview:
            self.scene().previewRequested.emit(self.data["id"])
        elif selected == remove:
            self.scene().remove_node(self.data["id"])
        elif output is not None and selected == output:
            self.scene().graph["output_node"] = self.data["id"]
            self.scene().graphChanged.emit()
        elif pair is not None and selected == pair:
            target = region.region.end if self.data['id'] == region.region.begin else region.region.begin
            self.scene().focus_node(target)
        elif frame is not None and selected == frame:
            for view in self.scene().views():
                view.fitInView(region.boundingRect().adjusted(-35, -35, 35, 35), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        elif color is not None and selected == color:
            chosen = QtWidgets.QColorDialog.getColor(QtGui.QColor(region.region.color),
                self.scene().views()[0] if self.scene().views() else None, '循环区域颜色')
            if chosen.isValid():
                self.scene().set_loop_color(region.region.begin, chosen.name())


class JunctionPort(PortItem):
    def boundingRect(self):
        return QtCore.QRectF(-6, -6, 12, 12)

    def paint(self, painter, _option, _widget=None):
        painter.setPen(QtGui.QPen(QtGui.QColor("#15202c"), 1))
        painter.setBrush(QtGui.QColor(COLORS.get(self.effective_type(), "#cccccc")))
        painter.drawEllipse(QtCore.QRectF(-3, -3, 6, 6))


class JunctionItem(NodeItem):
    """Compact movable dot with separately connectable input and output."""
    WIDTH = 28

    def __init__(self, scene, data, definition):
        QtWidgets.QGraphicsObject.__init__(self)
        self.data, self.definition, self.errors, self.ports = data, definition, [], {}
        self.height = 20
        self.orientation = scene.graph.get('view', {}).get('orientation', 'horizontal')
        self.setFlags(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        for output, port in ((False, definition.inputs[0]), (True, definition.outputs[0])):
            item = JunctionPort(self, port, output, 0)
            item.setPos(0, 14 if output else -14) if self.orientation == 'vertical' else item.setPos(14 if output else -14, 0)
            self.ports[(output, port.key)] = item
        scene.addItem(self)
        self.setPos(*data.get("position", [0, 0]))
        self.setToolTip(definition.description)
        self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)

    def boundingRect(self):
        return QtCore.QRectF(-22, -14, 44, 28)

    def shape(self):
        path = QtGui.QPainterPath()
        path.addEllipse(QtCore.QRectF(-9, -9, 18, 18))
        return path

    def paint(self, painter, _option, _widget=None):
        color = QtGui.QColor(COLORS.get(self.ports[(True, "value")].effective_type(), "#cccccc"))
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(color, 2))
        if self.orientation == 'vertical':
            painter.drawLine(QtCore.QPointF(0, -14), QtCore.QPointF(0, 14))
        else:
            painter.drawLine(QtCore.QPointF(-14, 0), QtCore.QPointF(14, 0))
        painter.setBrush(color)
        painter.setPen(QtGui.QPen(QtGui.QColor("#ff7a89" if self.errors else "#ffffff" if self.isSelected() else "#192331"), 2))
        painter.drawEllipse(QtCore.QRectF(-7, -7, 14, 14))


class EdgeItem(QtWidgets.QGraphicsPathItem):
    def __init__(self, scene, edge):
        super().__init__()
        self.edge = edge
        self.setZValue(-1)
        self.setPen(QtGui.QPen(QtGui.QColor("#a2b8d7"), 3))
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.hovered, self.insertion_preview = False, False
        scene.addItem(self)
        self.setToolTip("Alt + 左键添加中间接点；右键断开连接 / 调整操作顺序")

    def shape(self):
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(16)
        return stroker.createStroke(self.path())

    def boundingRect(self):
        # Qt's spatial index must include the enlarged, invisible hit stroke.
        return self.shape().boundingRect()

    def paint(self, painter, _option, _widget=None):
        # Like NodeGraphQt's PipeItem: draw the curve, not Qt's huge selection box.
        painter.save()
        scene = self.scene()
        kind = scene.port_types.get((self.edge[0], True, self.edge[1]), 'any') if hasattr(scene, 'port_types') else 'any'
        color = QtGui.QColor(color_for(kind))
        if self.isSelected() or self.hovered or self.insertion_preview: color = color.lighter(145)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(color, 4 if self.isSelected() or self.insertion_preview else 3))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())
        painter.restore()

    def hoverEnterEvent(self, event):
        self.hovered = True; self.update(); super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.hovered = False; self.update(); super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and event.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier:
            self._junction_click = closest_point(self.path(), event.scenePos())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        position = getattr(self, "_junction_click", None)
        if position is not None and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._junction_click = None
            # Wait for release before scheduling replacement of the mouse-grabbed edge.
            self.scene().insert_junction(self.edge, position)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu()
        remove = menu.addAction("断开连接")
        reinput = menu.addAction('重接下游输入端（保留上游）')
        reoutput = menu.addAction('重接上游输出端（保留下游）')
        earlier = menu.addAction("在此输入端提前执行")
        later = menu.addAction("在此输入端稍后执行")
        selected = menu.exec(event.screenPos())
        scene = self.scene()
        edges = scene.graph["connections"]
        if selected == remove:
            edges.remove(self.edge)
        elif selected in (reinput, reoutput):
            a, out, b, inp = self.edge
            fixed = scene.nodes[a].ports[(True, out)] if selected == reinput else scene.nodes[b].ports[(False, inp)]
            scene.begin_wire(fixed, replacing=(self.edge,))
            return
        elif selected in (earlier, later):
            same = [i for i,e in enumerate(edges) if e[2:] == self.edge[2:]]
            index = edges.index(self.edge)
            position = same.index(index) + (-1 if selected == earlier else 1)
            if 0 <= position < len(same):
                other = same[position]
                edges[index],edges[other] = edges[other],edges[index]
        else:
            return
        scene.schedule_refresh()


class NodeScene(QtWidgets.QGraphicsScene):
    subgraphRequested = QtCore.pyqtSignal(str)
    graphChanged = QtCore.pyqtSignal()
    connectionError = QtCore.pyqtSignal(str)
    previewRequested = QtCore.pyqtSignal(str)
    clipboardRequested = QtCore.pyqtSignal(str, object)

    def __init__(self, graph, registry, parent=None):
        super().__init__(parent)
        self.graph, self.registry = graph, registry
        self.nodes, self.edges = {}, []
        self.regions = {}
        self.port_types = {}
        self.pending = None
        self.wire = None
        self.snap_target = None
        self.replacing = ()
        self.insertion_target = None
        self._select_after_refresh = None
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self.refresh_changed)
        self.rebuild()

    def schedule_refresh(self):
        # Do not destroy a QGraphicsItem while Qt is dispatching its mouse event.
        self.refresh_timer.start(0)

    def append_nodes(self, records, *, select=None):
        """Incremental insertion: no reconstruction, layout, fit or navigation."""
        blocked = self.blockSignals(True)
        try:
            self.clearSelection()
            self.port_types, _ = resolve_port_types(self.graph, self.registry)
            top = max((item.zValue() for item in self.nodes.values()), default=0)
            for record in records:
                if record['id'] in self.nodes:
                    raise ValueError('节点已经在画布中，不能重复添加。')
                top += 1
                record.setdefault('appearance', {})['stack_order'] = top
                definition = self.registry.definitions[record['kind']]
                item_class = JunctionItem if record['kind'] in ROUTE_KINDS else NodeItem
                item = item_class(self, record, definition)
                item.setZValue(top)
                self.nodes[record['id']] = item
            known = {tuple(item.edge) for item in self.edges}
            for edge in self.graph['connections']:
                if tuple(edge) not in known:
                    self.edges.append(EdgeItem(self, edge))
            self.update_edges()
            if select in self.nodes:
                self.nodes[select].setSelected(True)
        finally:
            self.blockSignals(blocked)
        if not blocked:
            self.selectionChanged.emit()

    def refresh_changed(self):
        selected = ({self._select_after_refresh} if self._select_after_refresh else
                    {item.data["id"] for item in self.selectedItems() if isinstance(item, NodeItem)})
        self._select_after_refresh = None
        self.rebuild()
        for node_id in selected:
            if node_id in self.nodes:
                self.nodes[node_id].setSelected(True)
        self.graphChanged.emit()

    def rebuild(self):
        # Selection callbacks inspect port sources. Do not expose a half-built
        # scene while clearing multiple selected nodes or recreating fragments.
        blocked = self.blockSignals(True)
        self._building = True
        try:
            self.cancel_wire()
            self._rebuild_items()
        finally:
            self._building = False
            self.blockSignals(blocked)
        if not blocked:
            self.selectionChanged.emit()

    def _rebuild_items(self):
        centers = [(view, view.viewportTransform().inverted()[0].map(
                        QtCore.QPointF(view.viewport().width() / 2, view.viewport().height() / 2)),
                    view.transformationAnchor(),
                    (view.horizontalScrollBar().value(), view.verticalScrollBar().value()),
                    (view.horizontalScrollBar().minimum() != view.horizontalScrollBar().maximum(),
                     view.verticalScrollBar().minimum() != view.verticalScrollBar().maximum()))
                   for view in self.views()]
        previous_rect = self.sceneRect()
        self.pending = self.wire = None
        self.insertion_target = None
        self.nodes, self.edges = {}, []
        self.annotations = {}
        self.regions = {}
        self.clear()
        from model.node_workflow.contracts import NodeDefinition
        self.port_types, _ = resolve_port_types(self.graph, self.registry)
        for node in self.graph["nodes"]:
            definition = self.registry.definitions.get(node["kind"]) or NodeDefinition(node["kind"],"不可用："+node["kind"])
            item_class = JunctionItem if node["kind"] in ROUTE_KINDS and definition.inputs and definition.outputs else NodeItem
            self.nodes[node["id"]] = item_class(self,node,definition)
        self.edges = [EdgeItem(self,edge) for edge in self.graph["connections"]]
        for data in self.graph.get('annotations', []):
            self.annotations[data['id']] = AnnotationItem(self, data)
        self.refresh_annotations()
        self.refresh_regions()
        self.update_edges()
        # Shrinking scene bounds re-centers hidden scrollbars and makes all nodes
        # appear to jump. Keep the navigable range and restore each view's focus.
        self.setSceneRect(previous_rect.united(self.itemsBoundingRect().adjusted(-150,-150,150,150)))
        for view, center, anchor, scrolls, ranged in centers:
            view.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.NoAnchor)
            view.centerOn(center)
            # rect().center() is half a pixel off Qt's geometric viewport center.
            # Preserve integer offsets too, avoiding accumulated rounding drift.
            for bar, value, was_ranged in zip((view.horizontalScrollBar(), view.verticalScrollBar()), scrolls, ranged):
                if was_ranged:
                    bar.setValue(value)
            view.setTransformationAnchor(anchor)

    def path_between(self, start, end, style="smooth"):
        return connection_path(start, end, style, self.graph.get('view', {}).get('orientation', 'horizontal'))

    def connection_style(self):
        return self.graph.get("view", {}).get("connection_style", "smooth")

    def set_connection_style(self, style):
        from .node_connection_paths import WIRE_STYLES
        if style not in dict(WIRE_STYLES):
            self.connectionError.emit("未知连线样式。")
            return
        if style == self.connection_style():
            return
        self.graph.setdefault("view", {})["connection_style"] = style
        self.update_edges()
        if self.pending is not None and self.wire is not None:
            end = self.wire.path().currentPosition()
            self.move_wire(end)
        self.graphChanged.emit()

    def update_edges(self):
        for node in self.nodes.values():
            for port in node.ports.values():
                port.refresh_type()
        for edge in self.edges:
            a,out,b,inp = edge.edge
            first = self.nodes.get(a)
            second = self.nodes.get(b)
            if first and second and (True,out) in first.ports and (False,inp) in second.ports:
                edge.setPath(self.path_between(first.ports[(True,out)].scenePos(),second.ports[(False,inp)].scenePos(), self.connection_style()))
        self.refresh_regions()

    def refresh_annotations(self):
        hidden = {key for data in self.graph.get('annotations', []) if data.get('collapsed') for key in data.get('members', [])}
        for key, node in self.nodes.items():
            node.setVisible(key not in hidden)
        for edge in self.edges:
            edge.setVisible(edge.edge[0] not in hidden and edge.edge[2] not in hidden)

    def refresh_regions(self):
        # Membership comes from wires, never from spatial overlap with the blob.
        definitions = loop_regions(self.graph)
        maximum_depth = max((region.depth for region in definitions), default=0)
        wanted = {region.begin for region in definitions}
        for node_id in set(self.regions) - wanted:
            self.removeItem(self.regions.pop(node_id))
        for definition in definitions:
            item = self.regions.get(definition.begin)
            if item is None:
                item = self.regions[definition.begin] = LoopRegionItem(self, definition)
            else:
                item.region = definition
                item.setZValue(-20 + min(definition.depth, 16))
            item.refresh(self.nodes, self.edges, maximum_depth)

    def region_for_node(self, node_id):
        candidates = [item for item in self.regions.values() if node_id in item.region.members]
        return max(candidates, key=lambda item: item.region.depth, default=None)

    def focus_node(self, node_id):
        item = self.nodes.get(node_id)
        if item is not None:
            self.clearSelection()
            item.setSelected(True)
            for view in self.views():
                view.centerOn(item)

    def set_loop_color(self, begin_id, color):
        chosen = QtGui.QColor(color)
        if begin_id not in self.regions or not chosen.isValid():
            return
        node = self.nodes[begin_id].data
        if not isinstance(node.get('appearance'), dict):
            node['appearance'] = {}
        node['appearance']['loop_color'] = chosen.name()
        self.refresh_regions()
        for item in self.nodes.values():
            item.update()
        self.graphChanged.emit()

    def insert_junction(self, edge, position):
        try:
            node = insert_reroute(self.graph, self.registry, edge, [position.x(), position.y()])
            self.cancel_wire()
            self._select_after_refresh = node["id"]
            self.schedule_refresh()
            return node
        except (ValueError, KeyError, TypeError) as error:
            self.connectionError.emit(str(error))
            return None

    def port_pressed(self, port):
        if self.pending is None:
            self.begin_wire(port)
            return
        if port.output == self.pending.output:
            self.cancel_wire()
            self.begin_wire(port)
            return
        try:
            self.graph['connections'] = self.candidate(port)
            self.cancel_wire()
            self.schedule_refresh()
        except ValueError as error:
            self.cancel_wire(); self.connectionError.emit(str(error))

    def candidate(self, port):
        from model.node_workflow.editing import connection_candidate
        if port.output == self.pending.output: raise ValueError('请连接一个输出端和一个输入端。')
        source, target = (self.pending, port) if self.pending.output else (port, self.pending)
        return connection_candidate(self.graph, self.registry,
            [source.node.data['id'], source.port.key], [target.node.data['id'], target.port.key], replacing=self.replacing)

    def begin_wire(self, port, replacing=()):
        self.cancel_wire()
        self.pending, self.replacing = port, replacing
        self.wire = self.addPath(QtGui.QPainterPath(), QtGui.QPen(QtGui.QColor(color_for(port.effective_type())), 3))
        self.wire.setZValue(10)
        self.move_wire(port.scenePos())

    def nearest_port(self, position):
        if self.pending is None: return None
        view = self.views()[0] if self.views() else None
        transform = view.viewportTransform() if view else QtGui.QTransform()
        point = transform.map(position)
        candidates = []
        for node in self.nodes.values():
            for port in node.ports.values():
                if port is self.pending or port.output == self.pending.output or not port.isVisible(): continue
                rect = transform.mapRect(port.mapRectToScene(port.boundingRect()))
                dx = max(rect.left()-point.x(), 0, point.x()-rect.right())
                dy = max(rect.top()-point.y(), 0, point.y()-rect.bottom())
                distance = math.hypot(dx, dy)
                if distance <= 14:
                    try: self.candidate(port); valid = True
                    except ValueError: valid = False
                    candidates.append((not valid, distance, -node.zValue(), port))
        return min(candidates, key=lambda item: item[:3])[3] if candidates else None

    def finish_wire(self, position):
        port = self.nearest_port(position)
        if port is None:
            self.cancel_wire()
        else:
            self.port_pressed(port)

    def move_wire(self, position):
        if self.wire is not None and self.pending is not None:
            if self.snap_target is not None:
                self.snap_target.snap_state = ''; self.snap_target.update()
            self.snap_target = self.nearest_port(position)
            if self.snap_target is not None:
                try: self.candidate(self.snap_target); state = 'valid'
                except ValueError: state = 'invalid'
                self.snap_target.snap_state = state; self.snap_target.update()
                position = self.snap_target.scenePos()
            first, second = (self.pending.scenePos(), position) if self.pending.output else (position, self.pending.scenePos())
            self.wire.setPath(self.path_between(first, second, self.connection_style()))

    def preview_insertion(self, item):
        if not hasattr(self, 'edges') or getattr(self, '_building', False): return
        from model.node_workflow.editing import insertion_options
        point = item.sceneBoundingRect().center()
        view = self.views()[0] if self.views() else None
        transform = view.viewportTransform() if view else QtGui.QTransform()
        possible = []
        alt = QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.KeyboardModifier.AltModifier
        for edge in self.edges:
            edge.insertion_preview = False
            if item.data['id'] in (edge.edge[0], edge.edge[2]): continue
            nearest = closest_point(edge.path(), point)
            distance = (transform.map(nearest) - transform.map(point)).manhattanLength()
            if distance <= 24 and ((item.data['kind'] in ROUTE_KINDS and alt) or insertion_options(self.graph, self.registry, item.data['id'], edge.edge)):
                possible.append((distance, edge))
            edge.update()
        self.insertion_target = min(possible, key=lambda value: value[0])[1] if possible else None
        if self.insertion_target:
            self.insertion_target.insertion_preview = True; self.insertion_target.update()

    def complete_insertion(self, item, modifiers):
        from model.node_workflow.editing import insertion_options, insert_node_on_edge, gather_reroute
        target = self.insertion_target
        self.insertion_target = None
        if target is None: return
        try:
            if item.data['kind'] in ROUTE_KINDS and modifiers & QtCore.Qt.KeyboardModifier.AltModifier:
                changed = gather_reroute(self.graph, item.data['id'], target.edge)
            else:
                options = insertion_options(self.graph, self.registry, item.data['id'], target.edge)
                if not options: return
                chosen = options[0]
                if len(options) > 1:
                    menu = QtWidgets.QMenu()
                    actions = {menu.addAction(a+' → '+b): (a, b) for a, b in options}
                    chosen = actions.get(menu.exec(QtGui.QCursor.pos()))
                    if chosen is None: return
                insert_node_on_edge(self.graph, self.registry, item.data['id'], target.edge, chosen)
                changed = True
            if changed: self.schedule_refresh()
        except ValueError as error:
            self.connectionError.emit(str(error))

    def mousePressEvent(self, event):
        if self.pending is not None and (event.button() == QtCore.Qt.MouseButton.RightButton or
                event.button() == QtCore.Qt.MouseButton.LeftButton and not any(
                    isinstance(item, PortItem) for item in self.items(event.scenePos()))):
            self.cancel_wire()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.move_wire(event.scenePos())
        super().mouseMoveEvent(event)

    def cancel_wire(self):
        if self.snap_target is not None:
            self.snap_target.snap_state = ''; self.snap_target.update()
        self.snap_target, self.replacing = None, ()
        if self.wire is not None:
            self.removeItem(self.wire)
        self.wire = self.pending = None

    def remove_node(self, node_id):
        for item in self.graph.get('annotations', []):
            item['members'] = [key for key in item.get('members', []) if key != node_id]
        node = next((n for n in self.graph["nodes"] if n["id"] == node_id), None)
        if node and node["kind"] in ROUTE_KINDS:
            try:
                remove_reroute(self.graph, node_id)
                self.schedule_refresh()
            except ValueError as error:
                self.connectionError.emit(str(error))
            return
        self.graph["nodes"] = [n for n in self.graph["nodes"] if n["id"] != node_id]
        self.graph["connections"] = [e for e in self.graph["connections"] if e[0] != node_id and e[2] != node_id]
        if self.graph["output_node"] == node_id:
            self.graph["output_node"] = ""
        self.schedule_refresh()


class NodeView(QtWidgets.QGraphicsView):
    searchRequested = QtCore.pyqtSignal(object)

    def __init__(self, scene, parent=None):
        super().__init__(scene,parent)
        self.setRenderHints(QtGui.QPainter.RenderHint.Antialiasing | QtGui.QPainter.RenderHint.TextAntialiasing)
        self.setBackgroundBrush(QtGui.QColor("#192331"))
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.NoAnchor)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._pan_position = None
        self.setToolTip("中键拖动画布；滚轮缩放；Alt 点击连线添加接点；双击预览节点查看内容。")

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.MiddleButton:
            self.scene().cancel_wire()
            self._pan_position = event.globalPosition()
            self._pan_center = self.mapToScene(self.viewport().rect().center())
            self._pan_inverse = self.transform().inverted()[0]
            self._pan_anchor = self.transformationAnchor()
            self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.NoAnchor)
            self.viewport().setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_position is not None:
            # Absolute movement from press, not cumulative rounded scrollbar deltas.
            delta = event.globalPosition() - self._pan_position
            center = self.mapToScene(self.viewport().rect().center())
            displacement = self._pan_inverse.map(delta) - self._pan_inverse.map(QtCore.QPointF())
            target = self._pan_center - displacement
            visible = self.mapToScene(self.viewport().rect()).boundingRect()
            wanted = visible.translated(target - center).adjusted(-400, -400, 400, 400)
            self.scene().setSceneRect(self.sceneRect().united(wanted))
            self.centerOn(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.MiddleButton and self._pan_position is not None:
            self._pan_position = None
            self.setTransformationAnchor(self._pan_anchor)
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def focusOutEvent(self, event):
        if self._pan_position is not None:
            self.setTransformationAnchor(self._pan_anchor)
        self._pan_position = None
        self.scene().cancel_wire()
        self.viewport().unsetCursor()
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        if self._pan_position is not None:
            event.accept()
            return
        factor = 1.15 if event.angleDelta().y() > 0 else 1/1.15
        if .25 < self.transform().m11()*factor < 3:
            self.scale(factor,factor)
        event.accept()

    def keyPressEvent(self, event):
        for key, operation in ((QtGui.QKeySequence.StandardKey.Copy, 'copy'),
                               (QtGui.QKeySequence.StandardKey.Cut, 'cut'),
                               (QtGui.QKeySequence.StandardKey.Paste, 'paste'),
                               (QtGui.QKeySequence.StandardKey.SelectAll, 'select_all')):
            if event.matches(key):
                self.scene().clipboardRequested.emit(operation, None)
                event.accept()
                return
        if event.key() == QtCore.Qt.Key.Key_D and event.modifiers() == QtCore.Qt.KeyboardModifier.ControlModifier:
            self.scene().clipboardRequested.emit('duplicate', None)
            event.accept()
        elif event.key() == QtCore.Qt.Key.Key_Tab:
            self.searchRequested.emit(self.mapToScene(self.viewport().rect().center()))
            event.accept()
        elif event.key() == QtCore.Qt.Key.Key_Escape:
            self.scene().cancel_wire()
        elif event.key() in (QtCore.Qt.Key.Key_Delete,QtCore.Qt.Key.Key_Backspace):
            ids = [item.data["id"] for item in self.scene().selectedItems() if isinstance(item,NodeItem)]
            edges = [item.edge for item in self.scene().selectedItems() if isinstance(item,EdgeItem)]
            annotations = [item.data['id'] for item in self.scene().selectedItems() if isinstance(item, AnnotationItem)]
            if annotations:
                self.scene().graph['annotations'] = [data for data in self.scene().graph.get('annotations', []) if data['id'] not in annotations]
            for node_id in ids:
                self.scene().remove_node(node_id)
            for edge in edges:
                if edge in self.scene().graph["connections"]:
                    self.scene().graph["connections"].remove(edge)
            if edges or annotations:
                self.scene().schedule_refresh()
        else:
            super().keyPressEvent(event)

    def event(self, event):
        if event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Tab:
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def contextMenuEvent(self, event):
        if self.itemAt(event.pos()) is not None:
            return super().contextMenuEvent(event)
        menu = QtWidgets.QMenu(self)
        add = menu.addAction("查找／添加节点…（Tab）")
        paste_action = menu.addAction("粘贴节点（Ctrl+V）")
        select_all = menu.addAction("全选节点（Ctrl+A）")
        selected = menu.exec(event.globalPos())
        if selected == add:
            self.searchRequested.emit(self.mapToScene(event.pos()))
        elif selected == paste_action:
            self.scene().clipboardRequested.emit('paste', self.mapToScene(event.pos()))
        elif selected == select_all:
            self.scene().clipboardRequested.emit('select_all', None)
        event.accept()
