from qt_compat import QtWidgets

from .analyzer import (
    MATERIAL_ORGANIZER_CONFIG_KEY,
    normalize_material_organizer_settings,
)


class MaterialOrganizerSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        title = QtWidgets.QLabel("素材整理", self.widget)
        title.setStyleSheet("font-size:14px;font-weight:600;")
        layout.addWidget(title)
        description = QtWidgets.QLabel(
            "这些参数只影响自动分类。源视频始终保持原位；重新分析会更新"
            "虚拟分类和缩略图缓存。",
            self.widget,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        form = QtWidgets.QFormLayout()
        self.sample_fps = QtWidgets.QDoubleSpinBox(self.widget)
        self.sample_fps.setRange(0.25, 15.0)
        self.sample_fps.setDecimals(2)
        self.sample_fps.setSuffix(" 帧/秒")
        self.analysis_width = QtWidgets.QSpinBox(self.widget)
        self.analysis_width.setRange(240, 1280)
        self.analysis_width.setSingleStep(40)
        self.analysis_width.setSuffix(" px")
        self.maximum_samples = QtWidgets.QSpinBox(self.widget)
        self.maximum_samples.setRange(20, 3000)
        self.maximum_samples.setSuffix(" 帧")
        self.static_threshold = QtWidgets.QDoubleSpinBox(self.widget)
        self.static_threshold.setRange(0.01, 5.0)
        self.static_threshold.setDecimals(2)
        self.static_threshold.setSuffix(" %")
        self.motion_threshold = QtWidgets.QDoubleSpinBox(self.widget)
        self.motion_threshold.setRange(0.02, 10.0)
        self.motion_threshold.setDecimals(2)
        self.motion_threshold.setSuffix(" %")
        self.zoom_threshold = QtWidgets.QDoubleSpinBox(self.widget)
        self.zoom_threshold.setRange(0.01, 5.0)
        self.zoom_threshold.setDecimals(2)
        self.zoom_threshold.setSuffix(" %")
        self.rotation_threshold = QtWidgets.QDoubleSpinBox(self.widget)
        self.rotation_threshold.setRange(0.01, 10.0)
        self.rotation_threshold.setDecimals(2)
        self.rotation_threshold.setSuffix("°")
        self.subject_threshold = QtWidgets.QDoubleSpinBox(self.widget)
        self.subject_threshold.setRange(0.02, 10.0)
        self.subject_threshold.setDecimals(2)
        self.subject_threshold.setSuffix(" %")
        self.minimum_confidence = QtWidgets.QDoubleSpinBox(self.widget)
        self.minimum_confidence.setRange(0.10, 0.95)
        self.minimum_confidence.setDecimals(2)
        self.minimum_confidence.setSingleStep(0.05)

        form.addRow("分析抽帧频率：", self.sample_fps)
        form.addRow("分析画面宽度：", self.analysis_width)
        form.addRow("单视频最多分析：", self.maximum_samples)
        form.addRow("静态位移上限：", self.static_threshold)
        form.addRow("平移判定阈值：", self.motion_threshold)
        form.addRow("推近/拉远阈值：", self.zoom_threshold)
        form.addRow("旋转判定阈值：", self.rotation_threshold)
        form.addRow("主体运动阈值：", self.subject_threshold)
        form.addRow("最低自动分类置信度：", self.minimum_confidence)
        layout.addLayout(form)

        hint = QtWidgets.QLabel(
            "分析较慢时可降低抽帧频率或画面宽度。误把轻微抖动当运镜时，"
            "提高平移阈值；大量素材进入“待人工确认”时，可适当降低最低置信度。",
            self.widget,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        layout.addWidget(hint)
        layout.addStretch(1)

    def load_config(self, config):
        settings = normalize_material_organizer_settings(
            (config or {}).get(MATERIAL_ORGANIZER_CONFIG_KEY)
        )
        self.sample_fps.setValue(settings["sample_fps"])
        self.analysis_width.setValue(settings["analysis_width"])
        self.maximum_samples.setValue(settings["maximum_samples"])
        self.static_threshold.setValue(settings["static_translation_percent"])
        self.motion_threshold.setValue(settings["motion_translation_percent"])
        self.zoom_threshold.setValue(settings["zoom_threshold_percent"])
        self.rotation_threshold.setValue(settings["rotation_threshold_degrees"])
        self.subject_threshold.setValue(settings["subject_motion_percent"])
        self.minimum_confidence.setValue(settings["minimum_confidence"])

    def settings(self):
        return normalize_material_organizer_settings({
            "sample_fps": self.sample_fps.value(),
            "analysis_width": self.analysis_width.value(),
            "maximum_samples": self.maximum_samples.value(),
            "static_translation_percent": self.static_threshold.value(),
            "motion_translation_percent": self.motion_threshold.value(),
            "zoom_threshold_percent": self.zoom_threshold.value(),
            "rotation_threshold_degrees": self.rotation_threshold.value(),
            "subject_motion_percent": self.subject_threshold.value(),
            "minimum_confidence": self.minimum_confidence.value(),
        })

    def update_config(self, config):
        config[MATERIAL_ORGANIZER_CONFIG_KEY] = self.settings()
