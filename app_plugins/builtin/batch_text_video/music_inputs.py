"""Seek directly to the music needed by this output, inside its final encode."""
import math
from pathlib import Path

from .music_analysis import timeline_ranges


def append_pieces(args, graph, path, pieces, index, label='music_source'):
    for number, (start, end) in enumerate(pieces):
        length = end-start
        # Accurate input seeking avoids decoding every preceding minute again
        # for each successive video. Give decoders a small end cushion; trim
        # the actual samples before joining so there is no accumulated gap.
        args += ['-threads', '1', '-ss', f'{start:.9f}', '-t', f'{length+.1:.9f}', '-i', str(path)]
        graph.append(f'[{index+number}:a]atrim=duration={length:.9f},asetpts=PTS-STARTPTS,'
                     f'aresample=48000,aformat=channel_layouts=stereo,'
                     f'apad=whole_dur={length:.9f},atrim=duration={length:.9f}[music_piece{number}]')
    graph.append(''.join(f'[music_piece{i}]' for i in range(len(pieces))) +
                 f'concat=n={len(pieces)}:v=0:a=1[{label}]')
    return len(pieces)


def append_inputs(args, graph, music, index, duration, cache, ffmpeg, ffprobe, cancel, report, leases):
    ranges = music.get('source_ranges') or ((0., music['duration']),)
    pieces = timeline_ranges(ranges, music['start'], duration)
    if pieces is not None:
        count = append_pieces(args, graph, music['path'], pieces, index)
        report(f'音乐按需读取：{len(pieces)}段，共{duration:.2f}秒（跳过整曲转码）。')
        return count, '[music_source]', 0.
    path = Path(music['path'])
    if music.get('source_ranges'):
        from .engine import _materialize_music_cycle, Cancelled
        from .music_preparation import processing_slot
        from model.MediaCacheLeases import media_cache_build, media_cache_lease, trim_generated_cache
        key = music['audio_key']
        cleaned = Path(cache) / (key + '.flac')
        metadata = cleaned.with_suffix('.json')
        leases.callback(trim_generated_cache, cleaned.parent, '.flac', 1024**3)
        leases.enter_context(media_cache_lease(cleaned))
        try:
            with media_cache_build(cleaned, cancel):
                if not cleaned.is_file() or not cleaned.stat().st_size:
                    report('当前音乐切段／循环较多，建立一次可复用的音乐循环。')
                    with processing_slot(cancel):
                        _materialize_music_cycle(path, ranges, cleaned.parent, ffmpeg, ffprobe,
                                                 cancel, cleaned, metadata, key)
        except InterruptedError as error:
            raise Cancelled(str(error)) from error
        path = cleaned
    # A very short cycle may repeat thousands of times. Keep one decoder and
    # finite stream looping rather than creating thousands of input handles.
    loops = max(0, math.ceil((music['start']+duration)/music['duration'])-1)
    args += ['-threads', '1', '-stream_loop', str(loops), '-i', str(path)]
    return 1, f'[{index}:a]', music['start']
