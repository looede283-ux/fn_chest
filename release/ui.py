
from __future__ import annotations

import ctypes
import math
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from typing import Callable, Optional

BACKGROUND = "#212121"   # main canvas
SIDEBAR = "#181818"
SURFACE = "#262626"      # recessed wells (map) and the toast
HOVER = "#2a2a2a"
ACTIVE = "#303030"
BORDER = "#2e2e2e"       # hairline dividers
BORDER_STRONG = "#3a3a3a"  # control outlines
TRACK = "#333333"
TEXT = "#ececec"
TEXT_2 = "#a9a9a9"       # secondary
TEXT_3 = "#8a8a8a"       # metadata (still >= 4.5:1 on the background)
BAR = "#6e6e6e"          # neutral data bars
ACCENT = "#e2a93b"       # treasure gold: progress, positive changes, highlights
ACCENT_TEXT = "#f0bf5a"
GREEN = "#3fb950"        # status only: up to date
BLUE = "#6aa3f5"         # status only: reading replays
RED = "#e5534b"          # status only: errors
TOOLTIP = "#0d0d0d"
HEAT = ("#4a0c6b", "#a52c60", "#ed6925", "#fbb61a", "#fcffa4")  # few -> many chests (inferno-like)

TEXT_FACES = (("Segoe UI Variable Text", "Segoe UI Variable Text Semibold"), ("Segoe UI", "Segoe UI Semibold"))
DISPLAY_FACES = ("Segoe UI Variable Display Semib", "Segoe UI Semibold")
ICON_FACES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
FRAME_MS = 15

ICON = {"home": "\ue80f", "history": "\ue81c", "pin": "\ue707", "star": "\ue735",
        "folder": "\ue8b7", "refresh": "\ue72c", "chest": "\ue7b8"}


# ---------------------------------------------------------------- helpers

def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def mix(a: str, b: str, t: float) -> str:
    ra, ga, ba = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    rb, gb, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


def animate(widget: tk.Misc, key: str, duration_ms: int, step: Callable[[float], None],
            done: Optional[Callable[[], None]] = None) -> None:
    """Run `step(eased progress)` every frame for `duration_ms`, replacing any running `key`."""
    jobs = widget.__dict__.setdefault("_animations", {})
    if key in jobs:
        widget.after_cancel(jobs.pop(key))
    start = time.perf_counter()

    def tick() -> None:
        t = min((time.perf_counter() - start) * 1000 / duration_ms, 1.0)
        try:
            step(ease_out(t))
        except tk.TclError:
            return
        if t < 1:
            jobs[key] = widget.after(FRAME_MS, tick)
        else:
            jobs.pop(key, None)
            if done:
                done()

    tick()


class Scale:
    def __init__(self, root: tk.Misc) -> None:
        self.factor = root.winfo_fpixels("1i") / 96.0

    def __call__(self, pixels: float) -> int:
        return max(1, round(pixels * self.factor))


class Fonts:
    def __init__(self, root: tk.Misc, scale: Scale) -> None:
        available = set(tkfont.families(root))
        regular, semibold = next((pair for pair in TEXT_FACES if pair[0] in available), ("Segoe UI", "Segoe UI"))
        semibold = semibold if semibold in available else regular
        display = next((face for face in DISPLAY_FACES if face in available), semibold)
        icons = next((face for face in ICON_FACES if face in available), regular)

        def make(pixels: int, face: str) -> tkfont.Font:
            return tkfont.Font(root=root, family=face, size=-scale(pixels))

        self.meta = make(12, regular)
        self.meta_strong = make(12, semibold)
        self.body = make(13, regular)
        self.body_strong = make(13, semibold)
        self.title = make(15, semibold)
        self.metric = make(20, display)
        self.hero = make(40, display)
        self.icon = make(14, icons)


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def set_window_icon(window: tk.Tk, icon: Path) -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ChestCounter.App")
    except (AttributeError, OSError):
        pass
    if icon.exists():
        window.iconbitmap(default=str(icon))


def apply_dark_titlebar(window: tk.Tk) -> None:
    try:
        window.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        handle = user32.GetParent(window.winfo_id())
        dwm = ctypes.windll.dwmapi
        # dark mode, border colour, caption colour (COLORREF is 0x00BBGGRR)
        for attribute, value in ((20, 1), (34, 0x00181818), (35, 0x00181818)):
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(handle), attribute,
                                      ctypes.byref(ctypes.c_int(value)), 4)
    except (AttributeError, OSError):
        pass


def rounded_polygon(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                    radius: float, **options) -> int:
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
              x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
              x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=16, **options)


def shorten(text: str, font: tkfont.Font, width: float) -> str:
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…" if text else ""


def label(parent: tk.Misc, text: str, font: tkfont.Font, fg: str = TEXT, **options) -> tk.Label:
    return tk.Label(parent, text=text, font=font, fg=fg, bg=parent["bg"], anchor="w", justify="left", **options)


