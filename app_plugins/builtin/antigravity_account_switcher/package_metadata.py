"""Bounded, read-only package.json inspection for packaged Electron clients.

ASAR layout: https://github.com/electron/asar#format. Never extract/run code.
"""
import json
from pathlib import Path
import struct
from model.AppDataStorage import data_file

MAX_HEADER = 16 * 1024 * 1024


def packaged_metadata(executable):
    archive = Path(executable).parent / 'resources/app.asar'
    try:
        with data_file(archive).open('rb') as stream:
            prefix = stream.read(16)
            if len(prefix) != 16:
                return None
            size, header_size, payload_size, json_size = struct.unpack('<4I', prefix)
            if (size != 4 or not 8 <= header_size <= MAX_HEADER or
                    payload_size + 4 != header_size or not 0 < json_size <= header_size - 8):
                return None
            header = json.loads(stream.read(json_size).decode('utf8'))
            entry = header['files']['package.json']
            if not isinstance(entry, dict) or entry.get('unpacked') or entry.get('link') or entry.get('files'):
                return None
            length = entry['size']
            offset = int(entry['offset'])
            if type(length) is not int or not 0 < length <= 65536 or offset < 0:
                return None
            stream.seek(8 + header_size + offset)
            package = json.loads(stream.read(length).decode('utf8'))
            if (not isinstance(package, dict) or package.get('name') != 'antigravity' or package.get('productName') != 'Antigravity' or
                    package.get('main') != 'dist/main.js' or
                    not isinstance(package.get('author'), dict) or package['author'].get('name') != 'Google'):
                return None
            version = package.get('version', '')
            if not isinstance(version, str) or not version.startswith('2.'):
                return None
            return {'name': 'Antigravity', 'version': version, 'auth_kind': 'windows'}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, struct.error):
        return None
