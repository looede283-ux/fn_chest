from __future__ import annotations

import ctypes
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from typing import Callable, Optional

BACKGROUND = "#09090b"
BORDER = "#27272a"
FOREGROUND = "#fafafa"
MUTED = "#a1a1aa"
HOVER = "#18181b"
ACCENT = "#27272a"
PRIMARY = "#fafafa"
PRIMARY_TEXT = "#18181b"
PRIMARY_HOVER = "#d4d4d8"
SCROLL = "#3f3f46"
BADGE_TEXT = "#86efac"
BADGE_FILL = "#052e16"
BADGE_BORDER = "#14532d"
GREEN = "#22c55e"
AMBER = "#f59e0b"
RED = "#ef4444"

FONT_FAMILIES = ("Inter", "Geist", "Segoe UI Variable Text", "Segoe UI")


class Scale:
    def __init__(self, root: tk.Misc) -> None:
        self.factor = root.winfo_fpixels("1i") / 96.0

    def __call__(self, pixels: float) -> int:
        return max(1, round(pixels * self.factor))


class Fonts:
    def __init__(self, root: tk.Misc, scale: Scale) -> None:
        available = set(tkfont.families(root))
        family = next((name for name in FONT_FAMILIES if name in available), "Segoe UI")

        def make(pixels: int, weight: str = "normal") -> tkfont.Font:
            return tkfont.Font(root=root, family=family, size=-scale(pixels), weight=weight)

        self.small = make(12)
        self.small_bold = make(12, "bold")
        self.body = make(14)
        self.body_bold = make(14, "bold")
        self.title = make(16, "bold")
        self.heading = make(20, "bold")
        self.display = make(60, "bold")


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
        for attribute, value in ((20, 1), (34, 0x002A2727), (35, 0x000B0909)):
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(handle), attribute,
                                      ctypes.byref(ctypes.c_int(value)), 4)
    except (AttributeError, OSError):
        pass


def rounded_polygon(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                    radius: float, **options) -> int:
    points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
              x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
              x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=16, **options)


def shorten(text: str, font: tkfont.Font, width: int) -> str:
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


class Card(tk.Canvas):
    def __init__(self, parent: tk.Misc, scale: Scale, padding: int = 24, auto_height: bool = False) -> None:
        super().__init__(parent, bg=BACKGROUND, highlightthickness=0, bd=0)
        self._scale = scale
        self._padding = scale(padding)
        self._auto_height = auto_height
        self.body = tk.Frame(self, bg=BACKGROUND)
        self._window = self.create_window(self._padding, self._padding, anchor="nw", window=self.body)
        self.bind("<Configure>", self._redraw)
        if auto_height:
            self.body.bind("<Configure>", self._fit_height)

    def _fit_height(self, _event: tk.Event) -> None:
        self.configure(height=self.body.winfo_reqheight() + 2 * self._padding)

    def _redraw(self, event: tk.Event) -> None:
        self.delete("frame")
        rounded_polygon(self, 1, 1, event.width - 1, event.height - 1, self._scale(10),
                        fill=BACKGROUND, outline=BORDER, width=1, tags="frame")
        self.tag_lower("frame")
        inner_width = max(event.width - 2 * self._padding, 1)
        options = {"width": inner_width}
        if not self._auto_height:
            options["height"] = max(event.height - 2 * self._padding, 1)
        self.itemconfigure(self._window, **options)


class Button(tk.Canvas):
    STYLES = {
        "primary": {"fill": PRIMARY, "hover": PRIMARY_HOVER, "text": PRIMARY_TEXT, "border": PRIMARY},
        "outline": {"fill": BACKGROUND, "hover": ACCENT, "text": FOREGROUND, "border": BORDER},
    }

    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None], scale: Scale,
                 fonts: Fonts, variant: str = "outline") -> None:
        self._text = text
        self._command = command
        self._scale = scale
        self._font = fonts.body_bold if variant == "primary" else fonts.body
        self._style = self.STYLES[variant]
        self._hover = False
        width = self._font.measure(text) + 2 * scale(16)
        super().__init__(parent, width=width, height=scale(36), bg=BACKGROUND,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._release)

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self._draw()

    def _release(self, event: tk.Event) -> None:
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self._command()

    def _draw(self) -> None:
        self.delete("all")
        fill = self._style["hover"] if self._hover else self._style["fill"]
        rounded_polygon(self, 1, 1, self.winfo_width() - 1, self.winfo_height() - 1, self._scale(6),
                        fill=fill, outline=self._style["border"], width=1)
        self.create_text(self.winfo_width() / 2, self.winfo_height() / 2, text=self._text,
                         fill=self._style["text"], font=self._font)


@dataclass
class HistoryRow:
    title: str
    subtitle: str
    badge: str


