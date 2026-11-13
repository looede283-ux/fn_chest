from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog

import mapdata
import stats
import ui
import updater
from count_store import DEFAULT_START_COUNT, CountStore, ReplayEntry
from replay_watcher import DEFAULT_FOLDER, ReplayWatcher

APP_DIR = Path(__file__).resolve().parent
DEFAULT_COUNT_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ChestCounter" / "count.json"
ICON_FILE = APP_DIR / "icon.ico"
CHECK_SECONDS = 1.0
PAGES = (("overview", ui.ICON["home"], "Overview"),
         ("matches", ui.ICON["history"], "Matches"),
         ("spots", ui.ICON["pin"], "Drop spots"),
         ("token", ui.ICON["star"], "Pirate Token"))
TOKEN_TARGETS = (0.25, 0.5, 0.75, 0.9)
RECENT_ROWS = 5
RESCAN_MIN_SECONDS = 0.8  # a rescan that finds nothing still shows "Checking…" for this long
PAD = 24  # page gutter; everything else sits on the 4 px grid

MATCH_COLUMNS = [ui.Column("Time", 64), ui.Column("Length", 64, "e"), ui.Column("Pace", 72, "e"),
                 ui.Column("Top location", 0, hide_below=520), ui.Column("Squad", 64, "e", hide_below=420),
                 ui.Column("Chests", 96, "e")]
# The overview's recent list has no day headers, so its first column carries the day too.
RECENT_COLUMNS = [ui.Column("Played", 132), *MATCH_COLUMNS[1:]]
FORECAST_COLUMNS = [ui.Column("Chance", 0), ui.Column("Chests needed", 120, "e"), ui.Column("Still to go", 110, "e"),
                    ui.Column("Matches", 90, "e", hide_below=480), ui.Column("Play time", 96, "e")]


def _percent(value: float) -> str:
    if value <= 0:
        return "0%"
    return "<0.01%" if value < 0.0001 else f"{value * 100:.2f}%"


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 'es' if word.endswith('ch') else 's'}"


def _day_name(day: date) -> str:
    today = date.today()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%A, %b} {day.day}"


