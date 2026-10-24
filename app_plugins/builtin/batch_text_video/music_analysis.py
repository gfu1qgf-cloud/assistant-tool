"""Small silence edit indexes; the original music remains the audio source."""
from dataclasses import dataclass
import math
from pathlib import Path


@dataclass(frozen=True)
class MusicAnalysis:
    path: Path
    duration: float
    key: str
    ranges: tuple = ()
    source_duration: float = 0

    def __iter__(self):
        return iter((self.path, self.duration, self.key))

    def __getitem__(self, index):
        return (self.path, self.duration, self.key)[index]


def from_index(path, key, value):
    if value['version'] != 1:
        raise ValueError('Unknown music index')
    source_duration = float(value['source_duration'])
    if not math.isfinite(source_duration) or not .5 <= source_duration <= 86400:
        raise ValueError('Invalid music duration')
    raw = value['ranges']
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100000:
        raise ValueError('Invalid music ranges')
    ranges, previous = [], 0.
    for pair in raw:
        start, end = map(float, pair)
        if not (math.isfinite(start) and math.isfinite(end) and previous <= start < end <= source_duration):
            raise ValueError('Invalid music range')
        ranges.append((start, end))
        previous = end
    duration = sum(end-start for start, end in ranges)
    if duration < .5:
        raise ValueError('Invalid audible duration')
    return MusicAnalysis(Path(path), duration, key, tuple(ranges), source_duration)


def timeline_ranges(ranges, cursor, duration, *, limit=64):
    """Map the cleaned playhead to original audio; cap native input count."""
    total = sum(end-start for start, end in ranges)
    if not math.isfinite(total) or total < .5 or not math.isfinite(duration) or duration <= 0:
        raise ValueError('音乐时间线无效。')
    offset, left, pieces = float(cursor) % total, duration, []
    while left > 1e-8:
        for start, end in ranges:
            size = end-start
            if offset >= size-1e-9:
                offset = max(0., offset-size)
                continue
            take = min(size-offset, left)
            if take > 1e-8:
                pieces.append((start+offset, start+offset+take))
                if len(pieces) > limit:
                    return None
                left -= take
            offset = 0.
            if left <= 1e-8:
                break
    return pieces
