"""Legacy import compatibility; playback implementation lives in model.MediaPlayback.

Plugin consumers must import the shared module directly, not this adapter.
"""
from model.MediaPlayback import (
    FfplayReviewSurface, resolve_ffplay, resolve_mpv,
    _EmbeddedFfplayReviewSurface, _EmbeddedMpvReviewSurface, _QtMediaReviewSurface,
    _native_client_size, _windows_user32,
)

__all__ = ["FfplayReviewSurface", "resolve_ffplay", "resolve_mpv"]
