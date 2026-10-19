"""Typed final-encode policy shared by node inspectors and all render paths."""
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
import math

from .contracts import NodeParameter

PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow')


@dataclass(frozen=True)
class EncodingSettings:
    codec: str = 'libx264'
    preset: str = 'medium'
    crf: float = 30
    audio: str = 'mp3'
    audio_kbps: int = 128
    rate_control: str = 'crf'
    qp: int = 30
    video_kbps: int = 2000
    audio_mode: str = 'auto'
    sample_rate: int = 44100
    channels: int = 2
    frame_rate_enabled: bool = False
    frame_rate: float = 30
    cfr: bool = False
    keyframe_enabled: bool = False
    keyframe_seconds: float = 2
    resize_enabled: bool = False
    width: int = 1280
    height: int = 720
    resize_mode: str = 'fit'
    profile: str = 'auto'
    tune: str = 'none'
    strip_metadata: bool = False
    faststart: bool = True

    def __post_init__(self):
        options = {'codec': {'libx264','libx265'}, 'preset': set(PRESETS), 'audio': {'aac','mp3','copy'},
                   'rate_control': {'crf','qp','bitrate'}, 'audio_mode': {'auto','cbr','abr'},
                   'resize_mode': {'fit','pad','crop','stretch'}, 'profile': {'auto','baseline','main','high'},
                   'tune': {'none','film','animation','grain','stillimage','fastdecode','zerolatency'}}
        for key, choices in options.items():
            if not isinstance(getattr(self,key),str) or getattr(self,key) not in choices:
                raise ValueError(f'编码选项 {key} 无效；不接受原始命令或表达式。')
        for key in ('frame_rate_enabled','cfr','keyframe_enabled','resize_enabled','strip_metadata','faststart'):
            if type(getattr(self,key)) is not bool: raise ValueError(f'{key} 必须为开关。')
        for key, low, high in (('crf',0,51),('frame_rate',1,240),('keyframe_seconds',.1,600)):
            value = getattr(self,key)
            if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{key} 超出范围或不是有限数值。')
        for key, low, high in (('qp',0,51),('video_kbps',32,1000000),('audio_kbps',32,512),
                               ('width',2,8192),('height',2,8192)):
            if type(getattr(self,key)) is not int or not low <= getattr(self,key) <= high:
                raise ValueError(f'{key} 超出范围。')
        if type(self.channels) is not int or self.channels not in {0,1,2}:
            raise ValueError('声道只能为原版、单声道或立体声。')
        if type(self.sample_rate) is not int or self.sample_rate not in {0,8000,11025,12000,16000,22050,24000,32000,44100,48000}:
            raise ValueError('音频采样率无效。')
        if self.audio == 'mp3' and self.audio_kbps > 320:
            raise ValueError('MP3 码率不能超过320 kbps。')
        if self.audio == 'mp3':
            allowed = {32,40,48,56,64,80,96,112,128,160,192,224,256,320}
            if self.sample_rate and self.sample_rate < 32000:
                allowed = {32,40,48,56,64,80,96,112,128,144,160}
                if self.sample_rate < 16000: allowed = {32,40,48,56,64}
            if self.audio_kbps not in allowed:
                raise ValueError('MP3 码率与采样率组合不支持，请使用标准码率，如128kbps／44100Hz。')
        if self.resize_enabled and (self.width % 2 or self.height % 2):
            raise ValueError('H.264／H.265 输出宽高请使用偶数。')
        if self.codec == 'libx265' and (self.profile not in {'auto','main'} or self.tune in {'film','stillimage'}):
            raise ValueError('H.265 不支持此 H.264 Profile／Tune，请选择自动或对应选项。')


def from_parameters(params):
    values = {field.name: params[field.name] for field in fields(EncodingSettings) if field.name in params}
    if {'codec','preset','crf','audio','audio_kbps'} <= params.keys():
        # Old nodes did not request resampling/downmixing. New nodes explicitly
        # carry the screenshot defaults; merely opening an old graph must not.
        values.setdefault('sample_rate',0)
        values.setdefault('channels',0)
    return EncodingSettings(**values)


def checked(settings):
    if isinstance(settings,EncodingSettings): return settings
    # A running process may lazily import a renderer after source installation.
    # Accept its already-loaded five-field contract without hot-reloading it.
    cls=type(settings)
    if is_dataclass(settings) and cls.__name__=='EncodingSettings' and cls.__module__=='model.node_workflow.video_stream':
        return from_parameters(asdict(settings))
    raise TypeError('编码设置必须来自压缩节点。')


def video_encoder_args(settings=None):
    if settings is None:
        return ['-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p']
    settings=checked(settings)
    args = ['-c:v',settings.codec,'-preset',settings.preset]
    args += (['-crf',f'{settings.crf:g}'] if settings.rate_control == 'crf' else
             ['-qp',str(settings.qp)] if settings.rate_control == 'qp' else ['-b:v',f'{settings.video_kbps}k'])
    if settings.profile != 'auto': args += ['-profile:v',settings.profile]
    if settings.tune != 'none': args += ['-tune',settings.tune]
    if settings.cfr: args += ['-fps_mode','cfr']
    if settings.keyframe_enabled:
        args += ['-force_key_frames',f'expr:gte(t,n_forced*{settings.keyframe_seconds:g})']
    return args + ['-pix_fmt','yuv420p']


