"""When the games are soft: table looseness by hour and weekday, and when the
known fish are actually at your tables.

Two measures, because each answers a different objection.

*Looseness* is the share of opponents who voluntarily put money in preflop,
per hand, averaged over the hands you played in that hour. It needs no list
of names, so it cannot be an artefact of which players you happen to have a
long history with. Its per-hand SD is ~24 points, so an hour with 500 hands
carries a standard error near 1 point.

*Presence* is, for one fish, the share of your hands in which they were
seated -- only over the span between the first and last time you saw them,
so a player who quit in April is not "absent" from every hour since. It
answers "when is *this* player around", which looseness cannot.

Both are conditioned on the hours you played: an hour you never play has no
row, not a zero. Timestamps are the hand history's own (Europe/Rome).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .core import NL5

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MIN_CELL = 50          # hands before an hour or a cell is reported

_LOOSE_SQL = """
WITH pf AS (
    SELECT hand_id, name,
           max(CASE WHEN verb IN ('Calls', 'Raises to') THEN 1 ELSE 0 END) AS vpip
    FROM actions
    WHERE street = 'PRE-FLOP' AND NOT is_hero AND NOT dead
      AND verb NOT IN ('Posts SB', 'Posts BB', 'Posts Ante')
    GROUP BY hand_id, name)
SELECT h.hand_id, h.played_at, h.hero_net * 1.0 / h.bb,
       (SELECT avg(vpip) FROM pf WHERE pf.hand_id = h.hand_id) AS pool_vpip
FROM hands h
WHERE h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
"""

_PRESENCE_SQL = """
SELECT s.name, h.played_at
FROM seats s JOIN hands h USING (hand_id)
WHERE NOT s.is_hero AND s.name IN ({names})
  AND h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
"""


@dataclass(slots=True)
class Cell:
    n: int = 0
    total: float = 0.0
    sq: float = 0.0
    net: float = 0.0

    def add(self, v: float, net: float) -> None:
        self.n += 1
        self.total += v
        self.sq += v * v
        self.net += net

    @property
    def mean(self) -> float | None:
        return self.total / self.n if self.n >= MIN_CELL else None

    @property
    def se(self) -> float | None:
        if self.n < MIN_CELL:
            return None
        var = self.sq / self.n - (self.total / self.n) ** 2
        return (max(var, 0.0) / self.n) ** 0.5

    @property
    def bb100(self) -> float | None:
        return 100 * self.net / self.n if self.n >= MIN_CELL else None


@dataclass(slots=True)
class Presence:
    name: str
    hands: int
    first: datetime
    last: datetime
    by_hour: list[tuple[int, int, int]]      # (hour, hands with them, your hands that hour in their span)
    by_day: list[tuple[int, int, int]]

    def top_hours(self, k: int = 3) -> list[tuple[int, float, int]]:
        """(hour, share of your hands they were in, n) for the hours with the
        most shared hands, share reported only where n >= MIN_CELL."""
        rows = sorted(self.by_hour, key=lambda r: -r[1])[:k]
        return [(h, seen / n if n else 0.0, n) for h, seen, n in rows]


@dataclass(slots=True)
class When:
    hands: int = 0
    overall: float | None = None
    by_hour: dict[int, Cell] = field(default_factory=lambda: defaultdict(Cell))
    by_day: dict[int, Cell] = field(default_factory=lambda: defaultdict(Cell))
    grid: dict[tuple[int, int], Cell] = field(default_factory=lambda: defaultdict(Cell))
    by_month: dict[str, dict[str, Cell]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(Cell)))
    fish: list[Presence] = field(default_factory=list)


BLOCKS = (("02–06", range(2, 7)), ("07–12", range(7, 13)), ("13–20", range(13, 21)),
          ("21–01", (21, 22, 23, 0, 1)))


def _block(hour: int) -> str:
    return next(label for label, hours in BLOCKS if hour in hours)


def looseness(con, since: datetime, until: datetime, fish_names: list[str] = (),
              game: str = NL5) -> When:
    w = When()
    rows = con.execute(_LOOSE_SQL, [game, since, until]).fetchall()
    hero_hours: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    vals = []
    for _hid, at, net, v in rows:
        w.hands += 1
        if v is None:
            continue                          # everyone folded to a blind; no opponent acted
        vals.append(v)
        w.by_hour[at.hour].add(v, net)
        w.by_day[at.weekday()].add(v, net)
        w.grid[(at.weekday(), at.hour)].add(v, net)
        w.by_month[f"{at:%Y-%m}"][_block(at.hour)].add(v, net)
    w.overall = statistics.mean(vals) if vals else None

    if fish_names:
        seen: dict[str, list[datetime]] = defaultdict(list)
        sql = _PRESENCE_SQL.format(names=",".join("?" * len(fish_names)))
        for name, at in con.execute(sql, [*fish_names, game, since, until]).fetchall():
            seen[name].append(at)
        stamps = [at for _hid, at, _n, _v in rows]
        for name in fish_names:
            ats = seen.get(name)
            if not ats:
                continue
            first, last = min(ats), max(ats)
            span = [at for at in stamps if first <= at <= last]
            mine_h: dict[int, int] = defaultdict(int)
            mine_d: dict[int, int] = defaultdict(int)
            for at in span:
                mine_h[at.hour] += 1
                mine_d[at.weekday()] += 1
            theirs_h: dict[int, int] = defaultdict(int)
            theirs_d: dict[int, int] = defaultdict(int)
            for at in ats:
                theirs_h[at.hour] += 1
                theirs_d[at.weekday()] += 1
            w.fish.append(Presence(
                name, len(ats), first, last,
                [(h, theirs_h[h], mine_h[h]) for h in sorted(mine_h)],
                [(d, theirs_d[d], mine_d[d]) for d in sorted(mine_d)]))
    return w