class ChestCounterApp:
    def __init__(self, root: tk.Tk, store: CountStore, folder: Path) -> None:
        self.root = root
        self.store = store
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.watcher = ReplayWatcher(store, folder,
                                     on_status=lambda text: self.events.put(("status", text)),
                                     on_entry=lambda entry, new: self.events.put(("entry", (entry, new))),
                                     on_scan=lambda finished: self.events.put(("scan", finished)))
        # A rescan the user asked for: finished once scan number `target` completes.
        self._rescan_target = 0
        self._rescan_found = 0
        self._rescan_started = 0.0
        self._last_status = ""
        self._status_state = "ok"
        self.map = mapdata.MapData()
        self.map_loaded = False
        self.scale = ui.Scale(root)
        self.fonts = ui.Fonts(root, self.scale)
        if store.milestone is None:
            store.set_milestone(stats.milestone_at(store.total))
        self.page: str | None = None
        self._build()
        self._refresh()
        self.show("overview")
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(200, self._start_watching)
        root.after(300, self._drain_events)
        threading.Thread(target=lambda: self.events.put(("map", mapdata.load())), daemon=True).start()

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        s, f, root = self.scale, self.fonts, self.root
        root.title("Chest Counter")
        ui.set_window_icon(root, ICON_FILE)
        root.configure(bg=ui.BACKGROUND)
        root.geometry(f"{s(900)}x{s(600)}")
        root.minsize(s(860), s(560))

        side = tk.Frame(root, bg=ui.SIDEBAR, width=s(208))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=ui.SIDEBAR)
        brand.pack(fill="x", padx=s(18), pady=(s(16), s(14)))
        ui.label(brand, ui.ICON["chest"], f.icon, ui.TEXT_2).pack(side="left")
        ui.label(brand, "Chest Counter", f.body_strong).pack(side="left", padx=(s(10), 0))
        self.nav = ui.Nav(side, list(PAGES), s, f, self.show)
        self.nav.pack(fill="x", padx=s(8))

        footer = tk.Frame(side, bg=ui.SIDEBAR)
        footer.pack(side="bottom", fill="x")
        ui.divider(footer).pack(fill="x")
        status = tk.Frame(footer, bg=ui.SIDEBAR)
        status.pack(fill="x", padx=s(18), pady=(s(12), s(10)))
        line = tk.Frame(status, bg=ui.SIDEBAR)
        line.pack(fill="x")
        self.dot = ui.StatusDot(line, s)
        self.dot.pack(side="left")
        self.status_title = ui.label(line, "Starting…", f.meta_strong, ui.TEXT_2)
        self.status_title.pack(side="left", padx=(s(8), 0))
        self.status_detail = ui.label(status, "", f.meta, ui.TEXT_3, wraplength=s(172))
        self.status_detail.pack(fill="x", pady=(s(2), 0))
        self.status_bar = ui.ProgressBar(status, s, ui.BLUE, height=2)
        actions = tk.Frame(footer, bg=ui.SIDEBAR)
        actions.pack(fill="x", padx=s(12), pady=(0, s(12)))
        self.rescan_button = ui.Button(actions, "Rescan", self._rescan, s, f, icon=ui.ICON["refresh"])
        self.rescan_button.pack(side="left", fill="x", expand=True)
        ui.Button(actions, "", self._choose_folder, s, f, icon=ui.ICON["folder"]).pack(side="left", padx=(s(6), 0))

        self.main = tk.Frame(root, bg=ui.BACKGROUND)
        self.main.pack(side="left", fill="both", expand=True)
        self.pages = {"overview": self._build_overview(), "matches": self._build_matches(),
                      "spots": self._build_spots(), "token": self._build_token()}
        self.toaster = ui.Toaster(self.main, s, f)

        for index, (key, _glyph, _text) in enumerate(PAGES, start=1):
            root.bind(f"<Control-Key-{index}>", lambda _e, k=key: self.show(k))
        for keys in ("<F5>", "<Control-r>"):
            root.bind(keys, lambda _e: self._rescan())
        root.bind("<Control-o>", lambda _e: self._choose_folder())
        for key, amount in (("<Prior>", -1.0), ("<Next>", 1.0), ("<Up>", -0.15), ("<Down>", 0.15),
                            ("<Home>", -1e6), ("<End>", 1e6)):
            root.bind(key, lambda _e, a=amount: self._scroll_page(a))
        ui.apply_dark_titlebar(root)

    def _page(self, title: str, scroll: bool = False) -> tuple[tk.Frame, tk.Label, tk.Frame]:
        """Page shell: a slim header (title + context on the right) over the content. Returns
        (page, header meta label, body)."""
        s, f = self.scale, self.fonts
        page = tk.Frame(self.main, bg=ui.BACKGROUND)
        header = tk.Frame(page, bg=ui.BACKGROUND, height=s(52))
        header.pack(fill="x")
        header.pack_propagate(False)
        ui.label(header, title, f.title).pack(side="left", padx=(s(PAD), 0))
        meta = ui.label(header, "", f.meta, ui.TEXT_3)
        meta.pack(side="right", padx=(0, s(PAD)))
        ui.divider(page).pack(fill="x")
        if scroll:
            area = ui.ScrollArea(page, s)
            area.pack(fill="both", expand=True)
            page.scroller = area
            holder = area.body
        else:
            holder = page
        body = tk.Frame(holder, bg=ui.BACKGROUND)
        body.pack(fill="both", expand=True, padx=s(PAD), pady=(s(20), s(PAD)))
        return page, meta, body

    def _section(self, parent: tk.Misc, title: str) -> tk.Label:
        """Section heading; returns its right-aligned meta label."""
        s, f = self.scale, self.fonts
        head = tk.Frame(parent, bg=ui.BACKGROUND)
        head.pack(fill="x", pady=(0, s(6)))
        ui.label(head, title, f.body_strong).pack(side="left")
        meta = ui.label(head, "", f.meta, ui.TEXT_3)
        meta.pack(side="right")
        return meta

    def _build_overview(self) -> tk.Frame:
        s, f = self.scale, self.fonts
        page, self.overview_meta, body = self._page("Overview", scroll=True)

        hero = tk.Frame(body, bg=ui.BACKGROUND)
        hero.pack(fill="x")
        hero.columnconfigure(0, weight=1)
        left = tk.Frame(hero, bg=ui.BACKGROUND)
        left.grid(row=0, column=0, sticky="w")
        ui.label(left, "Total chests opened", f.meta, ui.TEXT_2).pack(anchor="w")
        self.total_label = ui.CountLabel(left, f.hero)
        self.total_label.pack(anchor="w")
        self.today_label = ui.label(left, "", f.meta, ui.TEXT_3)
        self.today_label.pack(anchor="w")
        right = tk.Frame(hero, bg=ui.BACKGROUND)
        right.grid(row=0, column=1, sticky="se", pady=(0, s(4)))
        ui.label(right, "Next milestone", f.meta, ui.TEXT_2).pack(anchor="w")
        goal = tk.Frame(right, bg=ui.BACKGROUND)
        goal.pack(fill="x", pady=(s(2), s(8)))
        self.milestone_label = ui.label(goal, "", f.metric)
        self.milestone_label.pack(side="left")
        self.milestone_hint = ui.label(goal, "", f.meta, ui.TEXT_3)
        self.milestone_hint.pack(side="right", anchor="s", pady=(0, s(4)))
        self.milestone_bar = ui.ProgressBar(right, s)
        self.milestone_bar.configure(width=s(260))
        self.milestone_bar.pack(fill="x")

        ui.divider(body).pack(fill="x", pady=(s(20), 0))
        strip = tk.Frame(body, bg=ui.BACKGROUND)
        strip.pack(fill="x")
        self.metrics: dict[str, ui.Metric] = {}
        names = (("matches", "Matches"), ("average", "Avg per match"), ("pace", "Per minute"),
                 ("squad", "With squad"), ("token", "Token chance"))
        for index, (key, name) in enumerate(names):
            if index:
                ui.divider(strip, vertical=True).grid(row=0, column=2 * index - 1, sticky="ns", pady=s(14))
            strip.columnconfigure(2 * index, weight=1, uniform="metric")
            metric = ui.Metric(strip, f, name, s)
            metric.grid(row=0, column=2 * index, sticky="w", padx=(0 if index == 0 else s(16), s(8)), pady=s(14))
            self.metrics[key] = metric
        ui.divider(body).pack(fill="x")

        self.split = tk.Frame(body, bg=ui.BACKGROUND)
        self.split.pack(fill="x", pady=(s(20), 0))
        self.kinds_box = tk.Frame(self.split, bg=ui.BACKGROUND)
        self.kinds_meta = self._section(self.kinds_box, "Chest types")
        self.kinds = ui.RankTable(self.kinds_box, s, f, "Chest types appear after your next counted match.")
        self.kinds.pack(fill="x")
        self.split_line = ui.divider(self.split, vertical=True)
        self.records_box = tk.Frame(self.split, bg=ui.BACKGROUND)
        self._section(self.records_box, "Personal records")
        self.records = ui.RankTable(self.records_box, s, f, "Records appear after your first match.")
        self.records.pack(fill="x")
        self.split.bind("<Configure>", self._layout_split)
        self._stacked: bool | None = None

        recent = tk.Frame(body, bg=ui.BACKGROUND)
        recent.pack(fill="x", pady=(s(24), 0))
        head = tk.Frame(recent, bg=ui.BACKGROUND)
        head.pack(fill="x", pady=(0, s(2)))
        ui.label(head, "Recent matches", f.body_strong).pack(side="left")
        ui.Link(head, "View all  →", lambda: self.show("matches"), f).pack(side="right")
        self.recent = ui.Table(recent, s, f, RECENT_COLUMNS, "No matches counted yet. Play a match with replays "
                                                            "turned on and it will show up here.", scrollable=False)
        self.recent.pack(fill="x")
        return page

    def _layout_split(self, event: tk.Event) -> None:
        stacked = event.width < self.scale(720)
        if stacked == self._stacked:
            return
        self._stacked = stacked
        s = self.scale
        for widget in (self.kinds_box, self.split_line, self.records_box):
            widget.grid_forget()
        if stacked:
            self.split.columnconfigure(0, weight=1)
            self.split.columnconfigure(2, weight=0)
            self.kinds_box.grid(row=0, column=0, sticky="nsew")
            self.records_box.grid(row=1, column=0, sticky="nsew", pady=(s(20), 0))
        else:
            self.split.columnconfigure(0, weight=11, uniform="split")
            self.split.columnconfigure(2, weight=9, uniform="split")
            self.kinds_box.grid(row=0, column=0, sticky="nsew", padx=(0, s(20)))
            self.split_line.grid(row=0, column=1, sticky="ns")
            self.records_box.grid(row=0, column=2, sticky="nsew", padx=(s(20), 0))

    def _build_matches(self) -> tk.Frame:
        s, f = self.scale, self.fonts
        page, self.matches_meta, body = self._page("Matches")
        body.pack_configure(pady=(s(8), 0))
        self.history = ui.Table(body, s, f, MATCH_COLUMNS, "No matches counted yet. Turn on replay recording in "
                                                           "Fortnite (Settings → Game → Replays), then play a match.")
        self.history.pack(fill="both", expand=True)
        return page

    def _build_spots(self) -> tk.Frame:
        s, f = self.scale, self.fonts
        page, self.spots_meta, body = self._page("Drop spots")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        well = ui.Panel(body, s)
        well.grid(row=0, column=0, sticky="nsew")
        self.heatmap = ui.HeatMap(well.body, s, f)
        self.heatmap.pack(fill="both", expand=True)
        legend = tk.Frame(well.body, bg=ui.SURFACE)
        legend.pack(fill="x", pady=(s(8), 0))
        ui.label(legend, "Fewer", f.meta, ui.TEXT_3).pack(side="left")
        swatches = ui.Canvas(legend, width=s(80), height=s(8))
        steps = s(80)
        for x in range(steps):  # continuous strip matching the overlay's ramp
            pos = x / (steps - 1) * (len(ui.HEAT) - 1)
            low, high = ui.HEAT[int(pos)], ui.HEAT[min(int(pos) + 1, len(ui.HEAT) - 1)]
            swatches.create_line(x, 0, x, s(8), fill=ui.mix(low, high, pos - int(pos)))
        swatches.pack(side="left", padx=s(8))
        ui.label(legend, "More chests", f.meta, ui.TEXT_3).pack(side="left")
        self.heatmap.configure(cursor="crosshair")  # dots show a tooltip on hover

        side = tk.Frame(body, bg=ui.BACKGROUND, width=s(330))
        side.grid(row=0, column=1, sticky="nsew", padx=(s(20), 0))
        side.pack_propagate(False)
        # The list keeps a readable width; the map takes whatever is left.
        body.bind("<Configure>", lambda e: side.configure(width=min(max(e.width * 0.38, s(260)), s(340))))
        self.spots_note = self._section(side, "Top locations")
        self.spot_rows = ui.RankTable(side, s, f, "No chest locations yet.", label_share=0.52)
        self.spot_rows.pack(fill="x")
        self.spots_hint = ui.label(side, "", f.meta, ui.TEXT_3)
        self.spots_hint.pack(fill="x", pady=(s(12), 0))
        side.bind("<Configure>", lambda e: self.spots_hint.configure(wraplength=e.width - s(8)))
        return page

    def _build_token(self) -> tk.Frame:
        s, f = self.scale, self.fonts
        page, self.token_meta, body = self._page("Pirate Token", scroll=True)
        hero = tk.Frame(body, bg=ui.BACKGROUND)
        hero.pack(fill="x")
        hero.columnconfigure(0, weight=1)
        left = tk.Frame(hero, bg=ui.BACKGROUND)
        left.grid(row=0, column=0, sticky="w")
        ui.label(left, "Chance you'd have found it by now", f.meta, ui.TEXT_2).pack(anchor="w")
        self.token_chance = ui.CountLabel(left, f.hero, _percent)
        self.token_chance.pack(anchor="w")
        self.token_line = ui.label(left, "", f.meta, ui.TEXT_3)
        self.token_line.pack(anchor="w")
        right = tk.Frame(hero, bg=ui.BACKGROUND)
        right.grid(row=0, column=1, sticky="se", pady=(0, s(4)))
        ui.label(right, "Average wait", f.meta, ui.TEXT_2).pack(anchor="w")
        wait = tk.Frame(right, bg=ui.BACKGROUND)
        wait.pack(fill="x", pady=(s(2), s(8)))
        ui.label(wait, f"{stats.TOKEN_ODDS:,}", f.metric).pack(side="left")
        self.wait_hint = ui.label(wait, "", f.meta, ui.TEXT_3)
        self.wait_hint.pack(side="right", anchor="s", pady=(0, s(4)))
        self.wait_bar = ui.ProgressBar(right, s)
        self.wait_bar.configure(width=s(260))
        self.wait_bar.pack(fill="x")

        ui.divider(body).pack(fill="x", pady=(s(20), s(20)))
        self.forecast_meta = self._section(body, "Forecast at your pace")
        self.forecast = ui.Table(body, s, f, FORECAST_COLUMNS, "Your forecast appears after your first match.",
                                 scrollable=False)
        self.forecast.pack(fill="x")
        return page

    def show(self, key: str) -> None:
        if key == self.page:
            return
        if self.page:
            self.pages[self.page].place_forget()
        self.page = key
        self.nav.select(key)
        page = self.pages[key]
        shift = self.scale(8)
        ui.animate(self.main, "page", 180, lambda t: page.place(x=0, y=round(shift * (1 - t)), relwidth=1, relheight=1))

    def _scroll_page(self, pages: float) -> None:
        page = self.pages[self.page]
        if self.page == "matches":
            self.history.scroll(pages * self.history.page())
        elif hasattr(page, "scroller"):
            page.scroller.scroll(pages * page.winfo_height() * 0.8)

    # ------------------------------------------------------------ data

    def _refresh(self) -> None:
        entries = self.store.history()
        total = self.store.total
        summary = stats.summarize(entries, total)
        played = bool(entries)
        self.overview_meta.configure(text=f"{_plural(summary.matches, 'match')} over {_plural(summary.days, 'day')}"
                                     if played else "")

        self.total_label.set(total, duration=800)
        if summary.today:
            self.today_label.configure(text=f"+{summary.today:,} today in {_plural(summary.today_matches, 'match')}",
                                       fg=ui.ACCENT_TEXT)
        else:
            self.today_label.configure(text="No matches yet today", fg=ui.TEXT_3)
        goal, floor = stats.next_milestone(total), stats.milestone_at(total)
        self.milestone_label.configure(text=f"{goal:,}")
        self.milestone_hint.configure(text=f"{goal - total:,} to go")
        self.milestone_bar.set((total - floor) / max(goal - floor, 1))

        m = self.metrics
        m["matches"].set(summary.matches if played else None, lambda v: f"{v:,.0f}",
                         f"{_plural(summary.days, 'day')} played" if played else "None counted yet")
        recent_up = summary.recent_average > summary.average
        m["average"].set(summary.average if played else None, lambda v: f"{v:.1f}",
                         f"Last {min(stats.RECENT, summary.matches)}: {summary.recent_average:.1f}"
                         if played else "—", positive=played and recent_up)
        m["pace"].set(summary.per_minute or None, lambda v: f"{v:.1f}",
                      f"~{summary.per_hour:,.0f} per hour" if summary.per_hour else "—")
        if summary.squad_matches:
            squad_avg = summary.squad_total / summary.squad_matches
            gain = (summary.squad_total - summary.squad_own) / summary.squad_matches
            m["squad"].set(squad_avg, lambda v: f"{v:.1f}", f"+{gain:.1f} from team", positive=gain > 0)
        else:
            m["squad"].set(None, str, "Squad games only")
        odds = stats.token_odds(total)
        m["token"].set(odds.chance, _percent, f"1 in {stats.TOKEN_ODDS:,}")

        peak = max((count for _name, count in summary.kinds), default=1)
        opened = sum(count for _name, count in summary.kinds)
        self.kinds.set([ui.Rank(name, f"{count:,}", count / peak, f"{count / opened:.0%}")
                        for name, count in summary.kinds])
        self.kinds_meta.configure(text=f"{_plural(summary.detailed, 'match')} with detail" if summary.detailed else "")
        self.records.set(self._records(summary.records))

        best = summary.records.best_match
        rows = [self._match_row(e, e is best) for e in entries]
        self.recent.set_rows([self._match_row(e, e is best, dated=True) for e in entries[:RECENT_ROWS]])
        self.history.set_rows(self._grouped(entries, rows))
        self.matches_meta.configure(text=f"{_plural(len(entries), 'match')} · "
                                         f"{sum(e.chests for e in entries):,} chests" if entries else "")
        self._refresh_spots(entries)
        self._refresh_token(total, summary)

    @staticmethod
    def _records(rec: stats.Records) -> list[ui.Rank]:
        rows = []
        if rec.best_match:
            rows.append(ui.Rank("Best match", f"{rec.best_match.chests}", note=_when(rec.best_match)))
        if rec.best_day:
            rows.append(ui.Rank("Best day", f"{rec.best_day[1]:,}", note=f"{rec.best_day[0]:%a %b} {rec.best_day[0].day}"))
        if rec.fastest:
            rows.append(ui.Rank("Fastest pace", f"{stats.per_minute(rec.fastest):.1f}/min",
                                note=f"{rec.fastest.chests} in {rec.fastest.duration_s // 60} min"))
        if rec.best_streak:
            rows.append(ui.Rank("Current streak", _plural(rec.streak, "day"), note=f"best {rec.best_streak}"))
        return rows

    def _match_row(self, entry: ReplayEntry, best: bool, dated: bool = False) -> ui.TableRow:
        when = datetime.fromisoformat(entry.match_time).astimezone() if entry.match_time else None
        spot = stats.spots([entry], self.map.place) if self.map.pois else []
        if when and dated:
            day = _day_name(when.date())
            played = f"{day if len(day) < 10 else f'{when:%b} {when.day}'}  {when:%H:%M}"
        else:
            played = f"{when:%H:%M}" if when else entry.file
        return ui.TableRow((
            played,
            f"{entry.duration_s // 60} min" if entry.duration_s else "—",
            f"{stats.per_minute(entry):.1f}/min" if entry.duration_s >= 60 else "—",
            spot[0].name if spot else "—",
            f"{entry.squad}" if entry.squad >= 0 and entry.squad_size > 1 else "—",
            f"{entry.chests}"), tag="Best" if best else "")

    @staticmethod
    def _grouped(entries: list[ReplayEntry], rows: list[ui.TableRow]) -> list[ui.TableRow]:
        """Insert a header with the day's totals before each day's matches."""
        out: list[ui.TableRow] = []
        days: dict[date | None, list[int]] = {}
        for index, entry in enumerate(entries):
            days.setdefault(stats.local_date(entry), []).append(index)
        for day, indexes in days.items():
            chests = sum(entries[i].chests for i in indexes)
            out.append(ui.TableRow((_day_name(day) if day else "Unknown date",
                                    f"{_plural(len(indexes), 'match')} · {chests:,} chests"), group=True))
            out.extend(rows[i] for i in indexes)
        return out

    def _refresh_spots(self, entries: list[ReplayEntry]) -> None:
        located = [e for e in entries if any(row[2] is not None for row in e.opened)]
        self.spots_meta.configure(text=f"{_plural(len(located), 'match')} with chest locations" if located else "")
        if self.map.pois:
            ranked = stats.spots(located, self.map.place)[:12]
            peak = max((spot.chests for spot in ranked), default=1)
            self.spot_rows.set([ui.Rank(spot.name, f"{spot.chests:,}", spot.chests / peak,
                                        f"{spot.per_match:.1f}/match") for spot in ranked])
            self.spots_note.configure(text="chests · per match" if ranked else "")
            self.spots_hint.configure(text="" if ranked else "Locations appear after your next counted match.")
        elif self.map_loaded:
            self.spots_hint.configure(text="Location names couldn't be loaded. Check your connection and "
                                           "reopen Chest Counter.")
        else:
            self.spots_hint.configure(text="Loading location names…")
        if self.map.calibrated:
            points = []
            for (cx, cy), count in stats.heat(located).items():
                wx, wy = cx * stats.HEAT_CELL, cy * stats.HEAT_CELL
                px, py = self.map.to_pixel(wx, wy)
                points.append(ui.HeatPoint(px, py, count, self.map.place(wx, wy)))
            self.heatmap.set_points(points, ("No chest locations yet",
                                             "Your chests appear on the map after your next counted match."))

    def _refresh_token(self, total: int, summary: stats.Summary) -> None:
        odds = stats.token_odds(total)
        self.token_chance.set(odds.chance, duration=900)
        self.token_line.configure(text=f"After {total:,} chests, at 1 in {stats.TOKEN_ODDS:,} per chest")
        self.wait_hint.configure(text=f"{total / stats.TOKEN_ODDS:.1%} of the way")
        self.wait_bar.set(total / stats.TOKEN_ODDS)
        self.token_meta.configure(text=f"{total:,} chests opened")
        self.forecast_meta.configure(text=f"{summary.average:.0f} chests a match · ~{summary.per_hour:,.0f} an hour"
                                     if summary.average else "")
        rows = []
        for target in TOKEN_TARGETS:
            need = odds.chests_for(target)
            left = max(need - total, 0)
            matches = f"~{left / summary.average:,.0f}" if left and summary.average else "—"
            hours = f"~{left / summary.per_hour:,.0f} h" if left and summary.per_hour else "—"
            rows.append(ui.TableRow((f"{target:.0%} chance", f"{need:,}", f"{left:,}" if left else "Reached",
                                     matches, hours if left else "Reached")))
        self.forecast.set_rows(rows if summary.matches or total else [])

    def _set_map(self, data: mapdata.MapData) -> None:
        self.map, self.map_loaded = data, True
        if not data.pois:
            message = ("Couldn't load the map", "Check your internet connection, then reopen Chest Counter.")
        elif not data.calibrated or not data.image:
            message = ("The map changed this season", "Top locations still work. The heatmap returns in an update.")
        else:
            message = ("", "")
        self.heatmap.set_image(data.image, mapdata.MAP_CROP, message)
        self._refresh()

    def _celebrate(self, new_entries: list[ReplayEntry]) -> None:
        # Only matches played in the last few hours, so importing old replays doesn't spam toasts.
        recent = [e for e in new_entries if e.match_time and
                  datetime.now(timezone.utc) - datetime.fromisoformat(e.match_time) < timedelta(hours=3)]
        for entry in recent:
            others = [e.chests for e in self.store.history() if e.guid != entry.guid]
            if others and entry.chests > max(others):
                self.toaster.show("New personal best", f"{entry.chests} chests in one match.")
        reached = stats.milestone_at(self.store.total)
        if reached > (self.store.milestone or 0):
            self.store.set_milestone(reached)
            if not recent:  # importing older replays: record the milestone quietly
                return
            self.toaster.show(f"{reached:,} chests opened", "Milestone reached. Next stop: "
                                                            f"{stats.next_milestone(self.store.total):,}.")

    def _set_status(self, text: str) -> None:
        lowered = text.lower()
        progress = re.search(r"(\d+)%$", text)
        if lowered.startswith("reading"):
            state, title = "busy", "Reading replays"
            subject = text[len("Reading "):].split(" ...")[0]
            detail = f"{subject} · {progress.group(1)}%" if progress else subject
        elif lowered.startswith("checking"):
            state, title, detail = "busy", "Checking for replays", "Looking in your replay folder…"
        elif lowered.startswith("folder not found"):
            state, title, detail = "error", "Replay folder not found", "Pick it with the folder button below."
        elif any(word in lowered for word in ("could not", "skipped", "error")):
            state, title, detail = "error", "A replay was skipped", text
        elif lowered.startswith("watching"):
            recording = re.search(r"\((\d+) still recording\)", text)
            state, title = "ok", "Up to date"
            detail = f"Checked {datetime.now():%H:%M}" + (f" · {recording.group(1)} recording" if recording else "")
        else:
            state, title, detail = "ok", text, ""
        self._status_state = state
        self.dot.set(state)
        self.status_title.configure(text=title)
        self.status_detail.configure(text=detail)
        if progress and state == "busy":
            self.status_bar.pack(fill="x", pady=(self.scale(8), 0))
            self.status_bar.set(int(progress.group(1)) / 100, duration=250)
        else:
            self.status_bar.pack_forget()

    def _rescan(self) -> None:
        """Rescan with visible feedback: the button and status show the scan, and a toast reports the
        result, even when nothing new is found (otherwise a quick scan looks like nothing happened)."""
        if self._rescan_target:
            return
        self._rescan_target = self.watcher.scans_started + 1
        self._rescan_found = 0
        self._rescan_started = time.monotonic()
        self.rescan_button.set_state("Scanning…", enabled=False)
        self._set_status("Checking for replays")
        self.watcher.rescan()

    def _finish_rescan(self) -> None:
        self._rescan_target = 0
        self.rescan_button.set_state("Rescan")
        self._set_status(self._last_status or "Watching for new replays")
        if self._status_state == "error":
            self.toaster.show("Rescan finished with a problem", self.status_detail.cget("text"))
        elif self._rescan_found:
            self.toaster.show("Rescan complete", f"Counted {_plural(self._rescan_found, 'replay')}.")
        else:
            self.toaster.show("Rescan complete", "No new replays found. You're up to date.")

    def _drain_events(self) -> None:
        changed = False
        status = None
        finished = 0
        fresh: list[ReplayEntry] = []
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    status = str(payload)  # only the latest one is worth drawing
                elif kind == "map":
                    self._set_map(payload)
                elif kind == "scan":
                    finished = payload
                else:
                    entry, new = payload
                    changed = True
                    self._rescan_found += bool(self._rescan_target)
                    if new:
                        fresh.append(entry)
        except queue.Empty:
            pass
        if status is not None:
            self._last_status = status
            # While a rescan is showing its progress, hold back the "all done" status until it finishes.
            if not (self._rescan_target and status.lower().startswith("watching")):
                self._set_status(status)
        if changed:
            self._refresh()
            self._celebrate(fresh)
        if self._rescan_target > 0 and finished >= self._rescan_target:
            self._rescan_target = -1  # finishing; keep the busy state up long enough to be seen
            wait = max(0.0, RESCAN_MIN_SECONDS - (time.monotonic() - self._rescan_started))
            self.root.after(int(wait * 1000), self._finish_rescan)
        self._drain_job = self.root.after(300, self._drain_events)

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
        self.dot.set("ok")  # stops the pulse loop
        if getattr(self, "_drain_job", None):
            self.root.after_cancel(self._drain_job)
        self.root.destroy()


