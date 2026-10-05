"""Record the release identity and resolved dependencies without local state."""

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import re


def make_manifest(version, commit):
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("Invalid release version")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a full Git commit SHA")
    import cv2
    import numpy

    # Assert the tested native stack; stop instead of quietly packaging old DLLs.
    if cv2.__version__ != "4.14.0" or numpy.__version__ != "2.2.6":
        raise ValueError("Release native stack differs from the tested OpenCV/NumPy versions")
    build = cv2.getBuildInformation()
    native = {"opencv": cv2.__version__, "numpy": numpy.__version__}
    for label in ("PNG", "avcodec", "avformat", "avutil", "swscale"):
        match = re.search(rf"^\s*{label}:\s*(.+)$", build, re.MULTILINE)
        if match:
            native[label.lower()] = match.group(1).strip()
    packages = {
        dist.metadata["Name"]: dist.version
        for dist in importlib.metadata.distributions()
        if dist.metadata.get("Name")
    }
    return {
        "version": version,
        "source_commit": commit,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "native_media": native,
        "installed_dependencies": dict(sorted(packages.items(), key=lambda row: row[0].casefold())),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = make_manifest(args.version, args.commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Release identity recorded: {args.version} / {args.commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
