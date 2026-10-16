"""Offline-first gallery. All large media/network work belongs to the host queue."""
from functools import wraps
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from model.AppDataStorage import data_file
from model.DrivePreviewCache import DrivePreviewCache
from model.GoogleDriveBrowser import (FOLDER, SHORTCUT, bookmark, configure_client, item_link,
    load_snapshot, private_root, read_bookmarks, snapshot_key, target)
from model.MediaPlayback import FfplayReviewSurface
from model.OfflineMode import require_online
from .background import run_drive_job


def guarded(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        if self.stopping: return
        try: return method(self, *args, **kwargs)
        except Exception as error: self.status.setText(str(error))
    return call


class ImageView(QtWidgets.QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.picture = self.scene().addPixmap(QtGui.QPixmap())
        self.auto_fit = True
        self.setBackgroundBrush(QtGui.QColor('#20242b'))
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)

    def set_image(self, value):
        self.picture.setPixmap(value); self.scene().setSceneRect(self.picture.boundingRect()); self.fit()

    def fit(self):
        self.auto_fit = True
        if not self.picture.pixmap().isNull(): self.fitInView(self.picture, QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, factor): self.auto_fit = False; self.scale(factor, factor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit: self.fit()

    def wheelEvent(self, event):
        self.zoom(1.15 if event.angleDelta().y() > 0 else 1/1.15); event.accept()


class DriveBrowserDialog(QtWidgets.QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context, self.stopping, self.handle = context, False, None
        self.generation, self.snapshot, self.history, self.current = 0, None, [], None
        self.operation = ''
        self.local, self.preview_item, self._pin = '', None, None
        self._thumbnail_queue = []
        self.setWindowTitle('谷歌网盘浏览器 · 完整缩略图 / 离线收藏'); self.resize(1280, 820)
        root = QtWidgets.QVBoxLayout(self)
        menu = QtWidgets.QMenuBar(self); menu.setNativeMenuBar(False); root.addWidget(menu)
        files = menu.addMenu('文件'); files.addAction('打开网盘链接…', self.paste_link)
        files.addAction('另存当前已准备的原件…', self.save_media); files.addSeparator(); files.addAction('关闭', self.close)
        favorites = menu.addMenu('收藏'); favorites.addAction('收藏当前文件夹', self.bookmark_folder)
        favorites.addAction('添加网盘链接…', self.add_bookmark); favorites.addAction('重命名选中收藏…', self.rename_bookmark)
        favorites.addAction('移除选中收藏（不删网盘文件）', self.remove_bookmark)
        auth = menu.addMenu('网盘授权'); auth.addAction('Google只读授权…', self.authorize)
        auth.addAction('选择OAuth客户端JSON…', self.select_client)
        caches = menu.addMenu('预览缓存'); caches.addAction('准备当前页全部缩略图', self.prepare_thumbnails)
        caches.addAction('缓存位置／容量说明', self.cache_info); caches.addAction('更改缓存目录…', self.change_cache)
        caches.addAction('设置缓存容量…', self.change_limits)
        caches.addAction('清理本插件预览缓存…', self.clear_cache)
        menu.addAction('使用说明', self.help)
        bar = QtWidgets.QHBoxLayout(); root.addLayout(bar)
        self.back_button = QtWidgets.QPushButton('← 返回'); self.back_button.clicked.connect(self.go_back); bar.addWidget(self.back_button)
        for label, name in [('我的网盘', 'root'), ('与我共享', 'shared')]:
            button = QtWidgets.QPushButton(label); button.clicked.connect(lambda _=False, value=name: self.open_target({'id': value, 'resourceKey': ''})); bar.addWidget(button)
        self.address = QtWidgets.QLineEdit(); self.address.setPlaceholderText('粘贴Google Drive文件／文件夹链接'); self.address.returnPressed.connect(self.open_address); bar.addWidget(self.address, 1)
        button = QtWidgets.QPushButton('打开'); button.clicked.connect(self.open_address); bar.addWidget(button)
        self.refresh_button = QtWidgets.QPushButton('联网刷新'); self.refresh_button.clicked.connect(self.refresh); bar.addWidget(self.refresh_button)
        filters = QtWidgets.QHBoxLayout(); root.addLayout(filters)
        self.path_label = QtWidgets.QLabel('选择收藏，或打开网盘链接'); self.path_label.setTextFormat(QtCore.Qt.TextFormat.PlainText); filters.addWidget(self.path_label, 1)
        self.search = QtWidgets.QLineEdit(); self.search.setPlaceholderText('当前目录按名称查找'); self.search.returnPressed.connect(self.refresh); filters.addWidget(self.search)
        self.media = QtWidgets.QComboBox()
        for label, key in [('全部素材', 'all'), ('图片', 'image'), ('视频', 'video')]: self.media.addItem(label, key)
        self.media.activated.connect(self.refresh); filters.addWidget(self.media)
        self.order = QtWidgets.QComboBox()
        for label, key in [('名称顺序', 'name_natural'), ('修改日期 · 新到旧', 'modifiedTime desc'), ('文件大小 · 大到小', 'quotaBytesUsed desc')]: self.order.addItem(label, key)
        self.order.activated.connect(self.refresh); filters.addWidget(self.order)
        self.offline = QtWidgets.QCheckBox('离线优先'); self.offline.setChecked(True)
        self.offline.setToolTip('已缓存目录直接本地打开，不自动联网；需要更新时点“联网刷新”。'); filters.addWidget(self.offline)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal); root.addWidget(splitter, 1)
        panel = QtWidgets.QWidget(); left = QtWidgets.QVBoxLayout(panel); left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(QtWidgets.QLabel('🔖 网盘收藏（密盘）'))
        self.bookmarks = QtWidgets.QListWidget(); self.bookmarks.itemDoubleClicked.connect(self.open_bookmark); left.addWidget(self.bookmarks, 1)
        self.bookmarks.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu); self.bookmarks.customContextMenuRequested.connect(self.bookmark_menu)
        add = QtWidgets.QPushButton('＋ 收藏链接'); add.clicked.connect(self.add_bookmark); left.addWidget(add); splitter.addWidget(panel)
        panel = QtWidgets.QWidget(); grid = QtWidgets.QVBoxLayout(panel); grid.setContentsMargins(0, 0, 0, 0)
        self.gallery = QtWidgets.QListWidget(); self.gallery.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.gallery.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust); self.gallery.setMovement(QtWidgets.QListView.Movement.Static)
        self.gallery.setIconSize(QtCore.QSize(170, 170)); self.gallery.setGridSize(QtCore.QSize(192, 225)); self.gallery.setWordWrap(True)
        self.gallery.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.gallery.currentItemChanged.connect(self.selection_changed); self.gallery.itemDoubleClicked.connect(self.open_selected)
        self.gallery.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu); self.gallery.customContextMenuRequested.connect(self.gallery_menu)
        grid.addWidget(self.gallery, 1)
        row = QtWidgets.QHBoxLayout(); grid.addLayout(row)
        self.more = QtWidgets.QPushButton('加载更多'); self.more.clicked.connect(self.load_more); self.more.setEnabled(False); row.addWidget(self.more)
        self.prepare = QtWidgets.QPushButton('准备离线缩略图'); self.prepare.clicked.connect(self.prepare_thumbnails); row.addWidget(self.prepare)
        self.thumb_size = QtWidgets.QComboBox()
        for label, pixels in [('小图', 130), ('中图', 170), ('大图', 260)]: self.thumb_size.addItem(label, pixels)
        self.thumb_size.setCurrentIndex(1); self.thumb_size.activated.connect(self.resize_thumbnails); row.addWidget(self.thumb_size)
        splitter.addWidget(panel)
        panel = QtWidgets.QWidget(); right = QtWidgets.QVBoxLayout(panel); right.setContentsMargins(0, 0, 0, 0)
        self.details = QtWidgets.QLabel('单击看完整缩略图，双击打开文件夹／完整预览'); self.details.setWordWrap(True); self.details.setTextFormat(QtCore.Qt.TextFormat.PlainText); right.addWidget(self.details)
        self.views = QtWidgets.QStackedWidget(); self.image = ImageView(); self.views.addWidget(self.image)
        # Instantiate the decoder only when a video is explicitly opened.
        self.player = None
        right.addWidget(self.views, 1)
        tools = QtWidgets.QHBoxLayout(); right.addLayout(tools)
        for label, callback in [('适应全图', self.image.fit), ('＋', lambda: self.image.zoom(1.25)), ('－', lambda: self.image.zoom(.8))]:
            button = QtWidgets.QPushButton(label); button.clicked.connect(callback); tools.addWidget(button)
        button = QtWidgets.QPushButton('完整预览（缓存原件）'); button.setToolTip('完整播放需要缓存原件；仅看完整缩略图不需要下载整个视频。'); button.clicked.connect(self.open_selected); tools.addWidget(button)
        self.play_button = QtWidgets.QPushButton('播放 / 暂停'); self.play_button.clicked.connect(self.toggle_play); self.play_button.setEnabled(False); right.addWidget(self.play_button)
        self.seek = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); self.seek.setRange(0, 10000); self.seek.setEnabled(False); self.seek.sliderReleased.connect(self.seek_video); right.addWidget(self.seek)
        self.time_label = QtWidgets.QLabel(''); right.addWidget(self.time_label)
        splitter.addWidget(panel); splitter.setSizes([190, 560, 480]); splitter.setChildrenCollapsible(False)
        footer = QtWidgets.QHBoxLayout(); root.addLayout(footer)
        self.progress = QtWidgets.QProgressBar(); self.progress.setRange(0, 100); footer.addWidget(self.progress, 1)
        self.cancel = QtWidgets.QPushButton('取消当前操作'); self.cancel.clicked.connect(self.cancel_job); self.cancel.setEnabled(False); footer.addWidget(self.cancel)
        self.status = QtWidgets.QLabel('首次准备需要联网；成功缓存后可离线看图。不使用Google裁切缩略图。')
        self.status.setWordWrap(True); self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText); root.addWidget(self.status)
        for button in self.findChildren(QtWidgets.QPushButton): button.setAutoDefault(False)
        self.cache_root, self.cache_limits = None, {}
        self.initialize()

    @guarded
    def initialize(self):
        preferences = private_root().file('preferences.json')
        if preferences.exists():
            values = preferences.read_json(); self.cache_root = values.get('cache_root'); self.cache_limits = values.get('cache_limits', {})
        self.cache_root = str(self.cache().root)
        self.reload_bookmarks()
        key = snapshot_key({'id': 'root'})
        cached = load_snapshot(key)
        if cached: self.show_snapshot(cached, cached=True)

    def selected(self):
        value = self.gallery.currentItem()
        return value.data(QtCore.Qt.ItemDataRole.UserRole) if value else None

    def cache(self): return DrivePreviewCache(self.cache_root, **self.cache_limits)

    def save_preferences(self):
        private_root().file('preferences.json').write_json({'cache_root': self.cache_root, 'cache_limits': self.cache_limits})

    def parameters(self): return {'search': self.search.text().strip(), 'order': self.order.currentData(), 'media': self.media.currentData()}

    @guarded
    def open_address(self, *_): self.open_target(target(self.address.text()))

    @guarded
    def paste_link(self, *_):
        value, ok = QtWidgets.QInputDialog.getText(self, '打开网盘链接', 'Google Drive文件／文件夹链接：')
        if ok: self.address.setText(value); self.open_address()

    @guarded
    def open_target(self, item, *, refresh=False, page='', back=False):
        private_root()
        self.release_preview()
        if not back and not page and self.current and item['id'] != self.current['id']: self.history.append(dict(self.current))
        self.current = item
        parameters = self.parameters(); key = snapshot_key(item, **parameters)
        cached = load_snapshot(key) if not refresh and not page and self.offline.isChecked() else None
        if cached:
            self.cancel_job(); self.show_snapshot(cached, cached=True)
        else: self.submit('browse', item=item, key=key, page=page, **parameters)

    @guarded
    def refresh(self, *_):
        if self.current: self.open_target(self.current, refresh=True)

    @guarded
    def go_back(self, *_):
        if self.history: self.open_target(self.history.pop(), back=True)

    @guarded
    def load_more(self, *_):
        if self.snapshot and self.snapshot.get('next'): self.open_target(self.current, page=self.snapshot['next'])

    def show_snapshot(self, value, *, cached=False):
        self.snapshot = value; self.current = value['folder']
        self.address.setText(item_link({'mimeType': FOLDER, **self.current}))
        self.path_label.setText(self.current.get('name') or {'root': '我的网盘', 'shared': '与我共享'}.get(self.current['id'], '网盘文件夹'))
        self.gallery.clear()
        for row in value['items']:
            item = QtWidgets.QListWidgetItem(self.caption(row)); item.setData(QtCore.Qt.ItemDataRole.UserRole, row)
            item.setToolTip(row['name'] + ('\n' + row['_thumbnail_error'] if row.get('_thumbnail_error') else ''))
            self.set_icon(item, row); self.gallery.addItem(item)
        self.more.setEnabled(bool(value.get('next'))); self.back_button.setEnabled(bool(self.history))
        available = sum(bool(row.get('_thumbnail') and Path(row['_thumbnail']).is_file()) for row in value['items'])
        self.status.setText(f'{"离线目录缓存" if cached else "目录读取完成"} · {len(value["items"])} 项 · 本地完整缩略图 {available} 张。'
                            + (' 当前目录可能不完整，请联网刷新。' if value.get('incomplete') else ''))
        self.progress.setValue(100)

    @staticmethod
    def caption(row):
        if row.get('mimeType') == FOLDER: label = '文件夹'
        elif row.get('mimeType') == SHORTCUT: label = '快捷方式'
        else: label = f'{int(row.get("size") or 0)/1024**2:.1f} MB'
        return row['name'] + '\n' + label

    def set_icon(self, widget_item, row):
        # A square canvas letterboxes the full frame; the list never crop-fills.
        size = self.gallery.iconSize().width()
        canvas = QtGui.QPixmap(size, size); canvas.fill(QtGui.QColor('#232a34'))
        painter = QtGui.QPainter(canvas)
        source = QtGui.QPixmap(row.get('_thumbnail', '')) if row.get('_thumbnail') else QtGui.QPixmap()
        if not source.isNull():
            try: self.cache().existing(row['_thumbnail'])
            except ValueError: pass  # Old, explicitly changed cache location remains readable.
            scaled = source.scaled(size, size, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((size-scaled.width())//2, (size-scaled.height())//2, scaled)
        else:
            painter.setPen(QtGui.QColor('#d7e6f3')); painter.drawText(canvas.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                '📁 文件夹' if row.get('mimeType') == FOLDER else ('未生成\n完整缩略图' if row.get('mimeType', '').startswith(('image/', 'video/')) else '文件 / 快捷方式'))
        painter.end(); widget_item.setIcon(QtGui.QIcon(canvas))

    @guarded
    def resize_thumbnails(self, *_):
        size = self.thumb_size.currentData()
        self.gallery.setIconSize(QtCore.QSize(size, size)); self.gallery.setGridSize(QtCore.QSize(size+22, size+55))
        for index in range(self.gallery.count()):
            item = self.gallery.item(index); self.set_icon(item, item.data(QtCore.Qt.ItemDataRole.UserRole))

    @guarded
    def selection_changed(self, *_):
        row = self.selected()
        if not row: return
        if self.operation == 'preview' and self.handle: self.cancel_job()
        self.release_preview()
        self.details.setText(row['name'] + '\n' + row.get('modifiedTime', '') + ('\n' + row['_thumbnail_error'] if row.get('_thumbnail_error') else ''))
        source = QtGui.QPixmap(row.get('_thumbnail', '')) if row.get('_thumbnail') else QtGui.QPixmap()
        self.image.set_image(source); self.views.setCurrentWidget(self.image)

    @guarded
    def open_selected(self, *_):
        row = self.selected()
        if not row: return
        if row.get('mimeType') in {FOLDER, SHORTCUT}: self.open_target({'id': row['id'], 'resourceKey': row.get('resourceKey', '')}); return
        if not row.get('mimeType', '').startswith(('image/', 'video/')): self.open_web(row); return
        account = self.snapshot.get('account', '') if self.snapshot else ''
        cached = account and self.cache().existing(self.cache().path('media', account, row))
        if row['mimeType'].startswith('video/') and not cached:
            size = int(row.get('size') or 0)
            if size > self.cache().limits['media']: raise ValueError('此视频超过完整预览缓存上限；完整缩略图不受此限制。')
            reply = QtWidgets.QMessageBox.question(self, '准备完整视频播放',
                f'完整播放将下载视频原件到卷外缓存，约 {size/1024**2:.1f} MB。\n仅查看完整缩略图无需下载整个视频。\n\n是否准备完整播放？')
            if reply != QtWidgets.QMessageBox.StandardButton.Yes: return
        self.release_preview()
        self.submit('preview', item=row, account=account)

    @guarded
    def prepare_thumbnails(self, *_):
        if not self.snapshot: return
        self._thumbnail_queue = [row['id'] for row in self.snapshot['items'] if row.get('mimeType', '').startswith(('image/', 'video/'))
            and not (row.get('_thumbnail') and Path(row['_thumbnail']).is_file())]
        if not self._thumbnail_queue: self.status.setText('当前已加载素材的完整缩略图均已缓存。'); return
        self.start_thumbnail_batch()

    def start_thumbnail_batch(self):
        if not self._thumbnail_queue: return
        ids, self._thumbnail_queue = self._thumbnail_queue[:8], self._thumbnail_queue[8:]
        self.submit('thumbnails', key=self.snapshot['key'], ids=ids, keep_queue=True)

    @guarded
    def submit(self, operation, *, keep_queue=False, **values):
        if not keep_queue: self._thumbnail_queue = []
        self.cancel_job(keep_queue=True)
        private_root()
        self.generation += 1; generation = self.generation
        self.operation = operation
        labels = {'browse': '读取目录', 'thumbnails': '准备完整缩略图', 'preview': '完整预览', 'authorize': '只读授权', 'clear_cache': '清理预览缓存', 'save_media': '另存原件'}
        handle = self.context.submit_background_task(title='网盘浏览 · ' + labels[operation], runner=run_drive_job,
            payload={'operation': operation, 'cache_root': self.cache_root, 'cache_limits': self.cache_limits, **values},
            kind='network' if operation not in {'clear_cache', 'save_media'} else 'io', resources=['google-drive-browser:session'],
            execution='parallel', retryable=False)
        self.handle = handle; self.cancel.setEnabled(True); self.progress.setValue(0)
        handle.completed.connect(lambda value, current=generation: self.completed(current, value))
        handle.failed.connect(lambda message, current=generation: self.failed(current, message))
        handle.progressChanged.connect(lambda message, percent, current=generation: self.report(current, message, percent))
        handle.finished.connect(lambda current=generation: self.finished(current))
        self.status.setText('已提交后台：' + labels[operation] + '。可取消或关闭此窗口。')

    @guarded
    def cancel_job(self, *_args, keep_queue=False):
        if not keep_queue: self._thumbnail_queue = []
        self.generation += 1
        if self.handle and self.handle.isRunning(): self.handle.requestInterruption()
        self.handle = None; self.cancel.setEnabled(False)
        if not keep_queue: self.status.setText('已请求取消；已成功生成的缩略图保留。')

    @guarded
    def report(self, generation, message, percent):
        if generation != self.generation: return
        self.status.setText(message); self.progress.setValue(int(percent or 0))

    @guarded
    def completed(self, generation, value):
        if generation != self.generation: return
        self.handle = None; self.cancel.setEnabled(False); self.progress.setValue(100)
        kind = value['kind']
        if kind == 'list':
            self.show_snapshot(value)
            self.prepare_thumbnails()
        elif kind == 'file':
            row = value['item']; item = QtWidgets.QListWidgetItem(self.caption(row)); item.setData(QtCore.Qt.ItemDataRole.UserRole, row)
            self.gallery.clear(); self.gallery.addItem(item); self.gallery.setCurrentItem(item); self.open_selected()
        elif kind == 'thumbnails':
            if not self.snapshot or value['key'] != self.snapshot['key']: return
            for index in range(self.gallery.count()):
                item = self.gallery.item(index); row = item.data(QtCore.Qt.ItemDataRole.UserRole)
                if row['id'] in value['updates']: row['_thumbnail'] = value['updates'][row['id']]; row.pop('_thumbnail_error', None)
                elif row['id'] in value['errors']: row['_thumbnail_error'] = value['errors'][row['id']]
                item.setData(QtCore.Qt.ItemDataRole.UserRole, row); self.set_icon(item, row)
                item.setToolTip(row['name'] + ('\n' + row['_thumbnail_error'] if row.get('_thumbnail_error') else ''))
            self.snapshot = load_snapshot(value['key'])
            selected = self.selected()
            if selected and selected.get('_thumbnail') and not self.local:
                self.image.set_image(QtGui.QPixmap(selected['_thumbnail']))
            self.status.setText(f'完整缩略图已缓存 {len(value["updates"])} 张，未生成 {len(value["errors"])} 张。'
                                + (' 可悬停查看原因；未用裁切图替代，也未改为整段下载。' if value['errors'] else '离线打开无需联网。'))
            if self._thumbnail_queue: self.start_thumbnail_batch()
        elif kind == 'preview':
            self.release_preview(); self.local, self.preview_item = value['local'], value['item']
            self._pin = self.cache().pin(self.local); self._pin.__enter__()
            if value.get('image'):
                picture = QtGui.QPixmap(); picture.loadFromData(value['image']); self.image.set_image(picture); self.views.setCurrentWidget(self.image)
            else:
                if self.player is None:
                    from app_paths import APP_ROOT
                    self.player = FfplayReviewSurface(ffplay_path=str(APP_ROOT/'runtime/ffmpeg/ffmpeg.exe'), parent=self, preload_enabled=False); self.views.addWidget(self.player)
                    self.player.positionChanged.connect(self.position_changed); self.player.errorOccurred.connect(self.player_error)
                self.video_duration = value['duration']; self.player.prepare(self.local, 0, self.video_duration)
                self.views.setCurrentWidget(self.player); self.play_button.setEnabled(True); self.seek.setEnabled(True)
            self.status.setText('完整预览已就绪，画面保持原比例；另存可复制原件到外部素材目录。')
        elif kind == 'authorize': self.status.setText('只读授权成功。可打开链接或“我的网盘”；原上传授权未修改。')
        elif kind == 'clear_cache': self.status.setText('已清理本插件可再生成的缓存，未删除收藏、索引或网盘文件。')
        elif kind == 'save_media': self.status.setText('原件已另存：' + value['paths'][0])

    @guarded
    def failed(self, generation, message):
        if generation != self.generation: return
        self.handle = None; self._thumbnail_queue = []; self.cancel.setEnabled(False); self.status.setText(message)

    @guarded
    def finished(self, generation):
        if generation == self.generation: self.handle = None; self.cancel.setEnabled(False)

    def release_preview(self):
        if self.player is not None: self.player.stop()
        if self._pin is not None: self._pin.__exit__(None, None, None); self._pin = None
        self.local, self.preview_item = '', None
        self.play_button.setEnabled(False); self.seek.setEnabled(False)

    @guarded
    def toggle_play(self, *_):
        if self.player and self.local:
            if self.player.is_playing(): self.player.pause()
            else: self.player.play()

    @guarded
    def seek_video(self):
        if self.player and self.local:
            playing = self.player.is_playing(); self.player.prepare(self.local, 0, self.video_duration, self.seek.value()*self.video_duration/10000)
            if playing: self.player.play()

    @guarded
    def position_changed(self, position):
        if not self.seek.isSliderDown(): self.seek.setValue(int(position*10000/max(.01, self.video_duration)))
        self.time_label.setText(f'{position:.1f} / {self.video_duration:.1f} 秒')

    @guarded
    def player_error(self, message): self.status.setText(message)

    @guarded
    def reload_bookmarks(self):
        self.bookmarks.clear()
        for row in read_bookmarks():
            item = QtWidgets.QListWidgetItem(row['name']); item.setData(QtCore.Qt.ItemDataRole.UserRole, row); self.bookmarks.addItem(item)

    @guarded
    def open_bookmark(self, item):
        self.open_target(target(item.data(QtCore.Qt.ItemDataRole.UserRole)['url']))

    @guarded
    def add_bookmark(self, *_):
        url, ok = QtWidgets.QInputDialog.getText(self, '收藏网盘链接', 'Google Drive文件／文件夹链接：')
        if not ok: return
        target(url)
        name, ok = QtWidgets.QInputDialog.getText(self, '收藏名称', '名称：')
        if ok: bookmark(name, url); self.reload_bookmarks()

    @guarded
    def bookmark_folder(self, *_):
        if self.current: self.bookmark_item({'mimeType': FOLDER, **self.current})

    @guarded
    def bookmark_item(self, item):
        name, ok = QtWidgets.QInputDialog.getText(self, '收藏网盘链接', '名称：', text=item.get('name', ''))
        if ok: bookmark(name, item_link(item)); self.reload_bookmarks()

    @guarded
    def rename_bookmark(self, *_):
        item = self.bookmarks.currentItem()
        if not item: return
        row = item.data(QtCore.Qt.ItemDataRole.UserRole)
        name, ok = QtWidgets.QInputDialog.getText(self, '重命名收藏', '名称：', text=row['name'])
        if ok: bookmark(name, row['url']); self.reload_bookmarks()

    @guarded
    def remove_bookmark(self, *_):
        item = self.bookmarks.currentItem()
        if item:
            row = item.data(QtCore.Qt.ItemDataRole.UserRole); bookmark(row['name'], row['url'], remove=True); self.reload_bookmarks()

    @guarded
    def bookmark_menu(self, point):
        item = self.bookmarks.itemAt(point)
        if item: self.bookmarks.setCurrentItem(item)
        menu = QtWidgets.QMenu(self); menu.addAction('打开收藏', lambda: self.open_bookmark(item) if item else None)
        menu.addAction('重命名…', self.rename_bookmark); menu.addAction('移除收藏', self.remove_bookmark)
        menu.exec(self.bookmarks.viewport().mapToGlobal(point))

    @guarded
    def gallery_menu(self, point):
        item = self.gallery.itemAt(point)
        if not item: return
        self.gallery.setCurrentItem(item); row = self.selected()
        menu = QtWidgets.QMenu(self); menu.addAction('打开文件夹 / 完整预览', self.open_selected)
        menu.addAction('收藏此链接…', lambda: self.bookmark_item(row))
        menu.addAction('复制网盘链接', lambda: QtWidgets.QApplication.clipboard().setText(item_link(row)))
        menu.addAction('在网页打开', lambda: self.open_web(row)); menu.exec(self.gallery.viewport().mapToGlobal(point))

    @guarded
    def open_web(self, item):
        require_online('打开网盘网页'); QtGui.QDesktopServices.openUrl(QtCore.QUrl(item_link(item)))

    @guarded
    def authorize(self, *_): self.submit('authorize')

    @guarded
    def select_client(self, *_):
        source, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Google桌面应用OAuth客户端', '', 'JSON (*.json)')
        if source: configure_client(source); self.status.setText('客户端已保存在密盘；请执行Google只读授权。')

    @guarded
    def cache_info(self, *_):
        cache = self.cache()
        QtWidgets.QMessageBox.information(self, '预览缓存', f'位置：{self.cache_root}\n完整缩略图上限{cache.limits["thumbs"]/1024**2:.0f}MB，完整预览原件上限{cache.limits["media"]/1024**3:.0f}GB，按最近使用淘汰。\n收藏、文件名、目录和授权均保存在密盘。缓存不保存Google链接或令牌。\n视频缩略图最多读取32MB／40秒，不会退回整段下载。小文件可能恰好读到全部，但不要求先下载完。')

    @guarded
    def change_limits(self, *_):
        cache = self.cache()
        thumbs, ok = QtWidgets.QInputDialog.getInt(self, '缩略图缓存容量', '最大容量 / MB（缓存淘汰后需联网重建）：', cache.limits['thumbs']//1024**2, 32, 8192)
        if not ok: return
        media, ok = QtWidgets.QInputDialog.getInt(self, '完整预览缓存容量', '最大容量 / GB：', cache.limits['media']//1024**3, 1, 32)
        if ok:
            self.cache_limits = {'thumb_limit': thumbs*1024**2, 'media_limit': media*1024**3}; self.save_preferences()
            self.status.setText('缓存上限已保存。不会立即删除正在播放的文件；后续准备预览时按上限淘汰。')

    @guarded
    def change_cache(self, *_):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, '选择卷外网盘预览缓存父目录')
        if not folder: return
        cache = DrivePreviewCache(Path(folder)/'AssistantTool-DrivePreviewCache')
        self.cancel_job(); self.release_preview()
        self.cache_root = str(cache.root); self.save_preferences(); self.status.setText('新缓存位置已保存；旧缓存未搬迁或删除。')

    @guarded
    def clear_cache(self, *_):
        if QtWidgets.QMessageBox.question(self, '清理预览缓存', '仅删除本插件可再生成的缩略图和完整预览缓存，不删除收藏或原素材。清理后离线缩略图需要重新准备。继续？') == QtWidgets.QMessageBox.StandardButton.Yes:
            self.release_preview(); self.submit('clear_cache')

    @guarded
    def save_media(self, *_):
        if not self.local or not self.preview_item: raise ValueError('请先打开并准备完整预览。')
        from model.GoogleDriveDownloader import safe_filename
        filename, _changed = safe_filename(self.preview_item['name'], 'drive-media')
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, '另存原件到外部素材目录（不覆盖已有文件）', filename)
        if destination: self.submit('save_media', source=self.local, destination=destination)

    @guarded
    def help(self, *_):
        QtWidgets.QMessageBox.information(self, '使用说明', '1. 粘贴网盘链接或打开收藏。可复用主程序已有Google Drive授权；没有则在“网盘授权”中只读授权。\n'
            '2. 首次后台生成自己的完整缩略图，不使用Google裁切缩略图。图片读取原图；视频只按字节取帧，每视频最多32MB／40秒，失败不会自动下载全片。\n'
            '3. 已缓存的目录默认离线打开，刷新才联网。按“准备离线缩略图”补齐当前已加载的素材；“加载更多”读取后续页，不会递归下载整库。\n'
            '4. 单击查看960px完整缩略图；滚轮缩放，拖动查看。双击／完整预览会另行下载原件（最多2GB），可播放和拖动视频进度。GIF静态看首帧。\n'
            '5. 收藏／目录／授权在密盘，图片缓存在卷外。超出缓存上限会淘汰旧图，未缓存／已淘汰的不能离线显示。只读操作不会修改网盘文件。')

    def closeEvent(self, event):
        self.release_preview(); super().closeEvent(event)

    def shutdown(self):
        self.cancel_job(); self.release_preview(); self.stopping = True
        if self.player is not None: self.player.release()
        self.close()