def divider(parent: tk.Misc, vertical: bool = False) -> tk.Frame:
    return tk.Frame(parent, bg=BORDER, **({"width": 1} if vertical else {"height": 1}))


class Canvas(tk.Canvas):
    """Borderless canvas that inherits its parent's background."""

    def __init__(self, parent: tk.Misc, **options) -> None:
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0, **options)


# ---------------------------------------------------------------- layout

class ScrollArea(tk.Frame):
    """A vertically scrolling frame (`.body`) with a slim overlay scrollbar."""

    def __init__(self, parent: tk.Misc, scale: Scale) -> None:
        super().__init__(parent, bg=parent["bg"])
        self._scale = scale
        self._canvas = Canvas(self)
        self._canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self._canvas, bg=self["bg"])
        self._window = self._canvas.create_window(0, 0, anchor="nw", window=self.body)
        self._bar = Canvas(self, width=scale(8))
        self._offset = self._target = 0.0
        self._canvas.bind("<Configure>", self._layout)
        self.body.bind("<Configure>", self._layout)
        self.bind("<Enter>", lambda _e: self.bind_all("<MouseWheel>", self._wheel))
        self.bind("<Leave>", lambda _e: self.unbind_all("<MouseWheel>"))

    def scroll(self, pixels: float) -> None:
        self._scroll_to(self._target + pixels)

    def _limit(self) -> float:
        return max(self.body.winfo_reqheight() - self._canvas.winfo_height(), 0)

    def _layout(self, _event: object = None) -> None:
        self._canvas.itemconfigure(self._window, width=self._canvas.winfo_width())
        self._scroll_to(self._offset, animate_it=False)

    def _wheel(self, event: tk.Event) -> None:
        self.scroll(-event.delta / 120 * self._scale(80))

    def _scroll_to(self, offset: float, animate_it: bool = True) -> None:
        target = min(max(offset, 0), self._limit())
        self._target = target
        start = self._offset

        def step(t: float) -> None:
            self._offset = start + (target - start) * t
            self._canvas.coords(self._window, 0, -round(self._offset))
            self._draw_bar()

        if animate_it:
            animate(self, "scroll", 160, step)
        else:
            step(1.0)

    def _draw_bar(self) -> None:
        view, content = self._canvas.winfo_height(), self.body.winfo_reqheight()
        self._bar.delete("all")
        if content <= view or view <= 1:
            self._bar.place_forget()
            return
        self._bar.place(relx=1, rely=0, relheight=1, anchor="ne")
        s = self._scale
        size = max(view * view / content, s(40))
        top = (view - size) * self._offset / (content - view)
        rounded_polygon(self._bar, s(3), top + s(3), s(6), top + size - s(3), s(2), fill=BORDER_STRONG,
                        outline=BORDER_STRONG)


class Panel(Canvas):
    """A recessed rounded surface hosting `.body`; used sparingly (the map well)."""

    def __init__(self, parent: tk.Misc, scale: Scale, padding: int = 12) -> None:
        super().__init__(parent)
        self._scale, self._padding = scale, scale(padding)
        self.body = tk.Frame(self, bg=SURFACE)
        self._window = self.create_window(self._padding, self._padding, anchor="nw", window=self.body)
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event: tk.Event) -> None:
        self.delete("frame")
        rounded_polygon(self, 1, 1, event.width - 1, event.height - 1, self._scale(10),
                        fill=SURFACE, outline=BORDER, tags="frame")
        self.tag_lower("frame")
        self.itemconfigure(self._window, width=max(event.width - 2 * self._padding, 1),
                           height=max(event.height - 2 * self._padding, 1))


# ---------------------------------------------------------------- controls

