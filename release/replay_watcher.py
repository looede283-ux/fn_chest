from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import os
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from count_store import CountStore, ReplayEntry
from fortnite_replay import PARSER_VERSION, ChestReplayParser, ReplayError, peek_replay_state

DEFAULT_FOLDER = Path(os.environ.get("LOCALAPPDATA", "~")) / "FortniteGame" / "Saved" / "Demos"
MAX_AGE_SECONDS = 4 * 24 * 60 * 60
STABLE_SECONDS = 90
POLL_SECONDS = 5
# Replays are parsed in worker processes so the UI never competes with the parser for the GIL,
# and several replays can be read at once on multi-core machines.
WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))

_progress: Optional[multiprocessing.Queue] = None


def _init_worker(progress: multiprocessing.Queue) -> None:
    global _progress
    _progress = progress
    parent = multiprocessing.parent_process()
    if parent is not None:
        # Exit with the app even if it is killed without a chance to shut the pool down.
        threading.Thread(target=lambda: (multiprocessing.connection.wait([parent.sentinel]), os._exit(0)),
                         daemon=True).start()


def parse_entry(path: Path, guid: str, size: int) -> ReplayEntry | str:
    """Parse one replay in a worker; returns the entry or a reason it was skipped."""
    shown = -1

    def progress(done: int, total: int) -> None:
        nonlocal shown
        percent = 100 * done // max(total, 1)
        if percent != shown and _progress is not None:
            shown = percent
            _progress.put((path.name, percent))

    parser = ChestReplayParser()
    try:
        result = parser.parse(path, progress=progress)
    except (ReplayError, OSError, EOFError) as exc:
        return f"Could not read {path.name}: {exc}"
    covered = parser.frame_time * 1000 >= result.duration_ms * 0.9
    if result.errors and not covered:
        return f"Skipped {path.name}: replay ended early"
    if not result.recorder_pawns:
        return f"Skipped {path.name}: could not tell which player recorded it"
    started = result.started or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    opened = [[round(e.time, 1), e.kind, None if e.x is None else round(e.x), None if e.y is None else round(e.y)]
              for e in result.opened]
    return ReplayEntry(guid=guid, file=path.name, chests=result.count, added=result.count,
                       size=size, match_time=started.isoformat(timespec="seconds"),
                       duration_s=result.duration_ms // 1000, version=PARSER_VERSION,
                       opened=opened, squad=len(result.squad_opened), squad_size=result.squad_size)


class ReplayWatcher:
    def __init__(self, store: CountStore, folder: Path,
                 on_status: Callable[[str], None] = lambda text: None,
                 on_entry: Callable[[ReplayEntry, bool], None] = lambda entry, new: None,
                 on_scan: Callable[[int], None] = lambda finished: None) -> None:
        self.store = store
        self.folder = Path(folder)
        self._on_status = on_status
        self._on_entry = on_entry
        self._on_scan = on_scan
        # Scan counters let a caller wait for "the next complete scan" after asking for a rescan.
        self.scans_started = 0
        self.scans_finished = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ProcessPoolExecutor] = None
        self._progress: Optional[multiprocessing.Queue] = None
        self._snapshot: dict[Path, tuple[int, int]] = {}
        self._state_cache: dict[Path, tuple[int, int, tuple[str, bool]]] = {}
        self._failed: dict[Path, int] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="replay-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            # Don't keep the app alive while a worker finishes a replay nobody will see.
            for process in list((getattr(self._pool, "_processes", None) or {}).values()):
                process.terminate()

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
            self.scans_started += 1
            try:
                self.scan_once()
            except Exception as exc:
                self._on_status(f"Scan error: {exc}")
            self.scans_finished += 1
            self._on_scan(self.scans_finished)
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def scan_once(self) -> None:
        if not self.folder.exists():
            self._on_status(f"Folder not found: {self.folder}")
            return
        jobs: list[tuple[Path, str, int, bool]] = []
        busy = 0
        for path in self.replays():
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
                state = None
            if state is None:
                if age < STABLE_SECONDS:
                    busy += 1
                continue
            guid, live = state
            if live:
                busy += 1
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            if age < STABLE_SECONDS and self._snapshot.get(path) != signature:
                self._snapshot[path] = signature
                continue
            self._snapshot[path] = signature
            if self._failed.get(path) == stat.st_size:
                continue
            known = self.store.get(guid)
            if known is not None and stat.st_size == known.size and known.version >= PARSER_VERSION:
                continue
            jobs.append((path, guid, stat.st_size, known is None))
        if jobs:
            self._process(jobs)
        if not self._stop.is_set():
            suffix = f" ({busy} still recording)" if busy else ""
            self._on_status(f"Watching for new replays{suffix}")

    def _process(self, jobs: list[tuple[Path, str, int, bool]]) -> None:
        if self._pool is None:
            context = multiprocessing.get_context("spawn")
            self._progress = context.Queue()
            self._pool = ProcessPoolExecutor(WORKERS, context, _init_worker, (self._progress,))
        pending: dict[Future, tuple[Path, int, bool]] = {
            self._pool.submit(parse_entry, path, guid, size): (path, size, new) for path, guid, size, new in jobs}
        percents = {path.name: 0 for path, *_ in jobs}
        while pending and not self._stop.is_set():
            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            try:
                while True:
                    name, percent = self._progress.get_nowait()
                    percents[name] = percent
            except queue.Empty:
                pass
            for future in done:
                path, size, new = pending.pop(future)
                percents.pop(path.name, None)
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = f"Could not read {path.name}: {exc}"
                if isinstance(outcome, str):
                    self._failed[path] = size
                    self._on_status(outcome)
                    continue
                self.store.record(outcome)
                self._on_entry(outcome, new)
            if percents:
                share = sum(percents.values()) // len(percents)
                label = next(iter(percents)) if len(percents) == 1 else f"{len(percents)} replays"
                self._on_status(f"Reading {label} ... {share}%")
