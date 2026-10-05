"""Require a real changelog entry and extract it for the matching release tag."""

import argparse
from pathlib import Path
import re
import sys


VERSION = r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?"
HEADING = re.compile(rf"^## (?P<version>{VERSION})(?:[ \t]+[^\n]+)?$", re.MULTILINE)


def extract_release_notes(markdown, version):
    if not re.fullmatch(VERSION, version):
        raise ValueError("发布标签格式无效，应为 v1.2.3 或带预发布后缀的版本。")
    headings = list(HEADING.finditer(markdown))
    matches = [(index, heading) for index, heading in enumerate(headings)
               if heading.group("version") == version]
    if len(matches) != 1:
        raise ValueError(f"CHANGELOG.md 必须包含且仅包含一条 {version} 更新日志。")
    index, heading = matches[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
    body = markdown[heading.end():end].strip()
    if not body or not re.search(r"^[-*] \S", body, re.MULTILINE):
        raise ValueError(f"{version} 更新日志为空或没有具体改动条目。")
    if re.search(r"\b(?:TODO|TBD)\b|待补充|待填写", body, re.IGNORECASE):
        raise ValueError(f"{version} 更新日志仍有占位文字，请补充后发布。")
    return markdown[heading.start():end].strip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", type=Path,
                        default=Path(__file__).resolve().parents[1] / "CHANGELOG.md")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        notes = extract_release_notes(args.changelog.read_text(encoding="utf-8-sig"), args.version)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(notes, encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"更新日志检查失败：{error}", file=sys.stderr)
        return 1
    print(f"更新日志已验证：{args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