class Button(Canvas):
    """`secondary` = outlined, `ghost` = text only until hovered. Icon-only when `text` is empty."""

    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None], scale: Scale, fonts: Fonts,
                 variant: str = "secondary", icon: str = "") -> None:
        self._text, self._icon, self._command, self._scale, self._fonts = text, icon, command, scale, fonts
        self._rest = parent["bg"]
        self._border = BORDER_STRONG if variant == "secondary" else None
        self._ink = TEXT if variant == "secondary" else TEXT_2
        self._color = self._rest
        self._enabled = True
        gap = scale(6) if icon and text else 0
        content = fonts.body.measure(text) + (fonts.icon.measure(icon) if icon else 0) + gap
        width = content + 2 * scale(10) if text else scale(30)
        super().__init__(parent, width=width, height=scale(30), cursor="hand2")
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._fade(HOVER, TEXT))
        self.bind("<Leave>", lambda _e: self._fade(self._rest, TEXT if self._border else TEXT_2))
        self.bind("<ButtonRelease-1>", self._release)

    def _fade(self, target: str, ink: str) -> None:
        start = self._color

        def step(t: float) -> None:
            self._color = mix(start, target, t)
            self._draw(ink)

        animate(self, "hover", 110, step)

    def set_state(self, text: str, enabled: bool = True) -> None:
        """Relabel the button; a disabled button is dimmed and ignores clicks (e.g. "Scanning…")."""
        self._text, self._enabled = text, enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _release(self, event: tk.Event) -> None:
        if self._enabled and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self._command()

    def _draw(self, ink: Optional[str] = None) -> None:
        self.delete("all")
        ink = (ink or self._ink) if self._enabled else TEXT_3
        w, h, s, f = self.winfo_width(), self.winfo_height(), self._scale, self._fonts
        rounded_polygon(self, 1, 1, w - 1, h - 1, s(8), fill=self._color, outline=self._border or self._color)
        icon_w = f.icon.measure(self._icon) if self._icon else 0
        gap = s(6) if self._icon and self._text else 0
        x = (w - icon_w - gap - f.body.measure(self._text)) / 2
        if self._icon:
            self.create_text(x, h / 2 + 1, text=self._icon, anchor="w", fill=ink, font=f.icon)
        if self._text:
            self.create_text(x + icon_w + gap, h / 2, text=self._text, anchor="w", fill=ink, font=f.body)


class Link(tk.Label):
    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None], fonts: Fonts) -> None:
        super().__init__(parent, text=text, font=fonts.meta, fg=TEXT_2, bg=parent["bg"], cursor="hand2")
        self.bind("<Enter>", lambda _e: self.configure(fg=TEXT))
        self.bind("<Leave>", lambda _e: self.configure(fg=TEXT_2))
        self.bind("<ButtonRelease-1>", lambda _e: command())


class Nav(Canvas):
    """Sidebar navigation: sliding active highlight, hover wash, shortcut hint on hover."""

    def __init__(self, parent: tk.Misc, items: list[tuple[str, str, str]], scale: Scale, fonts: Fonts,
                 on_select: Callable[[str], None]) -> None:
        self._items, self._scale, self._fonts, self._on_select = items, scale, fonts, on_select
        self._row = scale(34)
        super().__init__(parent, height=self._row * len(items), cursor="hand2")
        self._active = 0
        self._pill = 0.0
        self._hover: Optional[int] = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Motion>", lambda e: self._set_hover(self._index(e.y)))
        self.bind("<Leave>", lambda _e: self._set_hover(None))
        self.bind("<ButtonRelease-1>", self._click)

    def select(self, key: str) -> None:
        index = next(i for i, item in enumerate(self._items) if item[0] == key)
        start, self._active = self._pill, index
        animate(self, "pill", 180, lambda t: (setattr(self, "_pill", start + (index - start) * t), self._draw()))

    def _index(self, y: int) -> Optional[int]:
        index = y // self._row
        return index if 0 <= index < len(self._items) else None

    def _set_hover(self, index: Optional[int]) -> None:
        if index != self._hover:
            self._hover = index
            self._draw()

    def _click(self, event: tk.Event) -> None:
        index = self._index(event.y)
        if index is not None and index != self._active:
            self._on_select(self._items[index][0])

    def _draw(self) -> None:
        self.delete("all")
        s, w, row, f = self._scale, self.winfo_width(), self._row, self._fonts
        if self._hover is not None and self._hover != self._active:
            top = self._hover * row
            rounded_polygon(self, 0, top + s(1), w, top + row - s(1), s(8), fill=HOVER, outline=HOVER)
        top = self._pill * row
        rounded_polygon(self, 0, top + s(1), w, top + row - s(1), s(8), fill=ACTIVE, outline=ACTIVE)
        for index, (_key, glyph, text) in enumerate(self._items):
            middle = index * row + row / 2
            active = index == self._active
            self.create_text(s(10), middle + 1, text=glyph, anchor="w", fill=TEXT if active else TEXT_3, font=f.icon)
            self.create_text(s(34), middle, text=text, anchor="w", fill=TEXT if active else TEXT_2, font=f.body)
            if index == self._hover:
                self.create_text(w - s(10), middle, text=f"Ctrl+{index + 1}", anchor="e", fill=TEXT_3, font=f.meta)


class CountLabel(tk.Label):
    """A label whose number counts to new values."""

    def __init__(self, parent: tk.Misc, font: tkfont.Font, fmt: Callable[[float], str] = lambda v: f"{v:,.0f}",
                 fg: str = TEXT) -> None:
        super().__init__(parent, text=fmt(0), font=font, fg=fg, bg=parent["bg"], anchor="w")
        self._fmt = fmt
        self._value = 0.0

    def set(self, value: Optional[float], fmt: Optional[Callable[[float], str]] = None, duration: int = 600) -> None:
        if fmt:
            self._fmt = fmt
        if value is None:  # no data yet: an em dash, never a misleading zero
            self._value = 0.0
            self.configure(text="—", fg=TEXT_3)
            return
        self.configure(fg=TEXT)
        start = self._value
        animate(self, "count", duration,
                lambda t: (setattr(self, "_value", start + (value - start) * t),
                           self.configure(text=self._fmt(self._value))))


