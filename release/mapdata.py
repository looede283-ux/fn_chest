from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CACHE = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ChestCounter" / "map"
MAP_API = "https://fortnite-api.com/v1/map"
REFRESH_SECONDS = 12 * 60 * 60
TIMEOUT = 20

MAP_SIZE = 2048
MAP_ORIGIN = (976.0, 1016.0)
MAP_SCALE = 0.0066
REFERENCE_POI = ("Athena.Location.POI.Generic.11", 26307.0, 89235.0)
MAP_CROP = (470, 540, 1730, 1800)  # the island, in map pixels

NAMED_RADIUS = 20000  # 200 m: inside this you are "at" a POI, beyond it "near" one


@dataclass
class MapData:
    pois: list[tuple[str, float, float]] = field(default_factory=list)
    image: Optional[Path] = None
    calibrated: bool = False

    def place(self, x: float, y: float) -> str:
        if not self.pois:
            return "Unknown area"
        name, distance = min(((n, math.hypot(px - x, py - y)) for n, px, py in self.pois),
                             key=lambda item: item[1])
        return name if distance <= NAMED_RADIUS else f"Near {name}"

    def to_pixel(self, x: float, y: float) -> tuple[float, float]:
        return MAP_ORIGIN[0] + x * MAP_SCALE, MAP_ORIGIN[1] + y * MAP_SCALE


def load(refresh: bool = True) -> MapData:
    """Return cached map data, refreshing it from fortnite-api.com when stale."""
    info_file, image_file = CACHE / "map.json", CACHE / "map.png"
    stale = not info_file.exists() or time.time() - info_file.stat().st_mtime > REFRESH_SECONDS
    if refresh and stale:
        try:
            _download(info_file, image_file)
        except Exception:
            pass
    try:
        data = json.loads(info_file.read_text(encoding="utf-8"))["data"]
    except (OSError, ValueError, KeyError):
        return MapData()
    pois = [(p["name"], float(p["location"]["x"]), float(p["location"]["y"]))
            for p in data.get("pois", []) if p.get("name")]
    reference = next((p for p in data.get("pois", []) if p.get("id") == REFERENCE_POI[0]), None)
    calibrated = bool(reference) and math.hypot(reference["location"]["x"] - REFERENCE_POI[1],
                                                reference["location"]["y"] - REFERENCE_POI[2]) < 500
    return MapData(pois, image_file if image_file.exists() else None, calibrated)


def _download(info_file: Path, image_file: Path) -> None:
    raw = _read(MAP_API)
    data = json.loads(raw.decode("utf-8"))["data"]
    CACHE.mkdir(parents=True, exist_ok=True)
    old = info_file.read_bytes() if info_file.exists() else b""
    if old != raw or not image_file.exists():
        image = _read(data["images"]["blank"])
        tmp = image_file.with_suffix(".tmp")
        tmp.write_bytes(image)
        os.replace(tmp, image_file)
    info_file.write_bytes(raw)


def _read(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "chest-counter"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()
