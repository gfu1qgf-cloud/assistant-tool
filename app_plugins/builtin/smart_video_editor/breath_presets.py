"""Named breath-cut settings; applying one never mutates the caller's config."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BreathPreset:
    id: str
    name: str
    pause_ms: int
    padding_ms: int
    sensitivity: float

    @property
    def description(self):
        return (f"停顿 ≥ {self.pause_ms/1000:g} 秒；前后各留白 {self.padding_ms/1000:g} 秒；"
                f"人声检测阈值 {self.sensitivity:.0%}")

    def values(self):
        return {
            "breath_detection_mode": "voice_refined",
            "silence_detection_enabled": True,
            "voice_detection_enabled": True,
            "compress_internal_pauses": True,
            "internal_pause_mode": "enabled",
            "min_silence_ms": self.pause_ms,
            "pause_threshold_ms": self.pause_ms,
            "lead_padding_ms": self.padding_ms,
            "tail_padding_ms": self.padding_ms,
            "pause_keep_before_ms": self.padding_ms,
            "pause_keep_after_ms": self.padding_ms,
            "retained_pause_ms": 2 * self.padding_ms,
            "vad_threshold": self.sensitivity,
            # Keep the VAD's end threshold below its start threshold, including
            # when applying a preset over previously customized VAD settings.
            "vad_neg_threshold": round(self.sensitivity - .15, 2),
        }


BREATH_PRESETS = (
    BreathPreset("gentle", "温柔自然版", 850, 220, .60),
    BreathPreset("clear", "稳妥讲话清楚版", 900, 250, .60),
    BreathPreset("brisk", "快节奏流畅版", 700, 170, .68),
)


def get_preset(preset_id):
    for preset in BREATH_PRESETS:
        if preset.id == preset_id:
            return preset
    raise ValueError("未知的气口预设：" + str(preset_id))


def apply_preset(settings, preset_id):
    return dict(settings, **get_preset(preset_id).values())