class ProgressBar(Canvas):
    def __init__(self, parent: tk.Misc, scale: Scale, color: str = ACCENT, height: int = 4) -> None:
        super().__init__(parent, height=scale(height))
        self._color = color
        self._value = 0.0
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, fraction: float, duration: int = 700) -> None:
        start, target = self._value, max(0.0, min(fraction, 1.0))
        animate(self, "fill", duration, lambda t: (setattr(self, "_value", start + (target - start) * t), self._draw()))

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        rounded_polygon(self, 0, 0, w, h, h / 2, fill=TRACK, outline=TRACK)
        if self._value > 0:
            rounded_polygon(self, 0, 0, max(w * self._value, h), h, h / 2, fill=self._color, outline=self._color)


class Metric(tk.Frame):
    """Label, value and a line of context: a number is never shown without a comparison."""

    def __init__(self, parent: tk.Misc, fonts: Fonts, name: str, scale: Scale) -> None:
        super().__init__(parent, bg=parent["bg"])
        label(self, name, fonts.meta, TEXT_2).pack(anchor="w")
        self.value = CountLabel(self, fonts.metric)
        self.value.pack(anchor="w", pady=(scale(2), 0))
        self.context = label(self, "", fonts.meta, TEXT_3)
        self.context.pack(anchor="w")

    def set(self, value: Optional[float], fmt: Callable[[float], str], context: str, positive: bool = False) -> None:
        self.value.set(value, fmt)
        self.context.configure(text=context, fg=ACCENT_TEXT if positive else TEXT_3)


@dataclass
class Rank:
    label: str
    value: str
    fraction: Optional[float] = None  # None: no bar (key/value rows)
    note: str = ""


class RankTable(Canvas):
    """Compact rows: label · neutral bar · note · value. Doubles as a key/value list without bars."""

    def __init__(self, parent: tk.Misc, scale: Scale, fonts: Fonts, empty_text: str, row: int = 30,
                 label_share: float = 0.42) -> None:
        super().__init__(parent, height=scale(row))
        self._scale, self._fonts, self._empty, self._label_share = scale, fonts, empty_text, label_share
        self._rows: list[Rank] = []
        self._grow = 1.0
        self._hover: Optional[int] = None
        self._row = scale(row)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Motion>", lambda e: self._set_hover(int(e.y // self._row)))
        self.bind("<Leave>", lambda _e: self._set_hover(None))

    def set(self, rows: list[Rank]) -> None:
        changed = [(r.label, r.value) for r in rows] != [(r.label, r.value) for r in self._rows]
        self._rows = rows
        self.configure(height=max(len(rows), 1) * self._row)
        if changed:
            animate(self, "grow", 550, lambda t: (setattr(self, "_grow", t), self._draw()))
        else:
            self._draw()

    def _set_hover(self, index: Optional[int]) -> None:
        index = index if index is not None and 0 <= index < len(self._rows) else None
        if index != self._hover:
            self._hover = index
            self._draw()

    def _draw(self) -> None:
        self.delete("all")
        s, w, f = self._scale, self.winfo_width(), self._fonts
        if not self._rows:
            self.create_text(0, self._row / 2, text=self._empty, anchor="w", fill=TEXT_3, font=f.body)
            return
        pad = s(8)
        value_w = max(f.body_strong.measure(r.value) for r in self._rows)
        note_w = max((f.meta.measure(r.note) for r in self._rows if r.note), default=0)
        right_value = w - pad
        right_note = right_value - value_w - s(16)
        bars = any(r.fraction is not None for r in self._rows)
        label_w = (w - 2 * pad) * self._label_share if bars else right_note - note_w - s(16)
        bar_x1 = pad + label_w + s(8)
        bar_x2 = right_note - note_w - (s(16) if note_w else 0)
        for index, row in enumerate(self._rows):
            top = index * self._row
            middle = top + self._row / 2
            if index == self._hover:
                rounded_polygon(self, 0, top + s(1), w, top + self._row - s(1), s(6), fill=HOVER, outline=HOVER)
            self.create_text(pad, middle, text=shorten(row.label, f.body, label_w), anchor="w",
                             fill=TEXT if bars else TEXT_2, font=f.body)
            if row.note:
                self.create_text(right_note, middle, text=row.note, anchor="e", fill=TEXT_3, font=f.meta)
            self.create_text(right_value, middle, text=row.value, anchor="e", fill=TEXT, font=f.body_strong)
            if row.fraction is not None and bar_x2 - bar_x1 > s(24):
                length = (bar_x2 - bar_x1) * max(0.0, min(row.fraction, 1.0)) * self._grow
                y1, y2 = middle - s(2), middle + s(2)
                rounded_polygon(self, bar_x1, y1, bar_x2, y2, s(2), fill=TRACK, outline=TRACK)
                if length > 0:
                    rounded_polygon(self, bar_x1, y1, bar_x1 + max(length, s(4)), y2, s(2), fill=BAR, outline=BAR)


