"""Timestamp-bearing ElevenLabs responses and sample-accurate long joins."""
import base64
from pathlib import Path
import subprocess
import os
import wave

from model.AppDataStorage import data_io, data_file
from model.ElevenLabsTimings import TimedAudio, character_words, field, validate_words


def timed_response(response):
    # JSON uses audio_base64; the official Python SDK exposes audio_base_64.
    encoded = field(response,'audio_base64') or field(response,'audio_base_64')
    if not isinstance(encoded,str) or len(encoded) > 160*1024*1024:
        raise ValueError('ELE 音频响应为空或过大，未保存。')
    raw = base64.b64decode(encoded,validate=True)
    if not raw: raise ValueError('ELE 返回空音频。')
    result = TimedAudio((raw,))
    try: result.words = character_words(field(response,'alignment'))
    except (ValueError,TypeError,KeyError):
        result.warning = 'ELE 已返回音频，但没有有效的原文逐词时间；未伪造字幕，也不会再请求一次生成。'
    return result


def _run(args):
    result = subprocess.run(args,capture_output=True,timeout=300,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode: raise RuntimeError('FFmpeg 音频处理失败，未替换原音频。')
    return result.stdout


def duration(path, ffmpeg):
    import json
    probe = str(Path(ffmpeg).with_name('ffprobe.exe' if os.name=='nt' else 'ffprobe'))
    raw = _run([probe,'-hide_banner','-loglevel','error','-show_entries','format=duration','-of','json',str(path)])
    return float(json.loads(raw)['format']['duration'])


def dialogue_audio(client, chunks, filename, voice_id, model_id, ffmpeg, report):
    from elevenlabs import DialogueInput
    def request(text):
        return timed_response(client.text_to_dialogue.convert_with_timestamps(
            inputs=[DialogueInput(text=text,voice_id=voice_id)],model_id=model_id,
            output_format='mp3_44100_128',request_options={'max_retries':0,'timeout_in_seconds':180}))
    if len(chunks)==1: return request(chunks[0])
    result=TimedAudio(None)
    result.completed_requests=0
    def generate():
        with data_io.TemporaryDirectory(prefix='.ele-audio-',dir=str(Path(filename).parent)) as temporary:
            folder=Path(temporary); names=[]; words=[]; offset=0.; complete=True
            for index,text in enumerate(chunks,1):
                report(f'ELE 长文音频与时间戳 {index}/{len(chunks)}')
                speech=request(text)
                result.completed_requests+=1
                mp3=folder/f'part-{index:04}.mp3'; wav=mp3.with_suffix('.wav')
                data_file(mp3).write_bytes(b''.join(speech))
                _run([ffmpeg,'-hide_banner','-nostdin','-loglevel','error','-y','-i',str(mp3),
                    '-vn','-ar','44100','-c:a','pcm_s16le',str(wav)])
                with data_file(wav).open('rb') as handle, wave.open(handle,'rb') as audio:
                    seconds=audio.getnframes()/audio.getframerate()
                if speech.words is None: complete=False
                else:
                    try:
                        words.extend(dict(word,start=word['start']+offset,end=word['end']+offset)
                            for word in validate_words(speech.words,seconds))
                    except ValueError: complete=False
                offset+=seconds; names.append(wav.name)
            manifest=folder/'parts.txt'
            data_file(manifest).write_text_direct('\n'.join("file '"+name+"'" for name in names),'utf8')
            combined=folder/'combined.mp3'
            _run([ffmpeg,'-hide_banner','-nostdin','-loglevel','error','-y','-f','concat','-safe','1',
                '-i',str(manifest),'-vn','-c:a','libmp3lame','-b:a','128k','-ar','44100',str(combined)])
            if complete: result.words=words
            else: result.warning='长文音频已完整合并，但部分分段缺少有效时间戳，未拼凑不完整的动态字幕。'
            with data_file(combined).open('rb') as stream:
                while chunk:=stream.read(256*1024): yield chunk
    result.chunks=generate()
    return result