class HistoryList(tk.Canvas):
    def __init__(self, parent: tk.Misc, scale: Scale, fonts: Fonts, empty_text: str) -> None:
        super().__init__(parent, bg=BACKGROUND, highlightthickness=0, bd=0)
        self._scale = scale
        self._fonts = fonts
        self._empty_text = empty_text
        self._rows: list[HistoryRow] = []
        self._row_height = scale(60)
        self._offset = 0
        self._hover: Optional[int] = None
        self._drag_from: Optional[tuple[int, int]] = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag_from", None))

    def set_rows(self, rows: list[HistoryRow]) -> None:
        self._rows = rows
        self._offset = self._clamp(self._offset)
        self._draw()

    def _content_height(self) -> int:
        return len(self._rows) * self._row_height

    def _clamp(self, offset: float) -> int:
        limit = max(self._content_height() - self.winfo_height(), 0)
        return int(min(max(offset, 0), limit))

    def _wheel(self, event: tk.Event) -> None:
        self._offset = self._clamp(self._offset - event.delta / 120 * self._row_height * 0.7)
        self._draw()

    def _motion(self, event: tk.Event) -> None:
        index = (event.y + self._offset) // self._row_height
        hover = index if 0 <= index < len(self._rows) else None
        if hover != self._hover:
            self._hover = hover
            self._draw()

    def _leave(self, _event: tk.Event) -> None:
        if self._hover is not None:
            self._hover = None
            self._draw()

    def _thumb(self) -> Optional[tuple[int, int]]:
        height = self.winfo_height()
        content = self._content_height()
        if content <= height or height <= 1:
            return None
        size = max(int(height * height / content), self._scale(32))
        top = int((height - size) * self._offset / (content - height))
        return top, top + size

    def _press(self, event: tk.Event) -> None:
        thumb = self._thumb()
        if thumb and event.x >= self.winfo_width() - self._scale(14) and thumb[0] <= event.y <= thumb[1]:
            self._drag_from = (event.y, self._offset)

    def _drag(self, event: tk.Event) -> None:
        thumb = self._thumb()
        if not self._drag_from or not thumb:
            return
        start_y, start_offset = self._drag_from
        track = self.winfo_height() - (thumb[1] - thumb[0])
        if track > 0:
            ratio = (self._content_height() - self.winfo_height()) / track
            self._offset = self._clamp(start_offset + (event.y - start_y) * ratio)
            self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if not self._rows:
            self.create_text(width / 2, height / 2, text=self._empty_text, fill=MUTED,
                             font=self._fonts.body)
            return
        s = self._scale
        first = self._offset // self._row_height
        last = min(len(self._rows), (self._offset + height) // self._row_height + 2)
        text_width = width - s(110)
        for index in range(first, last):
            row = self._rows[index]
            top = index * self._row_height - self._offset
            middle = top + self._row_height / 2
            if index == self._hover:
                rounded_polygon(self, 0, top + s(4), width - s(10), top + self._row_height - s(4),
                                s(6), fill=HOVER, outline=HOVER)
            self.create_text(s(12), middle - s(10), anchor="w", text=row.title,
                             fill=FOREGROUND, font=self._fonts.body_bold)
            self.create_text(s(12), middle + s(11), anchor="w",
                             text=shorten(row.subtitle, self._fonts.small, text_width),
                             fill=MUTED, font=self._fonts.small)
            badge_width = self._fonts.small_bold.measure(row.badge) + 2 * s(12)
            right = width - s(22)
            rounded_polygon(self, right - badge_width, middle - s(12), right, middle + s(12), s(12),
                            fill=BADGE_FILL, outline=BADGE_BORDER, width=1)
            self.create_text(right - badge_width / 2, middle, text=row.badge, fill=BADGE_TEXT,
                             font=self._fonts.small_bold)
            if index < len(self._rows) - 1:
                self.create_line(s(12), top + self._row_height - 1, width - s(22),
                                 top + self._row_height - 1, fill=BORDER)
        thumb = self._thumb()
        if thumb:
            rounded_polygon(self, width - s(7), thumb[0], width - s(3), thumb[1], s(2),
                            fill=SCROLL, outline=SCROLL)


class StatusDot(tk.Canvas):
    COLORS = {"ok": GREEN, "busy": AMBER, "error": RED}

    def __init__(self, parent: tk.Misc, scale: Scale) -> None:
        size = scale(10)
        super().__init__(parent, width=size, height=size, bg=BACKGROUND, highlightthickness=0, bd=0)
        self._size = size
        self.set("ok")

    def set(self, state: str) -> None:
        self.delete("all")
        self.create_oval(1, 1, self._size - 1, self._size - 1, fill=self.COLORS[state], outline="")
