"""Thread-safe UTF-8 system log storage."""
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


class SystemWriter:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, filename, fields):
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), **fields}
        with self._lock:
            with (self.directory / filename).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def write_json(self, filename, fields):
        with self._lock:
            with (self.directory / filename).open("w", encoding="utf-8") as handle:
                json.dump(fields, handle, ensure_ascii=False, indent=2, default=str)
