"""Real FFmpeg against a local synthetic 128MB virtual MP4, not Google data."""
import io
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from model.DriveVideoFrames import extract_frame

FFMPEG = os.environ.get('BATCH_TEST_FFMPEG', '')


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), 'explicit test FFmpeg required')
class DriveFramesTests(unittest.TestCase):
    def test_tail_moov_large_video_is_not_downloaded_whole_and_frame_is_uncropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            movie = Path(temporary)/'source.mp4'
            result = subprocess.run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                'color=white:s=960x320:d=2,drawbox=x=0:y=0:w=80:h=320:color=red:t=fill,drawbox=x=880:y=0:w=80:h=320:color=blue:t=fill',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(movie)], capture_output=True, timeout=20,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            raw = movie.read_bytes(); offset, boxes = 0, []
            while offset < len(raw):
                length, kind = struct.unpack('>I4s', raw[offset:offset+8]); boxes.append((offset, length, kind)); offset += length
            moov = next(value for value in boxes if value[2] == b'moov'); self.assertEqual(moov[0]+moov[1], len(raw))
            padding = 128*1024**2
            first, last = raw[:moov[0]], raw[moov[0]:]
            prefix = first + struct.pack('>I4s', padding, b'free')
            size = len(raw)+padding; calls = []
            def fetch(start, end):
                calls.append((start,end))
                output = bytearray(end-start+1)
                for base, data in [(0, prefix), (len(first)+padding, last)]:
                    left, right = max(start,base), min(end+1,base+len(data))
                    if right > left: output[left-start:right-start] = data[left-base:right-base]
                return bytes(output)
            original_popen = subprocess.Popen
            errors = []
            class TraceProcess(original_popen):
                def communicate(self, *args, **kwargs):
                    result = super().communicate(*args, **kwargs)
                    if self.returncode: errors.append(result[1].decode(errors='replace'))
                    return result
            try:
                with patch('model.DriveVideoFrames.subprocess.Popen', TraceProcess):
                    frame, used = extract_frame(size, fetch, lambda: None, seconds=.5, ffmpeg=FFMPEG)
            except Exception as error:
                self.fail(f'{error}; fetched={calls}; decoder={errors}')
            self.assertLess(used, 8*1024**2); self.assertLess(used, size/10)
            self.assertTrue(any(start > size/2 for start, _ in calls), 'must seek to tail metadata')
            with Image.open(io.BytesIO(frame)) as image:
                self.assertEqual(image.size, (960,320))
                self.assertGreater(image.getpixel((5,150))[0], 200); self.assertGreater(image.getpixel((950,150))[2], 200)

    def test_decoder_failure_does_not_trigger_full_download(self):
        calls = []
        def fetch(start,end): calls.append((start,end)); return b'\0'*(end-start+1)
        with self.assertRaises((ValueError, TimeoutError)):
            extract_frame(128*1024**2, fetch, lambda: None, ffmpeg=FFMPEG, timeout=5)
        self.assertLessEqual(sum(end-start+1 for start,end in calls), 32*1024**2)