@dataclass
class Column:
    title: str
    width: int = 0         # px at 96 dpi; 0 = takes the remaining space
    align: str = "w"       # "w" text, "e" numbers
    hide_below: int = 0    # hidden when the table is narrower than this (96 dpi px)


@dataclass
class TableRow:
    cells: tuple[str, ...]
    group: bool = False    # a day header: cells = (title, summary)
    tag: str = ""          # short green marker after the last cell, e.g. "Best"


class Table(Canvas):
    """Virtualised activity table: sticky column header, day groups, hover rows, smooth scrolling.

    First column reads as primary text, the last as the emphasised number, the rest as secondary.
    """

    def __init__(self, parent: tk.Misc, scale: Scale, fonts: Fonts, columns: list[Column],
                 empty_text: str, scrollable: bool = True) -> None:
        super().__init__(parent)
        self._scale, self._fonts, self._columns = scale, fonts, columns
        self._empty, self._scrollable = empty_text, scrollable
        self._rows: list[TableRow] = []
        self._tops: list[int] = []
        self._head, self._row_h, self._group_h = scale(30), scale(34), scale(36)
        self._offset = self._target = 0.0
        self._hover: Optional[int] = None
        self._drag_from: Optional[tuple[int, float]] = None
        self._reveal = 1.0
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _e: self._set_hover(None))
        if scrollable:
            self.bind("<MouseWheel>", lambda e: self.scroll(-e.delta / 120 * self._row_h * 3))
            self.bind("<ButtonPress-1>", self._press)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag_from", None))

    def set_rows(self, rows: list[TableRow]) -> None:
        fresh = not self._rows and rows
        self._rows = rows
        self._tops, y = [], 0
        for row in rows:
            self._tops.append(y)
            y += self._group_h if row.group else self._row_h
        self._content = y
        if not self._scrollable:
            self.configure(height=self._head + max(y, self._row_h))
        self._offset = self._target = self._clamp(self._offset)
        if fresh:
            animate(self, "reveal", 450, lambda t: (setattr(self, "_reveal", t), self._draw()))
        else:
            self._draw()

    def scroll(self, pixels: float) -> None:
        start, self._target = self._offset, self._clamp(self._target + pixels)
        target = self._target
        animate(self, "scroll", 150, lambda t: (setattr(self, "_offset", start + (target - start) * t), self._draw()))

    def page(self) -> int:
        return max(self.winfo_height() - self._head - self._row_h, self._row_h)

    def _view(self) -> int:
        return self.winfo_height() - self._head

    def _clamp(self, offset: float) -> float:
        return min(max(offset, 0), max(getattr(self, "_content", 0) - self._view(), 0))

    def _row_at(self, y: float) -> Optional[int]:
        y = y - self._head + self._offset
        for index in range(len(self._rows) - 1, -1, -1):
            if self._tops[index] <= y:
                height = self._group_h if self._rows[index].group else self._row_h
                return index if y < self._tops[index] + height and not self._rows[index].group else None
        return None

    def _motion(self, event: tk.Event) -> None:
        self._set_hover(self._row_at(event.y) if event.y > self._head else None)

    def _set_hover(self, index: Optional[int]) -> None:
        if index != self._hover:
            self._hover = index
            self._draw()

    def _thumb(self) -> Optional[tuple[float, float]]:
        view, content = self._view(), getattr(self, "_content", 0)
        if not self._scrollable or content <= view or view <= 1:
            return None
        size = max(view * view / content, self._scale(32))
        top = self._head + (view - size) * self._offset / (content - view)
        return top, top + size

    def _press(self, event: tk.Event) -> None:
        thumb = self._thumb()
        if thumb and event.x >= self.winfo_width() - self._scale(12) and thumb[0] <= event.y <= thumb[1]:
            self._drag_from = (event.y, self._offset)

    def _drag(self, event: tk.Event) -> None:
        thumb = self._thumb()
        if not self._drag_from or not thumb:
            return
        start_y, start_offset = self._drag_from
        track = self._view() - (thumb[1] - thumb[0])
        if track > 0:
            self._offset = self._target = self._clamp(
                start_offset + (event.y - start_y) * (self._content - self._view()) / track)
            self._draw()

    def _layout(self, width: int) -> list[tuple[Column, int, float, float]]:
        """Visible columns with their x extent [x1, x2] and index into the row cells."""
        s, gap, pad = self._scale, self._scale(16), self._scale(10)
        shown = [(c, i) for i, c in enumerate(self._columns) if width >= s(c.hide_below)]
        fixed = sum(s(c.width) for c, _ in shown if c.width) + gap * (len(shown) - 1)
        flexible = max(width - 2 * pad - fixed - s(10), s(60))
        flex_count = sum(1 for c, _ in shown if not c.width) or 1
        out, x = [], pad
        for column, index in shown:
            span = s(column.width) if column.width else flexible / flex_count
            out.append((column, index, x, x + span))
            x += span + gap
        return out

    def _draw(self) -> None:
        self.delete("all")
        s, f = self._scale, self._fonts
        width, height = self.winfo_width(), self.winfo_height()
        columns = self._layout(width)
        right_edge = width - s(10)
        if not self._rows:
            self.create_text(columns[0][2] if columns else s(10), self._head + self._row_h / 2,
                             text=self._empty, anchor="w", fill=TEXT_3, font=f.body)
        view_top, view_bottom = self._offset, self._offset + height
        last_column = len(self._columns) - 1
        for index, row in enumerate(self._rows):
            row_h = self._group_h if row.group else self._row_h
            if self._tops[index] + row_h < view_top or self._tops[index] > view_bottom:
                continue
            delay = min(index, 10) * 0.035
            t = max(0.0, min((self._reveal - delay) / max(1 - delay, 0.01), 1.0))
            top = self._head + self._tops[index] - self._offset + (1 - t) * s(8)
            middle = top + row_h / 2
            fade = lambda color: mix(BACKGROUND, color, t)  # noqa: E731
            if row.group:
                self.create_text(columns[0][2], top + row_h - s(11), text=row.cells[0], anchor="w",
                                 fill=fade(TEXT_2), font=f.meta_strong)
                self.create_text(columns[-1][3], top + row_h - s(11), text=row.cells[1], anchor="e",
                                 fill=fade(TEXT_3), font=f.meta)
                continue
            if index == self._hover:
                rounded_polygon(self, s(2), top + s(1), right_edge, top + row_h - s(1), s(6), fill=HOVER, outline=HOVER)
            for column, cell, x1, x2 in columns:
                text = row.cells[cell]
                primary, emphasised = cell == 0, cell == last_column
                font = f.body_strong if emphasised else f.body
                ink = TEXT if primary or emphasised else TEXT_2
                if emphasised and row.tag:
                    self.create_text(x2 - f.body_strong.measure(text) - s(8), middle, text=row.tag, anchor="e",
                                     fill=fade(ACCENT_TEXT), font=f.meta_strong)
                self.create_text(x1 if column.align == "w" else x2, middle, text=shorten(text, font, x2 - x1),
                                 anchor=column.align, fill=fade(ink), font=font)
        # sticky header, drawn last so scrolled rows slide underneath it
        self.create_rectangle(0, 0, width, self._head, fill=self["bg"], outline="")
        for column, _index, x1, x2 in columns:
            self.create_text(x1 if column.align == "w" else x2, self._head / 2, text=column.title,
                             anchor=column.align, fill=TEXT_3, font=f.meta)
        self.create_line(0, self._head - 1, right_edge, self._head - 1, fill=BORDER)
        thumb = self._thumb()
        if thumb:
            rounded_polygon(self, width - s(6), thumb[0], width - s(3), thumb[1], s(2), fill=BORDER_STRONG,
                            outline=BORDER_STRONG)


