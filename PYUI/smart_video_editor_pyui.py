"""Compatibility alias for the extracted smart-video-editor plugin UI."""

import sys

from app_plugins.builtin.smart_video_editor import ui as _implementation

sys.modules[__name__] = _implementation
