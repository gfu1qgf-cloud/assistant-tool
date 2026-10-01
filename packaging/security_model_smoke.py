"""Offline compatibility checks using cached models and synthetic inputs only."""
import os
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['USE_TF'] = '0'
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'
import json
from pathlib import Path
import sys
import tempfile
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import torch
torch.set_num_threads(4)
mode, output = sys.argv[1:3]
result = {}

if mode == 'clip':
    from app_plugins.builtin.image_classifier.classifier import ImageClassifierEngine
    # Use the actual application loader, including pinned safe-weight revision.
    from app_plugins.builtin.image_classifier.classifier import normalize_image_classifier_settings
    engine = ImageClassifierEngine(normalize_image_classifier_settings({'model': 'base', 'device': 'cpu'}))
    _, Image, _, model, processor, device = engine._load_runtime()
    from model.ModelFeatures import feature_tensor
    with torch.inference_mode():
        values = feature_tensor(model.get_text_features(**processor(
            text=['a red square', 'a blue sky'], padding=True, return_tensors='pt')))
        result['text'] = values.detach().cpu().numpy()
        values = feature_tensor(model.get_image_features(**processor(
            images=[Image.new('RGB', (64, 96), 'red')], return_tensors='pt')))
        result['image'] = values.detach().cpu().numpy()
elif mode == 'image':
    from app_plugins.builtin.smart_image_search.encoder import ChineseImageEncoder
    from PIL import Image
    encoder = ChineseImageEncoder('base')
    result['text'] = encoder.text('教堂中的人')
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/'synthetic.png'
        Image.new('RGB', (64, 96), 'red').save(path)
        result['image'] = encoder.images([path])[0]
elif mode == 'music':
    from app_plugins.builtin.smart_music_search.encoder import MusicEncoder
    encoder = MusicEncoder()
    result['text'] = encoder.text('恐怖')[0]
    result['audio'] = encoder.audio(np.zeros(48000*2, dtype=np.float32))
    result['translation'] = np.array([encoder.translate('悲伤而安静的钢琴音乐')])
elif mode == 'audio':
    import torchaudio
    import stable_whisper
    import faster_whisper
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    samples = np.zeros(16000*2, dtype=np.float32)
    assert get_speech_timestamps(samples, VadOptions()) == []
    result['resample'] = torchaudio.functional.resample(torch.zeros(1,1600),16000,8000).numpy()
    cache = Path.home()/'.cache'/'huggingface'/'hub'/'models--Systran--faster-whisper-base'
    cached = next(path for path in (cache/'snapshots').glob('*') if (path/'model.bin').is_file())
    model = faster_whisper.WhisperModel(str(cached), device='cpu', compute_type='int8')
    segments, info = model.transcribe(samples, language='sk', vad_filter=True)
    result['whisper_segments'] = np.array([len(list(segments))])
else:
    raise ValueError(mode)

for name, value in result.items():
    if value.dtype.kind not in ('U', 'S'):
        assert np.all(np.isfinite(value)), name
np.savez(output, **result)
print(json.dumps({'mode': mode, 'torch': torch.__version__,
    'results': {name: list(value.shape) for name, value in result.items()}}, ensure_ascii=False))