def _when(entry: ReplayEntry) -> str:
    if not entry.match_time:
        return entry.file
    when = datetime.fromisoformat(entry.match_time).astimezone()
    return f"{when:%a %b} {when.day}"


def _parse_one(path: str) -> int:
    from fortnite_replay import ReplayError, count_chests
    try:
        result = count_chests(path)
    except (ReplayError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{Path(path).name}: {result.count} chests opened by you, {len(result.squad_opened)} with your squad "
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
    parser.add_argument("--no-update", action="store_true", help="skip the update check")
    args = parser.parse_args(argv)

    if args.parse:
        return _parse_one(args.parse)

    # Replays are parsed in worker processes; a short GIL switch interval keeps animations smooth.
    sys.setswitchinterval(0.001)
    ui.enable_dpi_awareness()
    root = tk.Tk()
    if not args.no_update and not _check_for_updates(root):
        return 0
    store = CountStore(args.count_file, args.start)
    folder = args.folder or (Path(store.folder) if store.folder and Path(store.folder).is_dir()
                             else DEFAULT_FOLDER)
    ChestCounterApp(root, store, folder)
    root.mainloop()
    return 0


def _window_open(root: tk.Tk) -> bool:
    try:
        return bool(root.winfo_exists())
    except tk.TclError:
        return False


def _check_for_updates(root: tk.Tk) -> bool:
    root.title("Chest Counter")
    ui.set_window_icon(root, ICON_FILE)
    root.configure(bg=ui.BACKGROUND)
    scale = ui.Scale(root)
    fonts = ui.Fonts(root, scale)
    root.geometry(f"{scale(900)}x{scale(600)}")
    root.minsize(scale(860), scale(560))
    ui.apply_dark_titlebar(root)
    root.protocol("WM_DELETE_WINDOW", root.destroy)

    curtain = tk.Frame(root, bg=ui.BACKGROUND)
    curtain.pack(fill="both", expand=True)
    message = tk.Label(curtain, text="Checking for updates…", bg=ui.BACKGROUND, fg=ui.TEXT_2, font=fonts.body)
    message.place(relx=0.5, rely=0.5, anchor="center")
    root.update()

    state = {"text": "Checking for updates…", "updated": False}

    def worker() -> None:
        state["updated"] = updater.apply(lambda text: state.__setitem__("text", text))

    started = time.monotonic()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while thread.is_alive() or time.monotonic() - started < CHECK_SECONDS:
        if not _window_open(root):
            return False
        if message.cget("text") != state["text"]:
            message.configure(text=state["text"])
        root.update()
        time.sleep(0.05)
    if not _window_open(root):
        return False
    if state["updated"]:
        root.destroy()
        subprocess.Popen([sys.executable, str(APP_DIR / "app.pyw")], cwd=str(APP_DIR))
        return False
    curtain.destroy()
    return True

if __name__ == "__main__":
    sys.exit(main())