@dataclass
class HeatPoint:
    x: float  # map image pixels
    y: float
    count: int
    place: str


def _hex_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


class HeatMap(Canvas):
    """Island map with a smooth chest-density overlay and hover tooltips.

    The map is dimmed and desaturated so the overlay carries the colour; density is a Gaussian splat of
    every chest location, coloured on a single warm ramp (brighter = more chests) and alpha-blended in.
    """

    def __init__(self, parent: tk.Misc, scale: Scale, fonts: Fonts) -> None:
        super().__init__(parent)
        self._scale, self._fonts = scale, fonts
        self._source: Optional[tk.PhotoImage] = None
        self._image: Optional[tk.PhotoImage] = None
        self._base: list[tuple[int, int, int]] = []  # dimmed map pixels for the current subsample
        self._size = (0, 0)
        self._crop = (0, 0, 1, 1)
        self._factor = 0.0
        self._points: list[HeatPoint] = []
        self._composed_for: Optional[tuple] = None
        self._message = ("Loading map…", "")
        self._empty: Optional[tuple[str, str]] = None
        self._resize_job: Optional[str] = None
        self.bind("<Configure>", self._schedule)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _e: self.delete("tip"))

    def set_image(self, path: Optional[Path], crop: tuple[int, int, int, int],
                  message: tuple[str, str] = ("", "")) -> None:
        self._message = message
        self._source = None
        if path and not message[0]:
            try:
                self._source = tk.PhotoImage(file=str(path))
            except tk.TclError:
                self._message = ("Couldn't open the map image.", "Top locations still work.")
        self._crop, self._factor, self._composed_for, self._size = crop, 0.0, None, (0, 0)
        self._draw()

    def set_points(self, points: list[HeatPoint], empty: tuple[str, str]) -> None:
        self._empty = empty
        if [(p.x, p.y, p.count) for p in points] != [(p.x, p.y, p.count) for p in self._points]:
            self._points = points
            self._composed_for = None
            self._draw()

    def _schedule(self, _event: object) -> None:
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(100, self._draw)

    def _geometry(self) -> tuple[float, float, float]:
        x1, y1, x2, y2 = self._crop
        k = self._factor or 1
        return (self.winfo_width() - (x2 - x1) / k) / 2, (self.winfo_height() - (y2 - y1) / k) / 2, k

    def _centre_text(self, title: str, detail: str) -> None:
        w, h, f = self.winfo_width(), self.winfo_height(), self._fonts
        self.create_text(w / 2, h / 2 - self._scale(10), text=title, fill=TEXT_2, font=f.body_strong, tags="dot")
        self.create_text(w / 2, h / 2 + self._scale(10), text=detail, fill=TEXT_3, font=f.meta,
                         width=max(w - self._scale(48), 50), justify="center", tags="dot")

    def _draw(self) -> None:
        self._resize_job = None
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if self._source is None:
            self._centre_text(*self._message)
            return
        side = max(min(w, h), 16)
        if abs(self._size[0] - side) > 2:
            self._load_base(side)
        if self._composed_for != (self._size, len(self._points)):
            self._compose()
            self._composed_for = (self._size, len(self._points))
        left, top, _ = self._geometry()
        self.create_image(left, top, image=self._image, anchor="nw")
        if not self._points and self._empty:
            self._centre_text(*self._empty)

    def _source_pixels(self) -> list[list[tuple[int, int, int]]]:
        """The cropped map at half resolution, read once; Tk itself can only scale by whole factors."""
        if getattr(self, "_half_for", None) is not self._source:
            x1, y1, x2, y2 = self._crop
            half = tk.PhotoImage()
            half.tk.call(half, "copy", self._source, "-from", x1, y1, x2, y2, "-subsample", 2, 2)
            self._half = [[(int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)) for c in self.tk.splitlist(row)]
                          for row in self.tk.splitlist(half.tk.call(half, "data"))]
            self._half_for = self._source
        return self._half

    def _load_base(self, side: int) -> None:
        """Resample the map to exactly `side` px (2x2 supersampled) and tone it down slightly so the
        semi-transparent overlay stays readable on top of it."""
        source = self._source_pixels()
        src_h, src_w = len(source), len(source[0])
        step = src_w / side
        cols = [(min(int((x + 0.25) * step), src_w - 1), min(int((x + 0.75) * step), src_w - 1)) for x in range(side)]
        bg_r, bg_g, bg_b = (c * 0.28 for c in _hex_rgb(BACKGROUND))
        keep, dim = 0.6 * 0.72 / 4, 0.72 / 4  # colour share and brightness, folded with the /4 sample average
        base = []
        append = base.append
        for y in range(side):
            top = source[min(int((y + 0.25) * step), src_h - 1)]
            bottom = source[min(int((y + 0.75) * step), src_h - 1)]
            for c0, c1 in cols:
                p, q, u, v = top[c0], top[c1], bottom[c0], bottom[c1]
                r, g, b = p[0] + q[0] + u[0] + v[0], p[1] + q[1] + u[1] + v[1], p[2] + q[2] + u[2] + v[2]
                grey = (0.3 * r + 0.59 * g + 0.11 * b) * 0.4 * dim
                append((int(r * keep + grey + bg_r), int(g * keep + grey + bg_g), int(b * keep + grey + bg_b)))
        self._base = base
        self._size = (side, side)
        self._factor = (self._crop[2] - self._crop[0]) / side
        self._image = tk.PhotoImage(width=side, height=side)

    def _compose(self) -> None:
        w, h = self._size
        if not w or not h:
            return
        density = [0.0] * (w * h)
        sigma = self._scale(5)
        reach = int(sigma * 2.5)
        kernel = [(dx, dy, math.exp(-(dx * dx + dy * dy) / (2 * sigma * sigma)))
                  for dy in range(-reach, reach + 1) for dx in range(-reach, reach + 1)
                  if dx * dx + dy * dy <= reach * reach]
        k, (cx, cy) = self._factor, self._crop[:2]
        binned: dict[tuple[int, int], int] = {}
        for point in self._points:  # merge points that land on the same pixel before splatting
            key = (round((point.x - cx) / k), round((point.y - cy) / k))
            binned[key] = binned.get(key, 0) + point.count
        for (px, py), count in binned.items():
            for dx, dy, weight in kernel:
                x, y = px + dx, py + dy
                if 0 <= x < w and 0 <= y < h:
                    density[y * w + x] += count * weight
        peak = max(density, default=0) or 1
        ramp = [_hex_rgb(c) for c in HEAT]
        out = ["#%02x%02x%02x" % c for c in self._base]
        for index, value in enumerate(density):
            if value <= 0:
                continue
            v = math.sqrt(value / peak)  # keep lightly visited areas visible next to the hottest spot
            if v < 0.05:
                continue
            # Translucent throughout: soft edges fade in, and even the hottest spot lets the map show through.
            alpha = min(1.0, (v - 0.05) / 0.2) * (0.5 + 0.35 * v)
            pos = v * (len(ramp) - 1)
            lo = ramp[int(pos)]
            hi = ramp[min(int(pos) + 1, len(ramp) - 1)]
            t = pos - int(pos)
            base = self._base[index]
            out[index] = "#%02x%02x%02x" % tuple(
                round(b + ((l + (u - l) * t) - b) * alpha) for b, l, u in zip(base, lo, hi))
        rows = (" ".join(out[y * w:(y + 1) * w]) for y in range(h))
        self._image.put(" ".join("{" + row + "}" for row in rows))

    def _motion(self, event: tk.Event) -> None:
        self.delete("tip")
        if self._source is None or not self._points:
            return
        s, f = self._scale, self._fonts
        left, top, k = self._geometry()
        radius = s(14)
        near = [p for p in self._points
                if math.hypot(left + (p.x - self._crop[0]) / k - event.x,
                              top + (p.y - self._crop[1]) / k - event.y) <= radius]
        if not near:
            return
        total = sum(p.count for p in near)
        place = max(near, key=lambda p: p.count).place
        text = f"{total:,} chest{'s' if total != 1 else ''} · {place}"
        width, height = f.meta.measure(text) + s(16), s(26)
        x = min(max(event.x - width / 2, s(4)), self.winfo_width() - width - s(4))
        y = event.y - height - s(10)
        if y < s(4):
            y = event.y + s(14)
        rounded_polygon(self, x, y, x + width, y + height, s(6), fill=TOOLTIP, outline=BORDER_STRONG, tags="tip")
        self.create_text(x + width / 2, y + height / 2, text=text, fill=TEXT, font=f.meta, tags="tip")


