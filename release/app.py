from __future__ import annotations

import argparse
import os
import queue
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

import ui
from count_store import DEFAULT_START_COUNT, CountStore, ReplayEntry
from replay_watcher import DEFAULT_FOLDER, ReplayWatcher

APP_DIR = Path(__file__).resolve().parent
DEFAULT_COUNT_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ChestCounter" / "count.json"
ICON_FILE = APP_DIR / "icon.ico"
ANIMATION_STEPS = 24
ANIMATION_MS = 14


class ChestCounterApp:
    def __init__(self, root: tk.Tk, store: CountStore, folder: Path) -> None:
        self.root = root
        self.store = store
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.watcher = ReplayWatcher(store, folder,
                                     on_status=lambda text: self.events.put(("status", text)),
                                     on_entry=lambda entry: self.events.put(("entry", entry)))
        self.scale = ui.Scale(root)
        self.fonts = ui.Fonts(root, self.scale)
        self._shown_total = store.total
        self._animation: str | None = None
        self._build()
        self._refresh()
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(200, self._start_watching)
        root.after(300, self._drain_events)

    def _build(self) -> None:
        s, fonts = self.scale, self.fonts
        root = self.root
        root.title("Chest Counter")
        ui.set_window_icon(root, ICON_FILE)
        root.configure(bg=ui.BACKGROUND)
        root.geometry(f"{s(580)}x{s(760)}")
        root.minsize(s(480), s(560))

        page = tk.Frame(root, bg=ui.BACKGROUND)
        page.pack(fill="both", expand=True, padx=s(24), pady=s(22))

        header = tk.Frame(page, bg=ui.BACKGROUND)
        header.pack(fill="x", pady=(0, s(16)))
        tk.Label(header, text="Chest Counter", bg=ui.BACKGROUND, fg=ui.FOREGROUND,
                 font=fonts.heading).pack(anchor="w")
        tk.Label(header, text="Chests opened, counted from your Fortnite replays.", bg=ui.BACKGROUND,
                 fg=ui.MUTED, font=fonts.body).pack(anchor="w", pady=(s(2), 0))

        total_card = ui.Card(page, s, auto_height=True)
        total_card.pack(fill="x")
        body = total_card.body
        tk.Label(body, text="Total chests opened", bg=ui.BACKGROUND, fg=ui.MUTED,
                 font=fonts.body).pack(anchor="w")
        self.total_label = tk.Label(body, text="", bg=ui.BACKGROUND, fg=ui.FOREGROUND, font=fonts.display)
        self.total_label.pack(anchor="w", pady=(s(2), s(16)))
        tk.Frame(body, bg=ui.BORDER, height=1).pack(fill="x")

        stats = tk.Frame(body, bg=ui.BACKGROUND)
        stats.pack(fill="x", pady=(s(16), 0))
        self.stat_values: dict[str, tk.Label] = {}
        for column, name in enumerate(("Replays", "Average", "Best match")):
            stats.columnconfigure(column, weight=1, uniform="stat")
            cell = tk.Frame(stats, bg=ui.BACKGROUND)
            cell.grid(row=0, column=column, sticky="w")
            tk.Label(cell, text=name, bg=ui.BACKGROUND, fg=ui.MUTED, font=fonts.small).pack(anchor="w")
            value = tk.Label(cell, text="-", bg=ui.BACKGROUND, fg=ui.FOREGROUND, font=fonts.title)
            value.pack(anchor="w")
            self.stat_values[name] = value

        history_card = ui.Card(page, s)
        history_card.pack(fill="both", expand=True, pady=(s(16), 0))
        body = history_card.body
        top = tk.Frame(body, bg=ui.BACKGROUND)
        top.pack(fill="x", pady=(0, s(12)))
        titles = tk.Frame(top, bg=ui.BACKGROUND)
        titles.pack(side="left")
        tk.Label(titles, text="History", bg=ui.BACKGROUND, fg=ui.FOREGROUND,
                 font=fonts.title).pack(anchor="w")
        tk.Label(titles, text="Chests you opened in each match.", bg=ui.BACKGROUND, fg=ui.MUTED,
                 font=fonts.body).pack(anchor="w")
        actions = tk.Frame(top, bg=ui.BACKGROUND)
        actions.pack(side="right")
        ui.Button(actions, "Folder", self._choose_folder, s, fonts).pack(side="left", padx=(0, s(8)))
        ui.Button(actions, "Rescan", self.watcher.rescan, s, fonts, variant="primary").pack(side="left")
        self.history = ui.HistoryList(body, s, fonts, "No replays counted yet.")
        self.history.pack(fill="both", expand=True)

        footer = tk.Frame(page, bg=ui.BACKGROUND)
        footer.pack(fill="x", pady=(s(14), 0))
        self.dot = ui.StatusDot(footer, s)
        self.dot.pack(side="left", padx=(s(2), s(8)))
        self.status = tk.Label(footer, text="Starting...", bg=ui.BACKGROUND, fg=ui.MUTED,
                               font=fonts.small, anchor="w", justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        footer.bind("<Configure>", lambda e: self.status.configure(wraplength=max(e.width - s(30), 100)))

        ui.apply_dark_titlebar(root)

    def _refresh(self) -> None:
        entries = self.store.history()
        self.history.set_rows([
            ui.HistoryRow(_format_match(entry), entry.file, f"+{entry.added}") for entry in entries])
        counts = [entry.chests for entry in entries]
        self.stat_values["Replays"].configure(text=str(len(counts)) if counts else "-")
        self.stat_values["Average"].configure(text=f"{sum(counts) / len(counts):.1f}" if counts else "-")
        self.stat_values["Best match"].configure(text=str(max(counts)) if counts else "-")
        self._animate_total(self.store.total)

    def _animate_total(self, target: int) -> None:
        if self._animation:
            self.root.after_cancel(self._animation)
            self._animation = None
        start = self._shown_total
        if start == target:
            self.total_label.configure(text=f"{target:,}")
            return

        def step(frame: int) -> None:
            progress = frame / ANIMATION_STEPS
            eased = 1 - (1 - progress) ** 3
            self._shown_total = round(start + (target - start) * eased)
            self.total_label.configure(text=f"{self._shown_total:,}")
            if frame < ANIMATION_STEPS:
                self._animation = self.root.after(ANIMATION_MS, step, frame + 1)
            else:
                self._animation = None

        step(0)

    def _set_status(self, text: str) -> None:
        lowered = text.lower()
        if lowered.startswith("reading"):
            state = "busy"
        elif any(word in lowered for word in ("could not", "skipped", "error", "not found")):
            state = "error"
        else:
            state = "ok"
        self.dot.set(state)
        self.status.configure(text=text)

    def _drain_events(self) -> None:
        changed = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self._set_status(str(payload))
                else:
                    changed = True
        except queue.Empty:
            pass
        if changed:
            self._refresh()
        self.root.after(300, self._drain_events)

    def _start_watching(self) -> None:
        self.watcher.start()

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=str(self.watcher.folder), parent=self.root,
                                         title="Fortnite replay folder")
        if folder:
            # Only remember a custom folder; the default is resolved per user at launch.
            chosen = Path(folder)
            self.store.set_folder(None if chosen == DEFAULT_FOLDER else str(chosen))
            self.watcher.set_folder(chosen)

    def _close(self) -> None:
        self.watcher.stop()
        self.root.destroy()


