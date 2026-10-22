"""Verified local ONNX models. Inference never uploads media or runs remote code."""
from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import threading
import urllib.request

from model.AppDataStorage import data_file, data_directory, data_io

MODELS = {
    'isnet': dict(filename='isnet-general-use.onnx', size=178648008,
        sha256='60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a',
        url='https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx'),
    'rvm': dict(filename='rvm_mobilenetv3_fp32.onnx', size=14975696,
        sha256='88d4531297118f595bf2fd60f6f566aec2e559393802d1f436c380f0cbbd2828',
        url='https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx'),
}
_SESSIONS = OrderedDict()
_VERIFIED = set()
_LOCK = threading.RLock()


class MattingCancelled(RuntimeError): pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set(): raise MattingCancelled('抠像已取消。')


def model_directory():
    from app_paths import DATA_ROOT
    return data_directory(DATA_ROOT/'BatchTextVideo/Matting/models').path


def ensure_model(name, root=None, cancel=None, report=None):
    if name not in MODELS: raise ValueError('未知AI抠像模型。')
    report = report or (lambda _message: None)
    spec = MODELS[name]; root = Path(root) if root else model_directory()
    target = root/spec['filename']
    check_cancel(cancel)
    if not target.is_file():
        data_directory(root).ensure()
        report('首次下载本地模型：'+spec['filename']+f"（约{spec['size']/1024**2:.0f}MB，不上传素材）")
        handle, temporary = data_io.mkstemp(prefix='model-', suffix='.download', dir=root)
        os.close(handle)
        try:
            digest, count = hashlib.sha256(), 0
            request = urllib.request.Request(spec['url'], headers={'User-Agent': 'BatchTextVideo-Matting/1.0'})
            with urllib.request.urlopen(request, timeout=30) as response, data_file(temporary).open('wb') as stream:
                while True:
                    check_cancel(cancel)
                    chunk = response.read(1024*1024)
                    if not chunk: break
                    count += len(chunk)
                    if count > spec['size']: raise ValueError('模型下载大小异常。')
                    stream.write(chunk); digest.update(chunk)
                    report(f"下载模型 {count/spec['size']:.0%}")
            if count != spec['size'] or digest.hexdigest() != spec['sha256']:
                raise ValueError('模型校验失败，未启用。请重试下载。')
            data_io.replace(temporary, target)
        finally:
            if Path(temporary).exists(): data_file(temporary).remove()
    info = target.stat(); token = (str(target.resolve()), info.st_size, info.st_mtime_ns)
    with _LOCK:
        if token not in _VERIFIED:
            digest = hashlib.sha256()
            with data_file(target).open('rb') as stream:
                while True:
                    check_cancel(cancel); chunk = stream.read(1024*1024)
                    if not chunk: break
                    digest.update(chunk)
            if info.st_size != spec['size'] or digest.hexdigest() != spec['sha256']:
                raise ValueError('本地AI模型损坏，请移走该模型后重新下载：'+str(target))
            _VERIFIED.add(token)
    return target


def session(name, device='auto', root=None, cancel=None, report=None):
    try:
        import onnxruntime as ort
    except ImportError as error:
        raise RuntimeError('当前程序缺少ONNX推理库，无法运行AI抠像。') from error
    path = ensure_model(name, root, cancel, report)
    available = ort.get_available_providers()
    gpu = device != 'cpu' and 'CUDAExecutionProvider' in available
    if device == 'cuda' and not gpu: raise RuntimeError('当前运行环境没有CUDA推理支持，请选择自动／CPU。')
    providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if gpu else ['CPUExecutionProvider']
    key = (str(path.resolve()), tuple(providers))
    with _LOCK:
        if key not in _SESSIONS:
            options = ort.SessionOptions(); options.intra_op_num_threads = min(8, os.cpu_count() or 2)
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            _SESSIONS[key] = ort.InferenceSession(str(path), sess_options=options, providers=providers)
            while len(_SESSIONS) > 2: _SESSIONS.popitem(last=False)
        _SESSIONS.move_to_end(key)
        value = _SESSIONS[key]
    actual = 'CUDA显卡' if 'CUDAExecutionProvider' in value.get_providers() else 'CPU'
    if device == 'cuda' and actual == 'CPU': raise RuntimeError('CUDA初始化失败，请选择CPU或检查显卡运行环境。')
    if report: report('本地AI运行设备：'+actual)
    return value, actual


class Predictor:
    def __init__(self, name, device='auto', quality='better', root=None, cancel=None, report=None):
        self.name, self.quality, self.cancel = name, quality, cancel
        self.session, self.device = session(name, device, root, cancel, report)
        self.rec = None

    def predict(self, rgb):
        import cv2
        import numpy as np
        check_cancel(self.cancel)
        h, w = rgb.shape[:2]
        if self.name == 'isnet':
            sample = cv2.resize(rgb, (1024, 1024), interpolation=cv2.INTER_AREA).astype(np.float32)/255-.5
            tensor = np.ascontiguousarray(sample.transpose(2, 0, 1)[None])
            output = self.session.run(None, {self.session.get_inputs()[0].name: tensor})[0][0, 0]
            low, high = float(output.min()), float(output.max())
            if high-low > .000001: output = (output-low)/(high-low)
            alpha = cv2.resize(output, (w, h), interpolation=cv2.INTER_LINEAR)
            foreground = rgb
        else:
            if self.rec is None: self.rec = [np.zeros((1, 1, 1, 1), np.float32) for _ in range(4)]
            tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None].astype(np.float32)/255)
            ratio = min(1., (512 if self.quality == 'better' else 320)/max(h, w))
            inputs = dict(src=tensor, downsample_ratio=np.array([ratio], np.float32))
            inputs.update({f'r{i+1}i': value for i, value in enumerate(self.rec)})
            foreground, alpha, *self.rec = self.session.run(None, inputs)
            foreground = np.rint(np.clip(foreground[0].transpose(1, 2, 0), 0, 1)*255).astype(np.uint8)
            alpha = alpha[0, 0]
        check_cancel(self.cancel)
        return foreground, np.clip(alpha, 0, 1).astype(np.float32)
