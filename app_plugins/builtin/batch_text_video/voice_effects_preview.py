"""Short, cancelable audio-only audition; same mixer as final composition."""
import copy
from pathlib import Path
import threading
from model.AppDataStorage import data_file, data_io
from model.QtThreadLifecycle import connect_thread_finished
from qt_compat import QtCore
from .voice_effects import normalized, active, resolved


def render_audition(source, settings, *, processed=True, start=0., ffmpeg='', cancel=None):
    from .engine import resolve_tools, probe, _run, Cancelled, filter_file_option
    from .mixing import audio_graph
    from .voice_reverb import write_ir
    import math
    if type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 86400:
        raise ValueError('试听起点无效。')
    source = data_file(source).path; stamp = source.stat()
    settings = dict(settings); settings.update(normalized(settings))
    executable, ffprobe = resolve_tools(ffmpeg)
    media = probe(source, ffprobe, audio_only=True)
    if not media['audio']: raise ValueError('所选人声文件没有音轨。')
    if start >= media['duration']: raise ValueError('试听起点已经超过这条人声的长度。')
    clip = min(12., media['duration']-start)
    preset = resolved(settings)
    duration = clip + (min(4., preset['rt']*.8)+.2 if preset else .2)
    if not processed: settings['voice_fx_preset'] = 'none'
    if cancel and cancel.is_set(): raise Cancelled('已取消试听。')
    with data_io.TemporaryDirectory(prefix='voice-space-audition-') as temporary:
        temporary = Path(temporary); target = temporary/'audition.wav'
        args = [executable, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', '-ss', f'{start:.6f}', '-t', f'{clip:.9f}', '-i', str(source)]
        ir_index = None
        if active(settings):
            response = write_ir(temporary/'space.wav', settings, cancel)
            args += ['-i', str(response)]; ir_index = 1
        graph, _ = audio_graph(settings, duration, voice_index=0, voice_ir_index=ir_index)
        script = temporary/'audio.filter'; data_file(script).write_text(';\n'.join(graph), 'utf-8')
        args += [filter_file_option(executable), str(script), '-map', '[a]', '-t', f'{duration:.9f}', '-c:a', 'pcm_s16le', str(target)]
        _run(args, cancel, timeout=90)
        after = source.stat()
        if (stamp.st_size, stamp.st_mtime_ns) != (after.st_size, after.st_mtime_ns): raise ValueError('试听期间人声文件改变，请重新试听。')
        if cancel and cancel.is_set(): raise Cancelled('已取消试听。')
        if target.stat().st_size > 8*1024**2: raise ValueError('试听音频意外过大。')
        return dict(audio=data_file(target).read_bytes(), duration=duration, clip=clip, start=start)


class VoiceEffectPreviewWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)
    def __init__(self, request, parent=None):
        super().__init__(parent); self.request = request; self.cancel = threading.Event()
    def run(self):
        from .engine import Cancelled
        result = dict(serial=self.request['serial'], processed=self.request['processed'])
        try:
            result.update(render_audition(self.request['source'], self.request['settings'], processed=self.request['processed'],
                start=self.request['start'], ffmpeg=self.request['ffmpeg'], cancel=self.cancel))
        except Cancelled: result['cancelled'] = True
        except Exception as error: result['error'] = str(error)
        self.ready.emit(result)


