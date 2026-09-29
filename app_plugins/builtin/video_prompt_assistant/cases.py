"""Local, versionable-in-format (but private on disk) successful prompt notes."""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app_paths import APP_ROOT


DEFAULT_CASE_PATH = APP_ROOT / "VideoPromptAssistant" / "cases.json"


class PromptCaseStore:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_CASE_PATH)

    def load(self):
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise ValueError("成功案例文件格式不正确")
        return [item for item in data["cases"] if isinstance(item, dict)]

    def save(self, cases):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "cases": cases}, ensure_ascii=False, indent=2)
        fd, temporary = tempfile.mkstemp(prefix=".cases-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def upsert(self, title, prompt, failed_prompt="", note="", case_id=None):
        title, prompt = title.strip(), prompt.strip()
        if not title or not prompt:
            raise ValueError("标题和成功提示词不能为空")
        cases = self.load()
        old = next((item for item in cases if item.get("id") == case_id), None)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = {
            "id": case_id if old else uuid.uuid4().hex,
            "title": title,
            "prompt": prompt,
            "failed_prompt": failed_prompt.strip(),
            "note": note.strip(),
            "tags": list(old.get("tags", [])) if old else [],
            "created_at": old.get("created_at", now) if old else now,
            "updated_at": now,
        }
        if old:
            cases[cases.index(old)] = entry
        else:
            cases.insert(0, entry)
        self.save(cases)
        return entry

    def delete(self, case_id):
        cases = self.load()
        remaining = [item for item in cases if item.get("id") != case_id]
        if len(remaining) == len(cases):
            return False
        self.save(remaining)
        return True
