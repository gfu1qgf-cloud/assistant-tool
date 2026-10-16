"""Isolated FFmpeg decoding over a bounded, authenticated byte-range bridge.

No Google token/URL in process arguments. No original video written to disk.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

from app_paths import APP_ROOT

MAX_READ = 32 * 1024**2
BLOCK = 1024**2


class RangeReader:
    def __init__(self, size, fetch, check, *, budget=MAX_READ):
        self.size, self.fetch, self.check = int(size), fetch, check
        self.budget, self.used, self.blocks = int(budget), 0, {}
        self.error = None
        self.lock = threading.RLock()

    def read(self, start, end):
        with self.lock: return self._read(start, end)

    def _read(self, start, end):
        self.check()
        end = min(end, self.size - 1)
        if start < 0 or end < start: raise ValueError('视频字节范围无效。')
        output = []
        for offset in range(start // BLOCK * BLOCK, end + 1, BLOCK):
            self.check()
            length = min(BLOCK, self.size - offset)
            if offset not in self.blocks:
                if self.used + length > self.budget:
                    raise ValueError('视频缩略图取帧达到32MB流量上限，未下载完整视频。')
                value = self.fetch(offset, offset + length - 1)
                if len(value) != length: raise ValueError('网盘分段读取不完整。')
                self.used += length; self.blocks[offset] = value
            value = self.blocks[offset]
            output.append(value[max(0, start-offset):min(length, end-offset+1)])
        return b''.join(output)


def extract_frame(size, fetch, check, *, seconds=0, ffmpeg=None, timeout=40):
    binary = Path(ffmpeg or APP_ROOT / 'runtime' / 'ffmpeg' / 'ffmpeg.exe')
    if not binary.is_file(): raise ValueError('缺少主程序FFmpeg，无法从视频生成完整缩略图。')
    deadline = time.monotonic() + timeout
    stopped = threading.Event()
    def checked():
        check()
        if stopped.is_set() or time.monotonic() > deadline: raise TimeoutError('视频取帧超时，未改为下载完整视频。')
    reader = RangeReader(size, fetch, checked)
    key = '/' + secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        def log_message(self, *_): pass
        def setup(self):
            super().setup(); self.connection.settimeout(3)
        def do_HEAD(self): self.serve(head=True)
        def do_GET(self): self.serve(head=False)
        def serve(self, head=False):
            if self.path != key or self.client_address[0] != '127.0.0.1':
                self.send_error(404); return
            try:
                checked()
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range', ''))
                start = int(match[1]) if match else 0
                end = min(size-1, int(match[2]) if match and match[2] else size-1)
                if start >= size or end < start:
                    self.send_error(416); return
                self.send_response(206 if match else 200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(end-start+1))
                if match: self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                if not head:
                    for offset in range(start, end+1, 65536):
                        self.wfile.write(reader.read(offset, min(end, offset+65535)))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
            except Exception as error:
                reader.error = error
                self.close_connection = True
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    args = [str(binary), '-v', 'error', '-nostdin', '-rw_timeout', '10000000', '-seekable', '1',
            '-request_size', str(BLOCK), '-initial_request_size', str(BLOCK), '-short_seek_size', str(BLOCK),
            '-protocol_whitelist', 'http,tcp', '-format_whitelist',
            'mov,matroska,webm,avi,mpegts,mpeg,mpegvideo,flv,ogg,asf,rm,ivf,h264,hevc,av1,nut',
            '-ss', str(max(0, seconds)), '-i', f'http://127.0.0.1:{server.server_port}{key}',
            '-map', '0:v:0', '-frames:v', '1', '-an', '-vf',
            'scale=960:960:force_original_aspect_ratio=decrease:reset_sar=1',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '3', 'pipe:1']
    process = None
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        while True:
            checked()
            try:
                output, _ = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired:
                if reader.error: raise reader.error
        if reader.error and not output: raise reader.error
        if process.returncode or not output.startswith(b'\xff\xd8'):
            raise ValueError('无法通过局部数据解码此视频缩略图；不会自动下载完整视频。')
        return output, reader.used
    finally:
        stopped.set()
        if process is not None:
            if process.poll() is None: process.kill()
            process.communicate(timeout=5)
        server.shutdown(); server.server_close(); thread.join(timeout=12)
