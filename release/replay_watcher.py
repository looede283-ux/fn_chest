from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from count_store import CountStore, ReplayEntry
from fortnite_replay import PARSER_VERSION, ChestReplayParser, ReplayError, peek_replay_state

DEFAULT_FOLDER = Path(os.environ.get("LOCALAPPDATA", "~")) / "FortniteGame" / "Saved" / "Demos"
MAX_AGE_SECONDS = 4 * 24 * 60 * 60
STABLE_SECONDS = 90
POLL_SECONDS = 5


class ReplayWatcher:
    def __init__(self, store: CountStore, folder: Path,
                 on_status: Callable[[str], None] = lambda text: None,
                 on_entry: Callable[[ReplayEntry], None] = lambda entry: None) -> None:
        self.store = store
        self.folder = Path(folder)
        self._on_status = on_status
        self._on_entry = on_entry
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snapshot: dict[Path, tuple[int, int]] = {}
        self._state_cache: dict[Path, tuple[int, int, tuple[str, bool]]] = {}
        self._failed: dict[Path, int] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="replay-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def rescan(self) -> None:
        self._wake.set()

    def set_folder(self, folder: Path) -> None:
        self.folder = Path(folder)
        self._snapshot.clear()
        self._state_cache.clear()
        self._failed.clear()
        self.rescan()

    def replays(self) -> list[Path]:
        try:
            return sorted(self.folder.glob("*.replay"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return []

    def _state(self, path: Path, mtime_ns: int, size: int) -> Optional[tuple[str, bool]]:
        cached = self._state_cache.get(path)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            return cached[2]
        state = peek_replay_state(path)
        if state is not None:
            self._state_cache[path] = (mtime_ns, size, state)
        return state

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:
                self._on_status(f"Scan error: {exc}")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def scan_once(self) -> None:
        if not self.folder.exists():
            self._on_status(f"Folder not found: {self.folder}")
            return
        files = self.replays()
        busy = 0
        for path in files:
            if self._stop.is_set():
                return
            try:
                stat = path.stat()
            except OSError:
                continue
            age = time.time() - stat.st_mtime
            if age > MAX_AGE_SECONDS:
                continue
            if stat.st_size == 0:
                busy += 1
                continue
            try:
                state = self._state(path, stat.st_mtime_ns, stat.st_size)
            except (OSError, ReplayError):
                if age < STABLE_SECONDS:
                    busy += 1
                continue
            if state is None:
                if age < STABLE_SECONDS:
                    busy += 1
                continue
            guid, live = state
            if live and age < STABLE_SECONDS:
                busy += 1
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            if not live and age < STABLE_SECONDS and self._snapshot.get(path) != signature:
                self._snapshot[path] = signature
                continue
            self._snapshot[path] = signature
            if self._failed.get(path) == stat.st_size:
                continue
            known = self.store.get(guid)
            if known is not None and stat.st_size <= known.size and known.version >= PARSER_VERSION:
                continue
            self._process(path, guid, stat.st_size, known)
        suffix = f" ({busy} still recording)" if busy else ""
        self._on_status(f"Watching {self.folder}{suffix}")

    def _process(self, path: Path, guid: str, size: int, known: Optional[ReplayEntry]) -> None:
        self._on_status(f"Reading {path.name} ...")
        parser = ChestReplayParser()
        try:
            result = parser.parse(path, progress=lambda done, total: self._on_status(
                f"Reading {path.name} ... {100 * done // max(total, 1)}%"))
        except (ReplayError, OSError, EOFError) as exc:
            self._failed[path] = size
            self._on_status(f"Could not read {path.name}: {exc}")
            return
        if not result.recorder_pawns:
            self._failed[path] = size
            self._on_status(f"Skipped {path.name}: could not tell which player recorded it")
            return
        started = result.started or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        entry = ReplayEntry(guid=guid, file=path.name, chests=result.count, added=result.count,
                            size=size, match_time=started.isoformat(timespec="seconds"),
                            duration_s=result.duration_ms // 1000, version=PARSER_VERSION)
        self.store.record(entry)
        self._on_entry(entry)
        gained = entry.added - (known.added if known else 0)
        self._on_status(f"{path.name}: {gained:+d} chests")
