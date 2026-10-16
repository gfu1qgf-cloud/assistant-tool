import math
import os
from qt_compat import QtWidgets
from model.image_retrieval.encoder import MODEL_SPECS
from ..smart_image_search.settings import SmartImageSearchSettingsPage
from ..smart_music_search.settings import SmartMusicSearchSettingsPage

CONFIG_KEY = 'smart_video_search'


def normalize_video_settings(value=None):
    value = value if isinstance(value, dict) else {}
    def number(key, default, low, high, integer=False):
        try:
            result = float(value.get(key, default))
            if not math.isfinite(result):
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            result = default
        result = min(high, max(low, result))
        return int(result) if integer else result
    roots, seen = [], set()
    raw = value.get('library_roots', [])
    for path in raw if isinstance(raw, (list, tuple)) else []:
        path = str(path or '').strip()
        key = os.path.normcase(os.path.abspath(path)) if path else ''
        if key and key not in seen:
            roots.append(path)
            seen.add(key)
    model = value.get('model')
    return {'model': model if isinstance(model, str) and model in MODEL_SPECS else 'large336',
            'library_roots': roots, 'include_inventory': value.get('include_inventory', True) is not False,
            'ffmpeg_path': str(value.get('ffmpeg_path') or '').strip(),
            'interval': number('interval', 3, .5, 60),
            'max_frames': number('max_frames', 240, 8, 1000, True),
            'detect_scenes': value.get('detect_scenes', True) is not False,
            'scene_threshold': number('scene_threshold', .18, .02, .9),
            'result_limit': number('result_limit', 100, 20, 500, True)}


class VideoSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        note = QtWidgets.QLabel('按代表帧搜索画面，结果定位到秒。不是对白、剧情或完整动作理解。'
                               '只在建立索引时读取原视频；搜索不重新解码整库。长视频达到帧数上限后会均匀覆盖全片。')
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QtWidgets.QFormLayout()
        self.model = QtWidgets.QComboBox()
        for key, spec in MODEL_SPECS.items():
            self.model.addItem(spec['label'], key)
        form.addRow('视觉模型', self.model)
        self.inventory = QtWidgets.QCheckBox('同时扫描库存与人物素材目录中的视频')
        form.addRow('库存联动', self.inventory)
        self.interval = QtWidgets.QDoubleSpinBox()
        self.interval.setRange(.5, 60); self.interval.setSuffix(' 秒')
        form.addRow('常规采样间隔', self.interval)
        self.max_frames = QtWidgets.QSpinBox()
        self.max_frames.setRange(8, 1000)
        form.addRow('每视频最多帧数', self.max_frames)
        self.scenes = QtWidgets.QCheckBox('补充画面切换处（低分辨率扫描，首次建库更慢）')
        form.addRow('画面切换检测', self.scenes)
        self.threshold = QtWidgets.QDoubleSpinBox()
        self.threshold.setRange(.02, .9); self.threshold.setSingleStep(.02)
        self.threshold.setToolTip('越小越敏感，也更容易把运动当成换镜头；无法保证覆盖每次叠化。')
        form.addRow('切换检测阈值', self.threshold)
        self.limit = QtWidgets.QSpinBox(); self.limit.setRange(20, 500)
        form.addRow('每批展示结果', self.limit)
        self.ffmpeg = QtWidgets.QLineEdit()
        self.ffmpeg.setPlaceholderText('留空使用程序附带的标准 FFmpeg')
        form.addRow('FFmpeg 路径', self.ffmpeg)
        layout.addLayout(form)
        layout.addWidget(QtWidgets.QLabel('额外视频库（可添加多个，递归扫描；不会移动原文件）'))
        self.roots = QtWidgets.QListWidget(); self.roots.setMaximumHeight(160)
        layout.addWidget(self.roots)
        actions = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton('添加文件夹…'); add.clicked.connect(self._add)
        remove = QtWidgets.QPushButton('移除选中'); remove.clicked.connect(self._remove)
        actions.addWidget(add); actions.addWidget(remove); actions.addStretch()
        layout.addLayout(actions); layout.addStretch()

    def _add(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self.widget, '选择视频库目录')
        if path and path not in self.values()['library_roots']:
            self.roots.addItem(path)

    def _remove(self):
        for item in self.roots.selectedItems():
            self.roots.takeItem(self.roots.row(item))

    def values(self):
        return normalize_video_settings({'model': self.model.currentData(),
            'include_inventory': self.inventory.isChecked(), 'interval': self.interval.value(),
            'max_frames': self.max_frames.value(), 'detect_scenes': self.scenes.isChecked(),
            'scene_threshold': self.threshold.value(), 'result_limit': self.limit.value(),
            'ffmpeg_path': self.ffmpeg.text(),
            'library_roots': [self.roots.item(i).text() for i in range(self.roots.count())]})

    def load_config(self, config):
        value = normalize_video_settings(config.get(CONFIG_KEY))
        self.model.setCurrentIndex(self.model.findData(value['model']))
        self.inventory.setChecked(value['include_inventory'])
        self.interval.setValue(value['interval']); self.max_frames.setValue(value['max_frames'])
        self.scenes.setChecked(value['detect_scenes']); self.threshold.setValue(value['scene_threshold'])
        self.limit.setValue(value['result_limit']); self.ffmpeg.setText(value['ffmpeg_path'])
        self.roots.clear(); self.roots.addItems(value['library_roots'])

    def update_config(self, config):
        config[CONFIG_KEY] = self.values()
        return config


class SmartSearchSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        tabs = QtWidgets.QTabWidget()
        self.pages = []
        for title, factory in (('图片', SmartImageSearchSettingsPage),
                               ('音乐', SmartMusicSearchSettingsPage), ('视频', VideoSettingsPage)):
            page = factory(tabs); self.pages.append(page)
            scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
            scroll.setWidget(page.widget); tabs.addTab(scroll, title)
        layout.addWidget(tabs)

    def load_config(self, config):
        for page in self.pages:
            page.load_config(config)

    def update_config(self, config):
        for page in self.pages:
            page.update_config(config)
        return config
