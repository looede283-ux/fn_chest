from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_START_COUNT = 0


@dataclass
class ReplayEntry:
    guid: str
    file: str
    chests: int
    added: int
    size: int
    match_time: str = ""
    duration_s: int = 0
    counted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    version: int = 1


class CountStore:
    VERSION = 1

    def __init__(self, path: Path, start_count: int = DEFAULT_START_COUNT) -> None:
        self.path = Path(path)
        self.start_count = start_count
        self.folder: Optional[str] = None
        self.entries: dict[str, ReplayEntry] = {}
        self._lock = threading.RLock()
        if self.path.exists():
            self._load()

    @property
    def total(self) -> int:
        with self._lock:
            return self.start_count + sum(e.added for e in self.entries.values())

    def get(self, guid: str) -> Optional[ReplayEntry]:
        with self._lock:
            return self.entries.get(guid)

    def history(self) -> list[ReplayEntry]:
        with self._lock:
            return sorted(self.entries.values(),
                          key=lambda e: (e.match_time, e.counted_at), reverse=True)

    def record(self, entry: ReplayEntry) -> None:
        with self._lock:
            self.entries[entry.guid] = entry
            self.save()

    def set_folder(self, folder: Optional[str]) -> None:
        with self._lock:
            self.folder = folder
            self.save()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"{self.path} is not valid JSON ({exc}); fix or delete it") from exc
        self.start_count = int(data.get("start_count", self.start_count))
        self.folder = data.get("folder")
        for raw in data.get("replays", []):
            known = {k: v for k, v in raw.items() if k in ReplayEntry.__dataclass_fields__}
            entry = ReplayEntry(**known)
            self.entries[entry.guid] = entry

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": self.VERSION,
                "start_count": self.start_count,
                "total": self.total,
                "folder": self.folder,
                "replays": [asdict(e) for e in self.history()],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
