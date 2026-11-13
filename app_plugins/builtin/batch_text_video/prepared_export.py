"""Only final FFmpeg work crosses threads; publishing stays on the coordinator."""
from dataclasses import dataclass, field
from pathlib import Path
import time


@dataclass
class PreparedExport:
    args: list
    target: Path
    output: Path
    ffprobe: str
    duration: float
    has_audio: bool
    music: object
    sequence_plan: list
    cancel: object
    progress: object
    report: object
    overwrite: bool
    encoder: str
    encoder_fallback: bool
    preview: bool
    workers: int
    effects: object = None
    cwd: object = None
    timings: dict = field(default_factory=dict)
    encoded: bool = False
    quality: str = 'high'
    render_scope: str = ''
    source_paths: tuple = field(default_factory=tuple)
    assignment: dict = field(default_factory=dict)
    requested_output: object = None
    caption_memory_bytes: int = 0
    render_config: dict = field(default_factory=dict)

    def encode(self):
        from .render_slots import render_slot
        from .render_progress import stage
        from .memory_pressure import ADMISSION,estimate
        from .media_errors import memory_failure,describe,MediaMemoryError
        amount=estimate(self.args)+self.caption_memory_bytes
        for attempt in range(4):
            stage(self.progress, '等待编码名额')
            try:
                with render_slot(self.render_scope,self.workers,self.cancel,self.report,
                                 memory_bytes=amount*(attempt+1),progress=self.progress):
                    stage(self.progress,'编码中')
                    return self._encode()
            except Exception as error:
                if not memory_failure(error):raise
                if self.cancel and self.cancel.is_set():
                    from .engine import Cancelled
                    raise Cancelled('已取消等待内存释放。') from error
                if attempt==3:
                    raise MediaMemoryError('释放资源后连续重试仍无法分配内存。'+describe(error)) from error
                ADMISSION.failed()
                stage(self.progress,'等待内存释放')
                if self.progress:self.progress(0)
                if self.report:self.report('内存分配失败，已回收本次编码进程；等待内存释放后按原素材重试，不标记失败。')

    def _encode(self):
        from .engine import _run, probe, Cancelled
        from model.MediaEncoding import hardware_error, video_encoding_args, encoder_label
        from .encoding_watchdog import EncodingStalled, serial_filters
        started = time.monotonic()
        try:
            _run(self.args, self.cancel, self.progress, self.duration,
                 timeout=max(600, self.duration*30), cwd=self.cwd)
        except RuntimeError as error:
            from .media_errors import MediaMemoryError, memory_failure, describe
            if memory_failure(error):
                raise MediaMemoryError(describe(error)) from error
            stalled = isinstance(error, EncodingStalled)
            if not self.encoder_fallback or (not stalled and (self.encoder == 'cpu' or not hardware_error(error))):
                raise
            if stalled:
                self.report(str(error)+' 本条使用CPU及单滤镜线程重试一次，保留全部画面和音频效果。')
                from .render_progress import stage
                stage(self.progress, '停滞恢复：CPU重试')
                if self.progress: self.progress(0)
            else:
                self.report(encoder_label(self.encoder)+'编码初始化／会话失败，本条使用CPU重新编码；不更改画布或滤镜。')
            old = video_encoding_args(self.encoder, preview=self.preview, workers=self.workers, quality=self.quality)
            start = next((i for i in range(len(self.args)) if self.args[i:i+len(old)] == old), None)
            if start is None:
                raise RuntimeError('找不到明确的编码参数，未盲目替换命令。') from error
            args = self.args[:start] + video_encoding_args('cpu', preview=self.preview, workers=self.workers, quality=self.quality) + self.args[start+len(old):]
            if self.encoder == 'qsv':
                pixel = args.index('-pix_fmt', start)
                args[pixel+1] = 'yuv420p'
            if stalled:
                args = serial_filters(args)
            _run(args, self.cancel, self.progress, self.duration,
                 timeout=max(600, self.duration*30), cwd=self.cwd)
            self.encoder = 'cpu'
        self.timings['encode_seconds'] = time.monotonic()-started
        started = time.monotonic()
        from .render_progress import stage
        stage(self.progress, '校验成品')
        if self.effects is not None:
            self.effects.verify()
        check = probe(self.target, self.ffprobe)
        if abs(check['duration']-self.duration) > max(.12, self.duration*.001):
            raise RuntimeError(f"成品时长{check['duration']:.3f}秒与目标{self.duration:.3f}秒不一致，未发布该成品。")
        if self.has_audio and not check['audio']:
            raise RuntimeError('成品音轨缺失，未发布该成品。')
        if self.cancel and self.cancel.is_set():
            raise Cancelled('已取消，未发布临时成品。')
        self.timings['verify_seconds'] = time.monotonic()-started
        self.encoded = True
        stage(self.progress, '等待保存')
        return self

    def publish(self):
        from model.AppDataStorage import data_io
        from model.MediaEncoding import encoder_label
        from .engine import Cancelled, safe_output
        from .render_progress import stage
        stage(self.progress, '保存成品')
        if not self.encoded:
            raise RuntimeError('视频尚未编码校验，不能提交成品。')
        if self.cancel and self.cancel.is_set():
            raise Cancelled('已取消，未发布临时成品。')
        if self.effects is not None:
            self.effects.verify()
        from .export_destination import publish
        publish(self)
        self.report(f"导出耗时：准备 {self.timings.get('prepare_seconds', 0):.2f}秒；"
            f"编码 {self.timings.get('encode_seconds', 0):.2f}秒；校验 {self.timings.get('verify_seconds', 0):.2f}秒；"
            f"实际编码器 {encoder_label(self.encoder)}。")
        return self.output
