"""Legacy import compatibility; playback implementation lives in model.MediaPlayback.

Plugin consumers must import the shared module directly, not this adapter.
"""
from model.MediaPlayback import FfplayReviewSurface, QtMediaReviewSurface

__all__ = ["FfplayReviewSurface", "QtMediaReviewSurface"]