class VoiceEffectController:
    def init_voice_effects(self, card):
        from .voice_effects_ui import VoiceEffectPanel
        self.voice_fx_worker, self._voice_fx_pending, self._voice_fx_player, self._voice_fx_buffer = None, None, None, None
        self._voice_fx_serial = 0
        self.voice_fx_panel = VoiceEffectPanel(self.state['settings'], card)
        card.layout().addWidget(self.voice_fx_panel)
        self.controls.update(self.voice_fx_panel.controls)
        self.voice_fx_panel.auditionRequested.connect(self.audition_voice_effect)
        self.voice_fx_panel.stopRequested.connect(self.stop_voice_effect_preview)
        self.voice_fx_panel.position.valueChanged.connect(self.stop_voice_effect_preview)

    def audition_voice_effect(self, processed=True):
        if self.is_batch_busy(): return
        try:
            from .audio_sources import resolve_voice
            settings = self.read_settings(); settings.update(normalized(settings))
            state = {**self.state, 'settings': settings}
            source = resolve_voice(self.active_entry(), state, discover=True)
            if not source: raise ValueError('请启用人声，并给当前条目分配有效的人声文件。')
            self.stop_playback()
            self._voice_fx_serial += 1
            request = dict(serial=self._voice_fx_serial, source=source, settings=copy.deepcopy(settings),
                           start=self.voice_fx_panel.position.value(), ffmpeg=self.ffmpeg.text().strip(), processed=bool(processed))
            if self.voice_fx_worker:
                self.voice_fx_worker.cancel.set(); self._voice_fx_pending = request
            else: self._start_voice_effect_preview(request)
        except (ValueError, OSError) as error: self.voice_fx_panel.status.setText(str(error))

    def _start_voice_effect_preview(self, request):
        self.voice_fx_worker = VoiceEffectPreviewWorker(request, self)
        self.voice_fx_worker.ready.connect(self._voice_effect_preview_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.voice_fx_worker, self, 'voice_fx_worker', self._voice_effect_preview_finished)
        self.voice_fx_panel.status.setText('正在准备人声试听…再次点试听可切换，停止可取消。')
        self.voice_fx_worker.start()

    def _voice_effect_preview_ready(self, result):
        if result['serial'] != self._voice_fx_serial: return
        if result.get('cancelled'): self.voice_fx_panel.status.setText('试听已取消。'); return
        if result.get('error'): self.voice_fx_panel.status.setText(result['error']); return
        try:
            self._play_voice_effect_audio(result['audio'])
            self.voice_fx_panel.status.setText(('效果试听' if result['processed'] else '原声对比')+
                f"：从{result['start']:.1f}秒起，取{result['clip']:.1f}秒，末尾附带试听留白。")
        except (RuntimeError, OSError) as error: self.voice_fx_panel.status.setText(str(error))

    def _play_voice_effect_audio(self, audio):
        from qt_compat import QtMultimedia
        if QtMultimedia is None: raise RuntimeError('当前安装缺少音频试听组件，请使用“播放预览”检查效果。')
        if self._voice_fx_player is None:
            self._voice_fx_player = QtMultimedia.QMediaPlayer(self)
            self._voice_fx_output = QtMultimedia.QAudioOutput(self)
            self._voice_fx_output.setVolume(1.)
            self._voice_fx_player.setAudioOutput(self._voice_fx_output)
            self._voice_fx_player.errorOccurred.connect(lambda _code, message: self.voice_fx_panel.status.setText('试听播放失败：'+message))
        self._voice_fx_player.stop(); self._voice_fx_player.setSource(QtCore.QUrl())
        if self._voice_fx_buffer: self._voice_fx_buffer.close(); self._voice_fx_buffer.deleteLater()
        self._voice_fx_buffer = QtCore.QBuffer(self)
        self._voice_fx_buffer.setData(QtCore.QByteArray(audio)); self._voice_fx_buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
        self._voice_fx_player.setSourceDevice(self._voice_fx_buffer, QtCore.QUrl('voice-space-preview.wav'))
        self._voice_fx_player.play()

    def _voice_effect_preview_finished(self):
        self.voice_fx_worker = None
        pending, self._voice_fx_pending = self._voice_fx_pending, None
        if pending: self._start_voice_effect_preview(pending)

    def stop_voice_effect_preview(self):
        self._voice_fx_serial = getattr(self, '_voice_fx_serial', 0)+1
        self._voice_fx_pending = None
        worker = getattr(self, 'voice_fx_worker', None)
        if worker: worker.cancel.set()
        player = getattr(self, '_voice_fx_player', None)
        if player: player.stop(); player.setSource(QtCore.QUrl())
        buffer = getattr(self, '_voice_fx_buffer', None)
        if buffer: buffer.close(); buffer.deleteLater(); self._voice_fx_buffer = None
        if (worker or buffer) and getattr(self, 'voice_fx_panel', None): self.voice_fx_panel.status.setText('试听已停止。')
