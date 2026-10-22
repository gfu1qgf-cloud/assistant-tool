"""Deterministic synthetic room responses, generated locally without downloads."""
from pathlib import Path
import wave
from model.AppDataStorage import data_file
from .voice_effects import resolved

SAMPLE_RATE = 48000


def impulse_response(settings, cancel=None):
    import numpy as np
    from .engine import Cancelled
    def check():
        if cancel and cancel.is_set(): raise Cancelled('已取消人声特效准备。')
    check(); p = resolved(settings)
    if p is None: return None
    rt = max(.1, p['rt']); delay = round(p['delay']*SAMPLE_RATE/1000)
    length = max(2048, round(rt*1.25*SAMPLE_RATE))
    nfft = 1 << (length-1).bit_length()
    random = np.random.default_rng(p['seed'])
    common = random.standard_normal(length)
    width = p['width']
    noise = np.stack([common*(1-.7*width**2)**.5 + random.standard_normal(length)*(.7**.5*width)
                      for _ in range(2)], axis=1)
    spectrum = np.fft.rfft(noise, n=nfft, axis=0)
    frequencies = np.fft.rfftfreq(nfft, 1/SAMPLE_RATE)
    t = np.arange(length)/SAMPLE_RATE
    response = np.zeros((length, 2), np.float64)
    # Independent frequency bands lose treble more quickly, as in an absorbed
    # room. A short build-up gives reflections density rather than word repeats.
    tone = (1-np.exp(-(frequencies/110)**2))/(1+(frequencies/p['tone'])**6)**.5
    for center, gain, decay in ((350, .48, 1.), (1100, .68, .87), (3300, .50, .64), (8500, .22, .43)):
        check()
        band = np.exp(-.5*(np.log2(np.maximum(1., frequencies)/center)/.85)**2)*tone
        colored = np.fft.irfft(spectrum*band[:, None], n=nfft, axis=0)[:length]
        envelope = np.exp(-np.log(1000)*t/(rt*decay))*(1-np.exp(-t/.025))
        colored *= envelope[:, None]
        energy = np.sqrt(np.mean(np.sum(colored*colored, axis=0)))
        response += colored*(gain/max(energy, 1e-12))
    end = min(length, round(.03*SAMPLE_RATE))
    response[-end:] *= np.linspace(1, 0, end)[:, None]
    response *= .75
    room_scale = min(1.8, max(.5, (rt/2.4)**.5))
    for index, (offset, gain) in enumerate(((0, .28), (.011, .22), (.027, .17), (.047, .12), (.079, .08))):
        position = round(offset*room_scale*SAMPLE_RATE)
        if position < length:
            pan = (.22 if index%2 else -.22)*width
            response[position] += gain*np.array([1-pan, 1+pan])
    for offset, gain in p.get('echoes', ()):
        position = round(offset*SAMPLE_RATE)
        if position < length: response[position] += gain
    # Calibrate against a speech-shaped spectrum, keeping preset wet gains
    # meaningful across dark/bright spaces without normalising the dry voice.
    check()
    transfer = np.fft.rfft(response, n=nfft, axis=0)
    weights = (1-np.exp(-(frequencies/90)**2))/(frequencies+180)/(1+(frequencies/9000)**4)
    energy = np.sqrt(np.sum(np.abs(transfer)**2*weights[:, None])/max(1e-12, 2*weights.sum()))
    response /= max(energy, 1e-12)
    peak = float(np.abs(response).max())
    if peak > .98: response *= .98/peak
    result = np.zeros((length+delay, 2), np.float32); result[delay:] = response
    check(); return result


def write_ir(path, settings, cancel=None):
    import numpy as np
    response = impulse_response(settings, cancel)
    if response is None: return None
    # 24-bit PCM preserves quiet late coefficients; no scipy/audio dependency.
    integers = np.rint(np.clip(response, -.999999, .999999)*8388607).astype('<i4')
    packed = integers.reshape(-1).view(np.uint8).reshape(-1, 4)[:, :3].copy().tobytes()
    with data_file(path).open('wb') as output, wave.open(output, 'wb') as stream:
        stream.setnchannels(2); stream.setsampwidth(3); stream.setframerate(SAMPLE_RATE); stream.writeframes(packed)
    return Path(path)