def audio_encoder_args(settings=None, *, timeline=False):
    if settings is None:
        return ['-c:a','aac','-b:a','192k','-ar','48000','-ac','2'] if timeline else ['-c:a','copy']
    settings=checked(settings)
    if settings.audio == 'copy' and not timeline: return ['-c:a','copy']
    codec = 'libmp3lame' if settings.audio == 'mp3' else 'aac'
    args = ['-c:a',codec,'-b:a',f'{settings.audio_kbps}k']
    if codec == 'libmp3lame' and settings.audio_mode != 'auto':
        args += ['-abr','1' if settings.audio_mode == 'abr' else '0']
    if settings.sample_rate: args += ['-ar',str(settings.sample_rate)]
    if settings.channels: args += ['-ac',str(settings.channels)]
    return args


def mux_args(settings=None):
    if settings is not None: settings=checked(settings)
    args = ['-movflags','+faststart'] if settings is None or settings.faststart else []
    if settings is not None and settings.strip_metadata:
        args += ['-map_metadata','-1','-map_chapters','-1']
    return args


def output_filters(chain, settings):
    if settings is None: return chain
    settings=checked(settings)
    filters = []
    if settings.resize_enabled:
        size = f'{settings.width}:{settings.height}'
        if settings.resize_mode == 'stretch': filters.append('scale='+size)
        elif settings.resize_mode == 'crop':
            filters.append('scale='+size+':force_original_aspect_ratio=increase,crop='+size)
        else:
            filters.append('scale='+size+':force_original_aspect_ratio=decrease:force_divisible_by=2')
            if settings.resize_mode == 'pad': filters.append('pad='+size+':(ow-iw)/2:(oh-ih)/2')
        filters.append('setsar=1')
    if settings.frame_rate_enabled: filters.append(f'fps={settings.frame_rate:g}')
    if not filters: return chain
    from .filter_graph import VideoFilterChain
    extra = ','.join(filters)
    if isinstance(chain,VideoFilterChain):
        return replace(chain,after=','.join(value for value in (chain.after,extra) if value))
    return chain+','+extra


def summary(settings):
    settings=checked(settings)
    mode = {'crf':f'质量 CRF {settings.crf:g}', 'qp':f'量化器 QP {settings.qp}',
            'bitrate':f'目标码率 {settings.video_kbps} kbps'}[settings.rate_control]
    audio = ('保留音频编码' if settings.audio == 'copy' else
             f'{settings.audio.upper()} {settings.audio_kbps} kbps / '+
             ('原采样率' if not settings.sample_rate else f'{settings.sample_rate} Hz')+' / '+
             {0:'原声道',1:'单声道',2:'立体声'}[settings.channels])
    return f'MP4 · {"H.264" if settings.codec == "libx264" else "H.265"} · {mode} · {settings.preset}；{audio}'


PARAMETERS = (
    NodeParameter('codec','编码器','choice','libx264',choices=(('libx264','H.264'),('libx265','H.265'))),
    NodeParameter('preset','编码速度','choice','medium',choices=tuple((p,p) for p in PRESETS)),
    NodeParameter('rate_control','视频控制方式','choice','crf',choices=(('crf','质量 CRF'),('qp','量化器 QP'),('bitrate','目标码率'))),
    NodeParameter('crf','视频质量 · 越小越清晰','number',30,0,51),
    NodeParameter('qp','量化器','integer',30,0,51), NodeParameter('video_kbps','视频码率 kbps','integer',2000,32,1000000),
    NodeParameter('audio','音频编码器','choice','mp3',choices=(('mp3','MP3'),('aac','AAC'),('copy','保留原编码'))),
    NodeParameter('audio_kbps','音频码率 kbps','integer',128,32,512),
    NodeParameter('audio_mode','MP3 模式','choice','auto',choices=(('auto','Auto · 按指定码率'),('cbr','CBR'),('abr','ABR'))),
    NodeParameter('sample_rate','采样率 Hz','integer',44100,0,48000), NodeParameter('channels','声道','integer',2,0,2),
    NodeParameter('frame_rate_enabled','修改帧率','boolean',False), NodeParameter('frame_rate','帧率','number',30,1,240),
    NodeParameter('cfr','恒定帧率 CFR','boolean',False), NodeParameter('keyframe_enabled','指定关键帧间隔','boolean',False),
    NodeParameter('keyframe_seconds','关键帧间隔 · 秒','number',2,.1,600),
    NodeParameter('resize_enabled','修改画面尺寸','boolean',False),
    NodeParameter('width','宽度','integer',1280,2,8192), NodeParameter('height','高度','integer',720,2,8192),
    NodeParameter('resize_mode','尺寸适配','choice','fit',choices=(('fit','保持比例 · 完整画面'),('pad','保持比例 · 补边'),('crop','铺满 · 裁切'),('stretch','拉伸'))),
    NodeParameter('profile','Profile','choice','auto',choices=(('auto','自动'),('baseline','Baseline'),('main','Main'),('high','High'))),
    NodeParameter('tune','Tune','choice','none',choices=tuple((v,v) for v in ('none','film','animation','grain','stillimage','fastdecode','zerolatency'))),
    NodeParameter('strip_metadata','移除元数据／章节','boolean',False), NodeParameter('faststart','网络快速起播','boolean',True))