def _format_match(entry: ReplayEntry) -> str:
    if not entry.match_time:
        return entry.file
    when = datetime.fromisoformat(entry.match_time).astimezone()
    text = f"{when:%b} {when.day}  ·  {when:%H:%M}"
    if entry.duration_s:
        text += f"  ·  {entry.duration_s // 60} min"
    return text


def _parse_one(path: str) -> int:
    from fortnite_replay import ReplayError, count_chests
    try:
        result = count_chests(path)
    except (ReplayError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{Path(path).name}: {result.count} chests opened by you "
          f"({len(result.all_opened)} by everyone)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fortnite chest counter")
    parser.add_argument("--parse", metavar="FILE", help="print the chest count of one replay and exit")
    parser.add_argument("--folder", type=Path, help=f"replay folder (default: {DEFAULT_FOLDER})")
    parser.add_argument("--count-file", type=Path, default=DEFAULT_COUNT_FILE,
                        help="where the running total is stored")
    parser.add_argument("--start", type=int, default=DEFAULT_START_COUNT,
                        help="starting total when count.json is first created")
    args = parser.parse_args(argv)

    if args.parse:
        return _parse_one(args.parse)

    store = CountStore(args.count_file, args.start)
    folder = args.folder or (Path(store.folder) if store.folder and Path(store.folder).is_dir()
                             else DEFAULT_FOLDER)
    ui.enable_dpi_awareness()
    root = tk.Tk()
    ChestCounterApp(root, store, folder)
    root.mainloop()
    return 0

if __name__ == "__main__":
    sys.exit(main())
