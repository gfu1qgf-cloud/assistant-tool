"""Compatibility alias for the extracted smart-video-editor engine."""

import sys

from app_plugins.builtin.smart_video_editor import engine as _implementation

# Preserve monkey-patching and identity semantics for callers using the old
# module path; a shallow ``import *`` wrapper would create two module states.
sys.modules[__name__] = _implementation
