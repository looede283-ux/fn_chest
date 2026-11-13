from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from count_store import ReplayEntry

TOKEN_ODDS = 137_500
TOKEN_CHANCE = 1 / TOKEN_ODDS

KIND_LABELS = {
    "TieredChest": "Chest",
    "AlwaysSpawnNormalChest": "Guaranteed chest",
    "AlwaysSpawnRareChest": "Rare chest",
    "BuriedChest": "Buried chest",
    "RelicChest": "Relic chest",
    "DelilahRelicChest": "Relic chest",
    "SpriteBootyChest": "Booty chest",
}
HEAT_CELL = 1000  # world units (10 m) per heatmap cell
RECENT = 10


def milestone_at(total: int) -> int:
    """Highest milestone reached: 100, 250, 500, then every 1,000."""
    if total >= 1000:
        return total // 1000 * 1000
    return max((m for m in (100, 250, 500) if m <= total), default=0)


def next_milestone(total: int) -> int:
    return next(m for m in (100, 250, 500) if m > total) if total < 500 else (total // 1000 + 1) * 1000


def local_date(entry: ReplayEntry) -> Optional[date]:
    if not entry.match_time:
        return None
    return datetime.fromisoformat(entry.match_time).astimezone().date()


@dataclass
class Records:
    best_match: Optional[ReplayEntry] = None
    best_day: Optional[tuple[date, int]] = None
    fastest: Optional[ReplayEntry] = None
    streak: int = 0
    best_streak: int = 0


@dataclass
class Summary:
    total: int
    matches: int = 0
    average: float = 0.0
    per_minute: float = 0.0
    per_hour: float = 0.0
    today: int = 0
    today_matches: int = 0
    recent_average: float = 0.0  # last RECENT matches, to compare against the overall average
    kinds: list[tuple[str, int]] = field(default_factory=list)
    detailed: int = 0
    squad_total: int = 0
    squad_own: int = 0  # your own chests in those same squad matches
    squad_matches: int = 0
    days: int = 0
    records: Records = field(default_factory=Records)


def per_minute(entry: ReplayEntry) -> float:
    return entry.chests / (entry.duration_s / 60) if entry.duration_s >= 60 else 0.0


def summarize(entries: list[ReplayEntry], total: int, today: Optional[date] = None) -> Summary:
    """`entries` newest first, as returned by CountStore.history()."""
    today = today or date.today()
    summary = Summary(total=total, matches=len(entries))
    if not entries:
        return summary
    counts = [e.chests for e in entries]
    summary.average = sum(counts) / len(counts)
    recent = counts[:RECENT]
    summary.recent_average = sum(recent) / len(recent)
    timed = [e for e in entries if e.duration_s >= 60]
    minutes = sum(e.duration_s for e in timed) / 60
    if minutes:
        summary.per_minute = sum(e.chests for e in timed) / minutes
        summary.per_hour = summary.per_minute * 60

    kinds: Counter[str] = Counter()
    for entry in entries:
        if entry.opened:
            summary.detailed += 1
            kinds.update(KIND_LABELS.get(row[1], "Other") for row in entry.opened)
        if entry.squad >= 0 and entry.squad_size > 1:
            summary.squad_matches += 1
            summary.squad_total += entry.squad
            summary.squad_own += entry.chests
    summary.kinds = kinds.most_common()

    by_day: dict[date, int] = defaultdict(int)
    for entry in entries:
        day = local_date(entry)
        if day:
            by_day[day] += entry.chests
            summary.today_matches += day == today
    summary.today = by_day.get(today, 0)
    summary.days = len(by_day)

    records = summary.records
    records.best_match = max(entries, key=lambda e: e.chests)
    if by_day:
        records.best_day = max(by_day.items(), key=lambda item: item[1])
    long_enough = [e for e in timed if e.duration_s >= 5 * 60]
    if long_enough:
        records.fastest = max(long_enough, key=per_minute)
    records.best_streak, records.streak = _streaks(set(by_day), today)
    return summary


def _streaks(days: set[date], today: date) -> tuple[int, int]:
    best = run = 0
    previous: Optional[date] = None
    for day in sorted(days):
        run = run + 1 if previous and day - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    current = 0
    day = today if today in days else today - timedelta(days=1)
    while day in days:
        current += 1
        day -= timedelta(days=1)
    return best, current


@dataclass
class TokenOdds:
    chests: int
    chance: float

    def chests_for(self, probability: float) -> int:
        return math.ceil(math.log(1 - probability) / math.log(1 - TOKEN_CHANCE))


def token_odds(total: int) -> TokenOdds:
    return TokenOdds(total, 1 - (1 - TOKEN_CHANCE) ** total)


@dataclass
class Spot:
    name: str
    chests: int
    matches: int

    @property
    def per_match(self) -> float:
        return self.chests / self.matches if self.matches else 0.0


def spots(entries: list[ReplayEntry], place) -> list[Spot]:
    """Group your chests by named location; `place(x, y)` names a world position."""
    chests: Counter[str] = Counter()
    matches: Counter[str] = Counter()
    for entry in entries:
        here = Counter(place(row[2], row[3]) for row in entry.opened if row[2] is not None)
        chests.update(here)
        matches.update(here.keys())
    return sorted((Spot(name, count, matches[name]) for name, count in chests.items()),
                  key=lambda s: s.chests, reverse=True)


def heat(entries: list[ReplayEntry], cell: int = HEAT_CELL) -> dict[tuple[int, int], int]:
    cells: Counter[tuple[int, int]] = Counter()
    for entry in entries:
        for row in entry.opened:
            if row[2] is not None:
                cells[(round(row[2] / cell), round(row[3] / cell))] += 1
    return dict(cells)
