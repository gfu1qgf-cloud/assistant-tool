"""Content-based duplicate checks for files entering the material library."""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from pathlib import Path


_MD5_RE = re.compile(r"[0-9a-fA-F]{32}\Z")


def _digest(path, algorithm):
    checksum = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


class MaterialDuplicateIndex:
    """Scan file sizes once; hash only candidates of the same size.

    Hidden staging directories and partial downloads are deliberately ignored.
    A newly accepted file can be registered so duplicates within one batch are
    caught without rescanning the whole library.
    """

    def __init__(self, roots):
        if isinstance(roots, (str, os.PathLike)):
            roots = [roots]
        self._by_size = defaultdict(list)
        self._digests = {}
        self._known_sizes = {}
        for root in roots:
            root = Path(root)
            if not root.is_dir():
                continue
            for directory, children, files in os.walk(root):
                children[:] = [
                    name for name in children
                    if not (name.startswith(".") and name.endswith(".tmp"))
                ]
                for name in files:
                    if name.endswith((".part", ".resume.json", ".complete.json")) or name.startswith("."):
                        continue
                    self.register(Path(directory) / name)

    def register(self, path):
        path = Path(path)
        if path.is_symlink():
            return
        try:
            size = path.stat().st_size
        except (FileNotFoundError, OSError):
            return
        key = os.path.normcase(str(path.absolute()))
        if self._known_sizes.get(key) != size:
            self._known_sizes[key] = size
            self._by_size[size].append(path)

    def register_tree(self, path):
        path = Path(path)
        if path.is_file():
            self.register(path)
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    self.register(child)

    def _matching_size(self, size, exclude=None):
        excluded = os.path.normcase(str(Path(exclude).absolute())) if exclude else None
        for candidate in self._by_size.get(size, ()):
            if os.path.normcase(str(candidate.absolute())) == excluded:
                continue
            try:
                if candidate.is_file() and candidate.stat().st_size == size:
                    yield candidate
            except OSError:
                continue

    def _hash(self, path, algorithm):
        path = Path(path)
        stat = path.stat()
        key = (os.path.normcase(str(path.absolute())), stat.st_size, stat.st_mtime_ns, algorithm)
        if key not in self._digests:
            self._digests[key] = _digest(path, algorithm)
        return self._digests[key]

    def find_remote_md5(self, size, md5_checksum):
        """Use Drive's byte checksum to avoid transferring a known file."""
        checksum = str(md5_checksum or "").strip()
        if not _MD5_RE.fullmatch(checksum):
            return None
        try:
            size = int(size)
        except (TypeError, ValueError):
            return None
        for candidate in self._matching_size(size):
            try:
                if self._hash(candidate, "md5") == checksum.casefold():
                    return candidate
            except OSError:
                continue
        return None

    def find_same_content(self, path):
        """Confirm a downloaded/staged file by size and SHA-256."""
        path = Path(path)
        size = path.stat().st_size
        candidates = list(self._matching_size(size, exclude=path))
        if not candidates:
            return None
        digest = self._hash(path, "sha256")
        for candidate in candidates:
            try:
                if self._hash(candidate, "sha256") == digest:
                    return candidate
            except OSError:
                continue
        return None
