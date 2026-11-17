"""Exercise the real frozen Python worker and Qt forms using disposable state."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

CODE = r'''
import json, os, time
from pathlib import Path
root = Path(os.environ['RELEASE_SMOKE_ROOT'])
import app_paths
app_paths.APP_ROOT = root
app_paths.bind_data_root(root)
os.chdir(root)
from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6 import sip
app = QtWidgets.QApplication([])
# The offscreen Qt backend does not enumerate Windows font fallback reliably.
for filename in ('msyh.ttc', 'segoeui.ttf', 'simsun.ttc', 'arial.ttf'):
    font = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts'/filename
    if font.is_file(): QtGui.QFontDatabase.addApplicationFont(str(font))
app.setFont(QtGui.QFont('Microsoft YaHei', 9))
errors = []
import sys
sys.excepthook = lambda kind, value, tb: errors.append(str(value))
from PYUI.first_run_pyui import FirstRunDialog
guide = FirstRunDialog(root)
guide.show()
app.processEvents()
assert guide.state == 'fresh'
assert guide.grab().save(str(root/'first-use.png'))
guide.close()
sip.delete(guide)
from model.FirstRun import initial_settings, create_normal_config
config = initial_settings(root, offline=True)
create_normal_config(root, config)
# Do not register global hotkeys against the developer's live desktop.
import model.GlobalHotkey as hotkeys
class LocalHotkey(QtCore.QObject):
    activated = QtCore.pyqtSignal()
    last_error = ''
    def __init__(self, parent=None, **kw): super().__init__(parent)
    def register(self, value): return True
    def close(self): pass
hotkeys.GlobalHotkeyManager = LocalHotkey
from PYUI.main_pyui import MainDialog
window = MainDialog()
window.show()
deadline = time.monotonic()+2
while time.monotonic()<deadline:
    app.processEvents()
    time.sleep(.01)
assert window.isVisible()
assert window.grab().save(str(root/'main-window.png'))
plugins = sorted(x['plugin_id'] for x in window.plugin_host.plugin_entries())
assert 'ai_agent_assistant' in plugins
assert 'flow_assistant' in plugins
assert 'batch_text_video' in plugins
import av, cv2, numpy, PIL, torch, torchaudio, onnxruntime, ctranslate2
clip = root/'synthetic.mp4'
with av.open(str(clip)) as container:
    frame = next(container.decode(video=0))
    assert (frame.width, frame.height) == (320, 240)
    assert frame.to_ndarray(format='rgb24')[120,160,2] > 240
capture = cv2.VideoCapture(str(clip))
try:
    ok, pixels = capture.read()
    assert ok and pixels.shape[:2] == (240,320) and pixels[120,160,0] > 240
finally:
    capture.release()
assert torch.equal(torch.ones(2)+1, torch.full((2,), 2.))
from PIL import Image
Image.new('RGBA', (3,3), (10,20,30,200)).save(root/'synthetic.png')
with Image.open(root/'synthetic.png') as picture:
    assert picture.getpixel((1,1)) == (10,20,30,200)
from model.MediaPlayback import QtMediaReviewSurface
surface = QtMediaReviewSurface()
frames, media_errors = [], []
surface.errorOccurred.connect(media_errors.append)
surface.backend.audio_outputs[0].setVolume(0)
surface.backend.video_probes[0].videoFrameChanged.connect(
    lambda frame: frames.append((frame.width(),frame.height())) if frame.isValid() else None)
surface.prepare(str(clip), 0., 1.8, 0.)
surface.play()
deadline = time.monotonic()+15
while not frames and not media_errors and time.monotonic()<deadline:
    app.processEvents()
    time.sleep(.01)
surface.pause()
assert not media_errors, media_errors
assert (320,240) in frames, frames
surface.close()
surface.deleteLater()
window.close()
app.processEvents()
assert not window.isVisible(), 'Fresh idle window did not close cooperatively'
window.deleteLater()
QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
app.processEvents()
assert not errors, errors
from model.AppVersion import APP_VERSION
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION
outputs = {'version': APP_VERSION, 'extension_version': EXTENSION_VERSION,
           'plugins': plugins, 'qt_runtime': QtCore.qVersion(), 'qt_compile': QtCore.QT_VERSION_STR,
           'opencv': cv2.__version__, 'numpy': numpy.__version__,
           'pillow': PIL.__version__, 'torch': str(torch.__version__),
           'torchaudio': str(torchaudio.__version__), 'closed': True,
           'native_media': {'av_decode': True, 'opencv_decode': True,
                            'pillow_png': True, 'torch_cpu': True, 'qt_video_frames': len(frames)}}
sip.delete(app)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--template', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--source-main', type=Path)
    args = parser.parse_args()
    if os.name == 'nt':
        import ctypes
        # A missing native dependency must fail the check, not open a modal
        # Windows error box and leave a headless release job waiting forever.
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='release-frozen-smoke-') as temporary:
        root = Path(temporary)
        (root/'config.example.json').write_bytes(args.template.read_bytes())
        env = os.environ.copy()
        for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'CODEX_HOME', 'HF_HOME'):
            child = root/key
            child.mkdir()
            env[key] = str(child)
        env.update(QT_QPA_PLATFORM='offscreen', RELEASE_SMOKE_ROOT=str(root),
                   HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                   LZX_OFFLINE_MODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        env.pop('PYTHONPATH', None)
        ffmpeg = ((args.source_main.resolve().parent/'runtime/ffmpeg/ffmpeg.exe')
                  if args.source_main else
                  args.executable.resolve().parent/'_internal/runtime/ffmpeg/ffmpeg.exe')
        subprocess.run([str(ffmpeg), '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=blue:s=320x240:r=25:d=2',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
            str(root/'synthetic.mp4')], check=True, capture_output=True, timeout=30, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        command = [str(args.executable)]
        if args.source_main:
            command.append(str(args.source_main.resolve()))
        command.append('--node-python-worker')
        request = json.dumps({'offline': True, 'inputs': {'tag': 'dict', 'items': {}}, 'code': CODE}).encode('utf-8')
        result = subprocess.run(command, input=request, capture_output=True,
                                cwd=root, env=env, timeout=120)
        (args.output/'stderr.txt').write_bytes(result.stderr)
        (args.output/'reply.json').write_bytes(result.stdout)
        assert result.returncode == 0, result.returncode
        reply = json.loads(result.stdout)
        assert 'error' not in reply, reply
        assert reply['outputs']['items']['version'] == args.version.removeprefix('v'), reply
        for name in ('first-use.png', 'main-window.png'):
            (args.output/name).write_bytes((root/name).read_bytes())
        print(json.dumps(reply, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
