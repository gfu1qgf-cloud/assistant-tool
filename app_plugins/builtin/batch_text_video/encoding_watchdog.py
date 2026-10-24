"""A repeated FFmpeg progress heartbeat is not evidence of encoding progress."""
import re


class EncodingStalled(RuntimeError):
    pass


class EncodingWatchdog:
    def __init__(self, idle_seconds=90):
        self.idle_seconds = idle_seconds
        self.last_advance = None
        self.values = {'frame': 0, 'out_time_us': 0, 'total_size': 0}
        self.pending = ''

    def observe(self, text, now):
        lines = (self.pending + text).split('\n')
        self.pending = lines.pop()
        advanced = False
        for line in lines:
            match = re.fullmatch(r'(frame|out_time_us|total_size)=(\d+)\r?', line)
            if match:
                key, value = match[1], int(match[2])
                if value > self.values[key]:
                    self.values[key] = value
                    advanced = True
        # A muxer header alone does not count as a first encoded frame. Slow
        # initialisation / first AI inference retains the separate full timeout.
        if advanced and (self.values['frame'] > 0 or self.values['out_time_us'] > 0):
            self.last_advance = now
        if self.last_advance is not None and now - self.last_advance >= self.idle_seconds:
            raise EncodingStalled(f"编码连续{self.idle_seconds}秒没有推进，停在"
                f"{self.values['out_time_us']/1e6:.3f}秒；本条未保存成品。")
        return self.values['out_time_us']


def serial_filters(args):
    """Keep all effects and inputs; only change the filter scheduling on retry."""
    result, index = [], 0
    while index < len(args):
        if args[index] in ('-filter_threads', '-filter_complex_threads'):
            index += 2
        else:
            result.append(args[index]); index += 1
    return result[:1] + ['-filter_threads', '1', '-filter_complex_threads', '1'] + result[1:]
