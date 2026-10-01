"""Small persistent reminder ledger; never marks a snoozed reminder as done."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading


class ReminderState:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = {"events": {}, "notices": {}}
        if self.path.exists():
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict) or not isinstance(value.get("events"), dict) or not isinstance(value.get("notices", {}), dict):
                raise ValueError("提醒记录格式无效；保留原文件，不自动覆盖。")
            self.data = value

    def snapshot(self):
        with self._lock:
            return deepcopy(self.data)

    def _commit(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.data = value
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record(self, event, now, confirmed=False, minutes=15):
        with self._lock:
            value = self.snapshot()
            record = {"date": event.day.isoformat(), "types": list(event.types),
                      "updated_at": now.isoformat(), "confirmed": bool(confirmed),
                      "snooze_until": 0 if confirmed else now.timestamp() + minutes * 60}
            value["events"][event.key] = record
            # Keep 90 days of confirmations. Acknowledgement writes are infrequent.
            cutoff = now.timestamp() - 90 * 86400
            value["events"] = {k: v for k, v in value["events"].items()
                               if k == event.key or datetime.fromisoformat(v["updated_at"]).timestamp() >= cutoff}
            self._commit(value)

    def notice(self, key):
        with self._lock:
            if key in self.data.setdefault("notices", {}):
                return False
            value = self.snapshot()
            value["notices"][key] = True
            if len(value["notices"]) > 400:
                value["notices"] = dict(list(value["notices"].items())[-300:])
            self._commit(value)
            return True
