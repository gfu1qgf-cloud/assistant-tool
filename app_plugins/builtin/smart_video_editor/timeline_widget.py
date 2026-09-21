from qt_compat import QtCore, QtGui, QtWidgets


_STATUS_COLORS = {
    "green": QtGui.QColor("#3F8F5A"),
    "orange": QtGui.QColor("#D88920"),
    "pink": QtGui.QColor("#C94B73"),
}
_ISSUE_COLORS = {
    "green": QtGui.QColor("#55B979"),
    "orange": QtGui.QColor("#F4A340"),
    "pink": QtGui.QColor("#FF5D8F"),
}


def format_time(seconds):
    value = max(0.0, float(seconds or 0.0))
    minutes = int(value // 60)
    remainder = value - minutes * 60
    return f"{minutes:02d}:{remainder:05.2f}"


class SmartTimelineWidget(QtWidgets.QWidget):
    seekRequested = QtCore.pyqtSignal(float)
    clipActivated = QtCore.pyqtSignal(int)
    clipReorderRequested = QtCore.pyqtSignal(int, int)
    removedSegmentRestoreRequested = QtCore.pyqtSignal(object)
    markerActivated = QtCore.pyqtSignal(object)
    subtitleActivated = QtCore.pyqtSignal(object)

    LABEL_WIDTH = 112
    VIDEO_TOP = 36
    VIDEO_HEIGHT = 35
    WAVE_TOP = 77
    WAVE_HEIGHT = 44
    RECOGNIZED_TOP = 128
    ALIGNED_TOP = 169
    SUBTITLE_HEIGHT = 34
    ISSUE_Y = 225

    def __init__(self, parent=None):
        super().__init__(parent)
        self.segments = []
        self.markers = []
        self.subtitle_tracks = {"recognized": [], "aligned": []}
        self.waveforms = {}
        self.duration = 0.0
        self.playhead = 0.0
        self.zoom = 1.0
        self.viewport_width = 900
        self._segment_rects = []
        self._subtitle_rects = []
        self._marker_points = []
        self._drag_clip_index = None
        self._drag_target_index = None
        self._drag_start = None
        self._dragging_clip = False
        self._last_playhead_pixel = None
        self.setMouseTracking(True)
        self.setMinimumHeight(260)
        self.setCursor(QtCore.Qt.PointingHandCursor)

    def set_data(
        self,
        segments,
        markers,
        duration,
        recognized_subtitles=None,
        aligned_subtitles=None,
    ):
        self.segments = list(segments or [])
        self.markers = list(markers or [])
        self.subtitle_tracks = {
            "recognized": list(recognized_subtitles or []),
            "aligned": list(aligned_subtitles or []),
        }
        self.duration = max(0.01, float(duration or 0.01))
        self.playhead = min(self.playhead, self.duration)
        self._update_width()
        self.update()

    def clear_waveforms(self):
        self.waveforms = {}
        self.update()

    def set_waveform(self, clip_index, points):
        self.waveforms[int(clip_index)] = list(points or [])
        self.update()

    def set_viewport_width(self, width):
        self.viewport_width = max(320, int(width or 320))
        self._update_width()

    def set_zoom(self, zoom, anchor_time=None):
        self.zoom = max(1.0, min(12.0, float(zoom or 1.0)))
        self._update_width()
        self.update()

    def set_playhead(self, seconds):
        value = max(0.0, min(self.duration, float(seconds or 0.0)))
        old_x = self._x_for_time(self.playhead)
        new_x = self._x_for_time(value)
        self.playhead = value
        new_pixel = int(round(new_x))
        if self._last_playhead_pixel == new_pixel:
            return
        self._last_playhead_pixel = new_pixel
        dirty_left = max(0, int(min(old_x, new_x)) - 9)
        dirty_width = max(20, int(abs(new_x - old_x)) + 18)
        self.update(dirty_left, 23, dirty_width, 226)

    def _pixels_per_second(self):
        usable = max(260, self.viewport_width - self.LABEL_WIDTH - 10)
        return max(3.0, usable / max(0.01, self.duration)) * self.zoom

    def _update_width(self):
        width = int(
            self.LABEL_WIDTH + self.duration * self._pixels_per_second() + 28
        )
        self.setMinimumWidth(max(self.viewport_width, width))
        self.resize(max(self.viewport_width, width), self.height())

    def _x_for_time(self, seconds):
        return self.LABEL_WIDTH + float(seconds or 0.0) * self._pixels_per_second()

    def _time_for_x(self, x):
        return max(
            0.0,
            min(
                self.duration,
                (float(x) - self.LABEL_WIDTH) / self._pixels_per_second(),
            ),
        )

    def _draw_track_background(self, painter, top, height, label):
        rect = QtCore.QRectF(
            self.LABEL_WIDTH,
            top,
            max(1.0, self._x_for_time(self.duration) - self.LABEL_WIDTH),
            height,
        )
        painter.fillRect(rect, QtGui.QColor("#1B1F29"))
        painter.setPen(QtGui.QColor("#AAB2C3"))
        painter.drawText(
            QtCore.QRectF(5, top, self.LABEL_WIDTH - 10, height),
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
            label,
        )

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        dirty = event.rect()
        visible_left = float(dirty.left() - 4)
        visible_right = float(dirty.right() + 4)
        painter.fillRect(dirty, QtGui.QColor("#151820"))

        for top, height, label in (
            (self.VIDEO_TOP, self.VIDEO_HEIGHT, "视频 / 删除区"),
            (self.WAVE_TOP, self.WAVE_HEIGHT, "音频波形"),
            (self.RECOGNIZED_TOP, self.SUBTITLE_HEIGHT, "模型识别字幕"),
            (self.ALIGNED_TOP, self.SUBTITLE_HEIGHT, "任务文本对齐"),
        ):
            self._draw_track_background(painter, top, height, label)
        painter.setPen(QtGui.QColor("#AAB2C3"))
        painter.drawText(
            QtCore.QRectF(5, self.ISSUE_Y - 14, self.LABEL_WIDTH - 10, 28),
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
            "问题标记",
        )

        pixels = self._pixels_per_second()
        major_step = 1
        while major_step * pixels < 70:
            major_step *= 2 if major_step < 10 else 3
        tick = 0.0
        painter.setPen(QtGui.QPen(QtGui.QColor("#343A49"), 1))
        while tick <= self.duration + 0.001:
            x = self._x_for_time(tick)
            if visible_left <= x <= visible_right:
                painter.drawLine(QtCore.QPointF(x, 27), QtCore.QPointF(x, 242))
                painter.setPen(QtGui.QColor("#8B93A7"))
                painter.drawText(QtCore.QRectF(x + 3, 7, 72, 17), format_time(tick))
                painter.setPen(QtGui.QPen(QtGui.QColor("#343A49"), 1))
            tick += major_step

        self._segment_rects = []
        for segment in self.segments:
            left = self._x_for_time(segment["timeline_start"])
            right = self._x_for_time(segment["timeline_end"])
            rect = QtCore.QRectF(
                left + 1,
                self.VIDEO_TOP,
                max(3.0, right - left - 2),
                self.VIDEO_HEIGHT,
            )
            self._segment_rects.append((rect, segment))
            if rect.right() < visible_left or rect.left() > visible_right:
                continue
            if segment.get("is_removed"):
                color = QtGui.QColor("#555B66")
            else:
                color = _STATUS_COLORS.get(
                    segment.get("status"), _STATUS_COLORS["green"]
                )
                if segment.get("review_acknowledged"):
                    color = QtGui.QColor("#357A4C")
            painter.setPen(QtGui.QPen(color.lighter(140), 1))
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 3, 3)
            if segment.get("duplicate_group_id"):
                duplicate_border = QtGui.QColor(
                    "#64D8FF"
                    if segment.get("duplicate_selected")
                    else "#FFD166"
                )
                painter.setPen(QtGui.QPen(duplicate_border, 2))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawRoundedRect(
                    rect.adjusted(1, 1, -1, -1), 3, 3
                )
            if (
                self._dragging_clip
                and self._drag_target_index is not None
                and int(segment["clip_index"]) == int(self._drag_target_index)
            ):
                painter.setPen(QtGui.QPen(QtGui.QColor("#FFD54F"), 3))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            label = segment.get("file_name") or f"片段 {segment['clip_index'] + 1}"
            if segment.get("auto_excluded_duplicate"):
                label = f"重复候选（未选）：{label}"
            elif segment.get("duplicate_group_id") and segment.get("is_removed"):
                label = (
                    "重复组·已选 / 删除："
                    f"{segment.get('remove_reason') or '气口'}"
                )
            elif segment.get("duplicate_group_id"):
                label = f"重复组·已选：{label}"
            elif segment.get("is_removed"):
                label = f"删除：{segment.get('remove_reason') or '气口'}"
            painter.setPen(QtGui.QColor("#FFFFFF"))
            painter.drawText(
                rect.adjusted(5, 0, -4, 0),
                QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                painter.fontMetrics().elidedText(
                    label, QtCore.Qt.ElideRight, max(1, int(rect.width() - 9))
                ),
            )

        center = self.WAVE_TOP + self.WAVE_HEIGHT / 2.0
        for segment in self.segments:
            points = self.waveforms.get(int(segment["clip_index"]), [])
            if not points:
                continue
            segment_left = self._x_for_time(segment["timeline_start"])
            segment_right = self._x_for_time(segment["timeline_end"])
            if segment_right < visible_left or segment_left > visible_right:
                continue
            painter.setPen(QtGui.QPen(
                QtGui.QColor("#707783")
                if segment.get("is_removed")
                else QtGui.QColor("#59B6D9"),
                1,
            ))
            # Keep at most one (largest) line per screen pixel.  Long source
            # files otherwise cause thousands of redundant draw calls for
            # every playhead update.
            visible_points = {}
            for source_time, amplitude in points:
                if not (
                    segment["source_start"] <= source_time <= segment["source_end"]
                ):
                    continue
                output_time = (
                    segment["timeline_start"]
                    + source_time
                    - segment["source_start"]
                )
                x = self._x_for_time(output_time)
                if x < visible_left or x > visible_right:
                    continue
                pixel = int(round(x))
                previous = visible_points.get(pixel)
                if previous is None or float(amplitude) > previous:
                    visible_points[pixel] = float(amplitude)
            for pixel, amplitude in visible_points.items():
                half = max(1.0, float(amplitude) * (self.WAVE_HEIGHT / 2.0 - 3))
                painter.drawLine(
                    QtCore.QPointF(pixel, center - half),
                    QtCore.QPointF(pixel, center + half),
                )

        self._subtitle_rects = []
        for kind, top, default_color in (
            ("recognized", self.RECOGNIZED_TOP, QtGui.QColor("#326C8C")),
            ("aligned", self.ALIGNED_TOP, QtGui.QColor("#66539A")),
        ):
            for block in self.subtitle_tracks.get(kind, []):
                left = self._x_for_time(block["timeline_start"])
                right = self._x_for_time(block["timeline_end"])
                block_width = max(4.0, right - left - 2)
                if block.get("is_missing"):
                    # Missing source text has no real audio duration.  Give its
                    # warning enough visual width to remain readable at fit
                    # zoom while keeping it anchored to the detected gap.
                    block_width = max(150.0, block_width)
                    timeline_right = self._x_for_time(self.duration) - 2
                    left = max(
                        self.LABEL_WIDTH,
                        min(left, timeline_right - block_width),
                    )
                rect = QtCore.QRectF(
                    left + 1,
                    top,
                    block_width,
                    self.SUBTITLE_HEIGHT,
                )
                self._subtitle_rects.append((rect, block))
                if rect.right() < visible_left or rect.left() > visible_right:
                    continue
                severity = block.get("severity", "green")
                if block.get("is_missing"):
                    color = QtGui.QColor("#B71C1C")
                else:
                    color = (
                        _ISSUE_COLORS[severity].darker(125)
                        if severity in {"orange", "pink"}
                        else default_color
                    )
                painter.setPen(QtGui.QPen(
                    color.lighter(165), 2 if block.get("is_missing") else 1
                ))
                painter.setBrush(color)
                painter.drawRoundedRect(rect, 3, 3)
                painter.setPen(QtGui.QColor("#FFFFFF"))
                painter.drawText(
                    rect.adjusted(5, 0, -4, 0),
                    QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                    painter.fontMetrics().elidedText(
                        block.get("text") or "（空）",
                        QtCore.Qt.ElideRight,
                        max(1, int(rect.width() - 9)),
                    ),
                )

        painter.setPen(QtGui.QPen(QtGui.QColor("#343A49"), 2))
        painter.drawLine(
            QtCore.QPointF(self.LABEL_WIDTH, self.ISSUE_Y),
            QtCore.QPointF(self._x_for_time(self.duration), self.ISSUE_Y),
        )
        self._marker_points = []
        for marker in self.markers:
            x = self._x_for_time(marker["time"])
            point = QtCore.QPointF(x, self.ISSUE_Y)
            self._marker_points.append((point, marker))
            if x < visible_left or x > visible_right:
                continue
            color = _ISSUE_COLORS.get(
                marker.get("severity"), _ISSUE_COLORS["orange"]
            )
            painter.setPen(QtGui.QPen(color, 2))
            painter.setBrush(color)
            if marker.get("kind") == "missing":
                painter.drawPolygon(QtGui.QPolygonF([
                    QtCore.QPointF(x, self.ISSUE_Y - 12),
                    QtCore.QPointF(x - 9, self.ISSUE_Y + 7),
                    QtCore.QPointF(x + 9, self.ISSUE_Y + 7),
                ]))
            else:
                painter.drawEllipse(QtCore.QPointF(x, self.ISSUE_Y), 5, 5)

        playhead_x = self._x_for_time(self.playhead)
        painter.setPen(QtGui.QPen(QtGui.QColor("#5EC8FF"), 2))
        painter.drawLine(QtCore.QPointF(playhead_x, 27), QtCore.QPointF(playhead_x, 245))
        painter.setBrush(QtGui.QColor("#5EC8FF"))
        painter.drawPolygon(QtGui.QPolygonF([
            QtCore.QPointF(playhead_x - 6, 27),
            QtCore.QPointF(playhead_x + 6, 27),
            QtCore.QPointF(playhead_x, 35),
        ]))

    def _nearest_marker(self, point):
        candidates = [
            (abs(marker_point.x() - point.x()), marker)
            for marker_point, marker in self._marker_points
            if abs(marker_point.y() - point.y()) <= 18
            and abs(marker_point.x() - point.x()) <= 10
        ]
        return min(candidates, default=(9999, None), key=lambda value: value[0])[1]

    def mousePressEvent(self, event):
        point = event.position()
        marker = self._nearest_marker(point)
        if marker is not None:
            self.markerActivated.emit(marker)
            self.seekRequested.emit(float(marker["time"]))
            if marker.get("clip_index") is not None:
                self.clipActivated.emit(int(marker["clip_index"]))
            return
        for rect, block in self._subtitle_rects:
            if rect.contains(point):
                self.subtitleActivated.emit(block)
                self.seekRequested.emit(float(block["timeline_start"]))
                self.clipActivated.emit(int(block["clip_index"]))
                return
        seconds = self._time_for_x(point.x())
        self.seekRequested.emit(seconds)
        for rect, segment in self._segment_rects:
            if rect.contains(point):
                clip_index = int(segment["clip_index"])
                self.clipActivated.emit(clip_index)
                if event.button() == QtCore.Qt.LeftButton:
                    self._drag_clip_index = clip_index
                    self._drag_target_index = clip_index
                    self._drag_start = QtCore.QPointF(point)
                    self._dragging_clip = False
                break

    def mouseMoveEvent(self, event):
        point = event.position()
        if self._drag_clip_index is not None and self._drag_start is not None:
            distance = (point - self._drag_start).manhattanLength()
            if distance >= QtWidgets.QApplication.startDragDistance():
                self._dragging_clip = True
                self.setCursor(QtCore.Qt.ClosedHandCursor)
                target = None
                for rect, segment in self._segment_rects:
                    if rect.contains(point):
                        target = int(segment["clip_index"])
                        break
                if target != self._drag_target_index:
                    self._drag_target_index = target
                    self.update()
                self.setToolTip(
                    "拖到另一个视频块上松开，即可调整整段视频的导出顺序"
                )
                return
        marker = self._nearest_marker(point)
        if marker is not None:
            self.setToolTip(
                f"{format_time(marker['time'])} · {marker.get('title', '问题')}\n"
                f"{marker.get('detail', '')}"
            )
            return
        for rect, block in self._subtitle_rects:
            if rect.contains(point):
                if block.get("is_missing"):
                    label = "⛔ 缺段原文"
                else:
                    label = (
                        "模型识别"
                        if block.get("kind") == "recognized"
                        else "任务文本对齐"
                    )
                self.setToolTip(
                    f"{label} · {format_time(block['timeline_start'])}\n"
                    f"{block.get('text') or '（空）'}\n{block.get('suggestion') or ''}"
                )
                return
        for rect, segment in self._segment_rects:
            if rect.contains(point):
                if segment.get("auto_excluded_duplicate"):
                    state = "重复候选，当前未选（不会导出）"
                elif segment.get("duplicate_group_id"):
                    state = "重复组中当前保留的片段"
                else:
                    state = (
                        f"将删除：{segment.get('remove_reason') or '气口'}"
                        if segment.get("is_removed") else "将保留"
                    )
                self.setToolTip(
                    f"{segment.get('file_name', '')} · {state}\n"
                    f"时间线 {format_time(segment['timeline_start'])} → "
                    f"{format_time(segment['timeline_end'])}\n"
                    f"原片 {format_time(segment['source_start'])} → "
                    f"{format_time(segment['source_end'])}"
                    + (
                        "\n点击后可在右侧重复片段列表切换保留版本"
                        if segment.get("duplicate_group_id")
                        else (
                            "\n双击可取消删除此区间"
                            if segment.get("is_removed") else ""
                        )
                    )
                )
                return
        self.setToolTip(format_time(self._time_for_x(point.x())))

    def mouseDoubleClickEvent(self, event):
        point = event.position()
        if event.button() == QtCore.Qt.LeftButton:
            for rect, segment in self._segment_rects:
                if rect.contains(point) and segment.get("is_removed"):
                    self.removedSegmentRestoreRequested.emit(dict(segment))
                    event.accept()
                    return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        source = self._drag_clip_index
        target = self._drag_target_index
        dragging = self._dragging_clip
        self._drag_clip_index = None
        self._drag_target_index = None
        self._drag_start = None
        self._dragging_clip = False
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.update()
        if (
            dragging
            and event.button() == QtCore.Qt.LeftButton
            and source is not None
            and target is not None
            and int(source) != int(target)
        ):
            self.clipReorderRequested.emit(int(source), int(target))
