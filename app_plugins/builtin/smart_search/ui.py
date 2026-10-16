import logging
from qt_compat import QtCore, QtWidgets


class SmartSearchDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle('智能搜索'); self.resize(1160, 840)
        layout = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        self.pages = []
        for title in ('图片', '音乐', '视频'):
            page = QtWidgets.QWidget(); QtWidgets.QVBoxLayout(page).setContentsMargins(0, 0, 0, 0)
            self.pages.append(page); self.tabs.addTab(page, title)
        layout.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self.ensure_tab)

    def ensure_tab(self, number):
        if not 0 <= number < 3:
            return
        page = self.pages[number]
        if page.layout().count() == 0:
            try:
                if number == 0:
                    from ..smart_image_search.ui import SmartImageSearchDialog
                    controller = self.plugin.image
                    panel = SmartImageSearchDialog(controller.settings, parent=page)
                    controller.dialog = panel
                    panel.settingsChanged.connect(controller._save_search_settings)
                    if (panel.index.count(panel.encoder.model_id) and panel.encoder.model_is_cached()):
                        QtCore.QTimer.singleShot(0, panel.update_index)
                elif number == 1:
                    from ..smart_music_search.ui import SmartMusicSearchDialog
                    controller = self.plugin.music
                    panel = SmartMusicSearchDialog(controller.settings, parent=page)
                    controller.dialog = panel
                    panel.settingsChanged.connect(controller._save_search_settings)
                else:
                    from .video_ui import VideoSearchPanel
                    panel = VideoSearchPanel(self.plugin.video_settings, self.plugin.context, parent=page)
                    self.plugin.video = panel
                    panel.settingsChanged.connect(self.plugin.save_video_settings)
                panel.setWindowFlags(QtCore.Qt.WindowType.Widget)
                page.layout().addWidget(panel)
            except Exception as error:
                logging.getLogger(__name__).exception('智能搜索页面打开失败')
                QtWidgets.QMessageBox.warning(self, '智能搜索', str(error))
                return
        if self.tabs.currentIndex() != number:
            self.tabs.setCurrentIndex(number)

    def closeEvent(self, event):
        # Hiding never cancels indexing; task/model owners outlive this window.
        if self.plugin.music.dialog is not None:
            self.plugin.music.dialog.stop_preview()
            self.plugin.music.dialog.tag_input.picker.close()
        if self.plugin.video is not None:
            self.plugin.video.stop_preview()
        self.hide(); event.ignore()
