import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets

from app_plugins.builtin.smart_video_editor.player import (
    _EmbeddedMpvReviewSurface,
    resolve_mpv,
)


class SmartVideoMpvPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_explicit_mpv_runtime_is_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "mpv.exe"
            executable.write_bytes(b"test")
            with patch.dict(os.environ, {"LZX_MPV_PATH": str(executable)}):
                os.environ.pop("QT_QPA_PLATFORM", None)
                self.assertEqual(resolve_mpv(), str(executable))

    def test_ipc_send_returns_request_id(self):
        surface = _EmbeddedMpvReviewSurface("missing-mpv.exe")
        try:
            socket = Mock()
            socket.write.return_value = 10
            surface.socket = socket
            surface._connected = True

            request_id = surface._send(["get_property", "path"])

            self.assertIsInstance(request_id, int)
            payload = json.loads(socket.write.call_args.args[0].decode("utf-8"))
            self.assertEqual(payload["request_id"], request_id)
            self.assertEqual(payload["command"], ["get_property", "path"])
        finally:
            surface.release()
            surface.close()

    def test_decode_error_is_reported_for_automatic_fallback(self):
        surface = _EmbeddedMpvReviewSurface("missing-mpv.exe")
        errors = []
        surface.errorOccurred.connect(errors.append)
        try:
            surface._handle_message({
                "event": "end-file",
                "reason": "error",
                "file_error": "unsupported codec",
            })
            self.assertEqual(
                errors,
                ["mpv 无法播放该视频：unsupported codec"],
            )
        finally:
            surface.release()
            surface.close()

    def test_file_loaded_uses_reported_path_before_revealing_video(self):
        surface = _EmbeddedMpvReviewSurface("missing-mpv.exe")
        try:
            source = str(Path(tempfile.gettempdir()) / "video.mp4")
            surface.source = source
            surface.range_start = 1.0
            surface.range_end = 3.0
            surface.position = 0.25
            surface._waiting_for_file = True
            surface._path_request_id = 91
            surface._send = Mock(return_value=92)

            surface._handle_message({
                "request_id": 91,
                "error": "success",
                "data": source,
            })

            self.assertFalse(surface._waiting_for_file)
            self.assertEqual(surface.loaded_source, source)
            surface._send.assert_any_call([
                "seek", 1.25, "absolute+exact"
            ])
        finally:
            surface.release()
            surface.close()


if __name__ == "__main__":
    unittest.main()
