"""Optional original audio for visual controls, with bounded in-memory joins."""
import subprocess
import tempfile

RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 8  # Stereo float32.


class AudioInput(str):
    def __new__(cls, path, duration, *, source_end, loop_start=0., loop=False, overlap=0., ffmpeg=''):
        from .matting_assets import fingerprint
        value = super().__new__(cls, path)
        value.binding = {'source': fingerprint(path)}
        value.duration, value.source_end = duration, source_end
        value.loop_start, value.loop, value.overlap, value.ffmpeg = loop_start, loop, overlap, ffmpeg
        return value

    def __deepcopy__(self, memo):
        return self

    def options(self):
        return ['-thread_queue_size', '2', '-f', 'f32le', '-ar', str(RATE), '-ac', str(CHANNELS)]


from .matting_live import Feed


class AudioFeed(Feed):
    error_prefix = '素材音频处理失败：'

    def decode(self, start, duration):
        """Decode one interval, padding missing audio to the source video clock."""
        from .matting_models import check_cancel
        from .matting_render import _stop
        spec = self.spec
        args = [spec.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
                '-ss', f'{start:.9f}', '-i', str(spec), '-map', '0:a:0', '-vn',
                '-af', f'aresample={RATE},aformat=sample_fmts=flt:channel_layouts=stereo,apad,atrim=end_sample={max(1, round(duration*RATE))}',
                '-f', 'f32le', '-ar', str(RATE), '-ac', str(CHANNELS), 'pipe:1']
        check_cancel(self.cancel)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            with self._process_lock:
                self.decoder = process
            try:
                while True:
                    check_cancel(self.cancel)
                    chunk = process.stdout.read(4096*SAMPLE_BYTES)
                    if not chunk:
                        break
                    if len(chunk) % SAMPLE_BYTES:
                        raise ValueError('素材音频样本不完整。')
                    yield chunk
                if process.wait() != 0:
                    errors.seek(0)
                    raise ValueError(errors.read(4096).decode('utf8', errors='replace'))
            finally:
                _stop(process)
                process.stdout.close()
                with self._process_lock:
                    if self.decoder is process:
                        self.decoder = None

    def frames(self):
        from contextlib import closing
        from .matting_models import check_cancel
        import numpy as np
        spec = self.spec
        remaining = max(1, round(spec.duration*RATE))
        overlap = round(spec.overlap*RATE) if spec.loop and spec.duration > spec.source_end else 0
        head = np.empty((0, CHANNELS), np.float32)
        if overlap:
            with closing(self.decode(spec.loop_start, overlap/RATE)) as source:
                head = np.frombuffer(b''.join(source), np.float32).reshape(-1, CHANNELS)
            if len(head) != overlap:
                raise ValueError('循环音频头部长度不足。')
        first = True
        while remaining > 0:
            check_cancel(self.cancel)
            start = 0. if first else spec.loop_start+overlap/RATE
            length = spec.source_end-start
            if length <= 0:
                raise ValueError('素材音频循环区间无效。')
            tail = np.empty((0, CHANNELS), np.float32)
            with closing(self.decode(start, length)) as source:
                for chunk in source:
                    data = np.frombuffer(chunk, np.float32).reshape(-1, CHANNELS)
                    tail = np.concatenate((tail, data))
                    count = min(remaining, max(0, len(tail)-overlap))
                    if count:
                        yield tail[:count].tobytes()
                        remaining -= count
                        tail = tail[count:]
                    if remaining <= 0:
                        return
            if overlap:
                if len(tail) != overlap:
                    raise ValueError('循环音频尾部长度不足。')
                weight = np.linspace(0., 1., overlap+2, dtype=np.float32)[1:-1, None]
                blended = tail*(1-weight)+head*weight
                count = min(remaining, overlap)
                yield blended[:count].tobytes()
                remaining -= count
            if not spec.loop:
                return
            first = False


def append(args, filters, specs, input_index, duration, ffmpeg, ffprobe, *, report=None, voice_path=''):
    """Return labels mixed with narration/music; never change project duration."""
    from .engine import probe
    from .visual_loop_region import original_path, selected, validate, identity
    from .matting_video import describe
    labels = []
    for layer, timing, overlap in specs:
        if not layer.get('media_audio_enabled') or layer.get('media_audio_volume', 100) <= 0:
            continue
        path = original_path(layer)
        if voice_path and identity(path) == identity(voice_path):
            if report: report(layer.get('name', '视频')+'：同一素材已用作人声，不重复叠音。')
            continue
        info = probe(path, ffprobe)
        if not info.get('audio'):
            if report: report(layer.get('name', '视频')+'：素材没有音轨。')
            continue
        source_end, loop_start = info['duration'], 0.
        region = selected(layer)
        if region:
            validate(region, describe(info))
            source_end, loop_start = region['end'], region['start']
        local_duration = timing.source_duration(duration) if timing else duration
        spec = AudioInput(path, local_duration, source_end=source_end, loop_start=loop_start,
            loop=layer.get('media_loop', True), overlap=overlap, ffmpeg=ffmpeg)
        args += ['-i', spec]
        label = 'visual_audio_'+str(input_index)
        chain = f'[{input_index}:a]asetpts=PTS-STARTPTS,volume={layer.get("media_audio_volume",100)/100:.9f}'
        if timing:
            start = timing.start if timing.clock == 'global' else 0.
            span = timing.end-timing.start
            chain += f',atrim=start={start:.9f}:duration={span:.9f},asetpts=PTS-STARTPTS'
            if timing.fade_in:
                chain += f',afade=t=in:d={timing.fade_in:.9f}'
            if timing.fade_out:
                chain += f',afade=t=out:st={span-timing.fade_out:.9f}:d={timing.fade_out:.9f}'
            chain += f',adelay={round(timing.start*RATE)}S:all=1'
        filters.append(chain+f',apad=whole_dur={duration:.9f},atrim=duration={duration:.9f}[{label}]')
        labels.append('['+label+']')
        input_index += 1
    return input_index, labels
