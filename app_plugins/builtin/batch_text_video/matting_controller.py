"""Material matting menu; edits never replace pool files or rotate cursors."""
from qt_compat import QtCore, QtWidgets


class MattingController:
    def init_matting_menu(self):
        self.matting_dialog = None
        panel = getattr(self, 'layer_panel', None)
        if panel is not None and hasattr(panel, 'mattingRequested'):
            panel.inspector.matting_available = True
            panel.mattingRequested.connect(self.matte_from_properties)
        menu = self.menu_bar.addMenu('抠像')
        menu.addAction('素材抠像工作台…', lambda: self.open_matting())
        menu.addAction('抠当前图层／正在试看的素材…', self.matte_current_layer)
        menu.addAction('抠当前资源池所选素材…', self.matte_pool_selection)
        menu.addSeparator()
        menu.addAction('查看已保存的素材抠像…', self.matte_saved_sources)

    def matte_from_properties(self, identifier):
        current = self.layer_panel.current()
        if current and current.get('id') == identifier: self.matte_current_layer()

    def open_matting(self, paths=()):
        from .matting_ui import MattingDialog
        from .matting_assets import identity
        if self.matting_dialog is None:
            self.matting_dialog = MattingDialog(paths, self, ffmpeg=self.ffmpeg.text().strip())
            self.matting_dialog.saved.connect(self.matte_changed)
        else:
            self.matting_dialog.add_sources(paths)
        dialog = self.matting_dialog
        dialog._closing = False
        if paths:
            for i in range(dialog.files.count()):
                if identity(dialog.files.item(i).data(QtCore.Qt.ItemDataRole.UserRole)) == identity(paths[0]):
                    dialog.files.setCurrentRow(i); break
        dialog.show(); dialog.showNormal(); dialog.raise_(); dialog.activateWindow()
        return dialog

    def matte_current_layer(self):
        try:
            from .components import resolve_sources
            current = self.layer_panel.current()
            if not current or current.get('kind') != 'image': raise ValueError('请先选中图片／GIF／视频图层。')
            settings = self.read_settings()
            if getattr(self, 'pool_preview', None): settings = self.pool_preview.apply(settings)
            layer = next(v for v in settings['layers'] if v['id'] == current['id'])
            layer = dict(layer, use_saved_matte=False)
            source = resolve_sources([layer])[0][0]['path']
            return self.open_matting([source])
        except (ValueError, OSError) as error: self.status.setText(str(error))

    def matte_pool_selection(self, _checked=False, *, key=None):
        try:
            from .visual_media import VISUAL_SUFFIXES
            from pathlib import Path
            key = key or self.active_resource()
            if key is None: raise ValueError('请在资源池中选中图片或视频。')
            selected = set(self.table_for(key).identifiers())
            items = self.resource_items(key)
            paths = [item if isinstance(item, str) else item.get('path', '') for item in items
                     if (item if isinstance(item, str) else item.get('id')) in selected]
            paths = [p for p in paths if Path(p).suffix.lower() in VISUAL_SUFFIXES]
            if not paths: raise ValueError('请在资源池中选中图片或视频。')
            return self.open_matting(paths)
        except (KeyError, ValueError, OSError) as error: self.status.setText(str(error))

    def matte_saved_sources(self):
        try:
            from .matting_assets import Catalogue
            paths = [entry['source']['path'] for entry in Catalogue().load()['assets'].values()]
            return self.open_matting(paths)
        except (KeyError, ValueError, OSError, TypeError) as error: self.status.setText('读取抠像记录失败：'+str(error))

    def matte_changed(self, _source):
        from .layers import ImageCache
        self._image_cache = ImageCache()
        self.invalidate_playback(); self.update_preview()