class StatusDot(Canvas):
    COLORS = {"ok": GREEN, "busy": BLUE, "error": RED}

    def __init__(self, parent: tk.Misc, scale: Scale) -> None:
        self._size = scale(7)
        super().__init__(parent, width=self._size, height=self._size)
        self._state = ""
        self.set("ok")

    def set(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.delete("all")
        dot = self.create_oval(0, 0, self._size, self._size, fill=self.COLORS[state], outline="")
        if state == "busy":  # a slow breathing pulse while replays are read
            def pulse(phase: int = 0) -> None:
                if self._state != "busy":
                    return
                t = (math.sin(phase / 8) + 1) / 2
                self.itemconfigure(dot, fill=mix(SIDEBAR, BLUE, 0.45 + 0.55 * t))
                self.after(50, pulse, phase + 1)

            pulse()


class Toaster:
    """Notifications that slide in at the bottom-right of `host`, one at a time."""

    def __init__(self, host: tk.Misc, scale: Scale, fonts: Fonts) -> None:
        self._host, self._scale, self._fonts = host, scale, fonts
        self._queue: list[tuple[str, str]] = []
        self._showing = False

    def show(self, title: str, body: str) -> None:
        self._queue.append((title, body))
        if not self._showing:
            self._next()

    def _next(self) -> None:
        if not self._queue:
            self._showing = False
            return
        self._showing = True
        title, body = self._queue.pop(0)
        s, f = self._scale, self._fonts
        width = max(f.body_strong.measure(title), f.meta.measure(body)) + s(48)
        height = s(56)
        toast = Canvas(self._host, width=width, height=height)
        rounded_polygon(toast, 1, 1, width - 1, height - 1, s(10), fill=SURFACE, outline=BORDER_STRONG)
        toast.create_oval(s(16), s(18), s(23), s(25), fill=ACCENT, outline="")
        toast.create_text(s(32), s(21), text=title, anchor="w", fill=TEXT, font=f.body_strong)
        toast.create_text(s(32), s(38), text=body, anchor="w", fill=TEXT_2, font=f.meta)
        margin = s(16)
        left = {"done": False}

        def slide(t: float, out: bool = False) -> None:
            shift = (t if out else 1 - t) * (height + margin)
            toast.place(relx=1, rely=1, x=-margin, y=-margin + shift, anchor="se")

        def leave() -> None:
            if left["done"]:
                return
            left["done"] = True
            animate(toast, "slide", 220, lambda t: slide(t, out=True), lambda: (toast.destroy(), self._next()))

        toast.configure(cursor="hand2")
        toast.bind("<Button-1>", lambda _e: leave())
        animate(toast, "slide", 280, slide, lambda: toast.after(5000, leave))
