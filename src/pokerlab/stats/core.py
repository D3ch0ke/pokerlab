"""Hero statistics, always over an explicit time window.

There is deliberately no lifetime default anywhere in this module. Play
changed materially over 11 months, so an all-time average blends several
different players and describes none of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import duckdb

NL5 = "NLHE 0.02/0.05 6 Max"
DEFAULT_WINDOW_DAYS = 90
#: Below this many opportunities a rate is noise and is reported as None.
MIN_N = 30

EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
FOREVER = datetime(2100, 1, 1, tzinfo=timezone.utc)

# Per-hand hero preflop facts. `raises_before` separates an open (0) from a
# 3-bet (1) from a 4-bet (2), which also gives fold-to-3bet for free.
# Params: game, since, until.
_CTE = """
WITH live AS (
    SELECT a.*, h.hero_pos, h.bb, h.hero_net, h.saw_flop, h.showdown, h.hero_rake, h.played_at,
           -- COALESCE matters: the window is empty for a hand's first action,
           -- and a NULL comparison quietly becomes "false" in the CASEs below.
           COALESCE(SUM(CASE WHEN a.verb = 'Raises to' THEN 1 ELSE 0 END) OVER (
               PARTITION BY a.hand_id ORDER BY a.idx
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS raises_before
    FROM actions a JOIN hands h USING (hand_id)
    WHERE a.street = 'PRE-FLOP' AND NOT a.dead
      AND a.verb NOT IN ('Posts SB', 'Posts BB', 'Posts Ante')
      AND h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
),
hero AS (
    SELECT hand_id,
           any_value(hero_pos) AS pos, any_value(bb) AS bb, any_value(hero_net) AS net,
           any_value(saw_flop) AS saw_flop, any_value(showdown) AS showdown,
           any_value(hero_rake) AS hero_rake,
           any_value(played_at) AS played_at,
           max(CASE WHEN is_hero AND verb IN ('Calls','Raises to','Bets') THEN 1 ELSE 0 END) AS vpip,
           max(CASE WHEN is_hero AND verb = 'Raises to' THEN 1 ELSE 0 END) AS pfr,
           max(CASE WHEN is_hero AND verb = 'Raises to' AND raises_before = 1 THEN 1 ELSE 0 END) AS threebet,
           max(CASE WHEN is_hero AND raises_before >= 1 THEN 1 ELSE 0 END) AS faced_raise,
           max(CASE WHEN is_hero AND verb = 'Calls' AND raises_before = 0
                     AND hero_pos NOT IN ('SB','BB') THEN 1 ELSE 0 END) AS limped,
           max(CASE WHEN is_hero AND verb = 'Raises to' AND raises_before = 0 THEN 1 ELSE 0 END) AS opened,
           max(CASE WHEN is_hero AND verb = 'Folds' AND raises_before >= 2 THEN 1 ELSE 0 END) AS folded_to_3b,
           max(CASE WHEN is_hero AND raises_before >= 2 THEN 1 ELSE 0 END) AS faced_3bet
    FROM live GROUP BY hand_id
)
"""


@dataclass(slots=True)
class Stat:
    """A rate that refuses to report itself on too small a sample."""

    made: int
    opportunities: int

    @property
    def pct(self) -> float | None:
        if self.opportunities < MIN_N:
            return None
        return round(100 * self.made / self.opportunities, 1)

    def __str__(self) -> str:
        return "--" if self.pct is None else f"{self.pct:.1f}%"


def window(days: int = DEFAULT_WINDOW_DAYS, until: datetime | None = None
           ) -> tuple[datetime, datetime]:
    until = until or datetime.now(timezone.utc)
    return until - timedelta(days=days), until


def summary(con: duckdb.DuckDBPyConnection, since: datetime, until: datetime,
            game: str = NL5) -> dict:
    (n, vpip, pfr, tb, faced, limp, faced_3b, f3b, net, bbsum, flops, wwsf, sd, wsd,
     rake) = con.execute(
        _CTE + """
        SELECT count(*), sum(vpip), sum(pfr), sum(threebet), sum(faced_raise),
               sum(limped), sum(faced_3bet), sum(folded_to_3b), sum(net), sum(bb),
               sum(saw_flop),
               sum(CASE WHEN saw_flop AND net > 0 THEN 1 ELSE 0 END),
               sum(CASE WHEN showdown THEN 1 ELSE 0 END),
               sum(CASE WHEN showdown AND net > 0 THEN 1 ELSE 0 END),
               sum(hero_rake)
        FROM hero""", [game, since, until]).fetchone()

    if not n:
        return {"hands": 0, "since": since.date(), "until": until.date()}

    bb = bbsum / n  # cents per big blind; constant within a stake

    return {
        "hands": n, "since": since.date(), "until": until.date(),
        "net_eur": round(net / 100, 2),
        "bb100": round(100 * (net / bb) / n, 2),
        "rake_eur": round(rake / 100, 2),
        "rake_bb100": round(100 * (rake / bb) / n, 2),
        "VPIP": Stat(vpip, n), "PFR": Stat(pfr, n),
        "3bet": Stat(tb, faced), "fold_to_3bet": Stat(f3b, faced_3b),
        "limp": Stat(limp, n), "WWSF": Stat(wwsf, flops),
        "WTSD": Stat(sd, flops), "W$SD": Stat(wsd, sd),
    }


def by_position(con, since: datetime, until: datetime, game: str = NL5) -> list[tuple]:
    return con.execute(_CTE + """
        SELECT pos, count(*) AS n,
               round(100.0 * sum(vpip) / count(*), 1) AS vpip,
               round(100.0 * sum(pfr) / count(*), 1) AS pfr,
               CASE WHEN sum(faced_raise) >= 30
                    THEN round(100.0 * sum(threebet) / sum(faced_raise), 1) END AS tb,
               round(100.0 * sum(limped) / count(*), 1) AS limp,
               round(sum(net) / 100.0, 2) AS net_eur
        FROM hero WHERE pos IS NOT NULL
        GROUP BY pos
        ORDER BY array_position(['UTG','HJ','CO','BTN','SB','BB'], pos)
    """, [game, since, until]).fetchall()


def monthly(con, game: str = NL5) -> list[tuple]:
    """The full history as a monthly series. The trend is the point."""
    return con.execute(_CTE + """
        SELECT strftime(played_at, '%Y-%m') AS month, count(*) AS n,
               round(100.0 * sum(vpip) / count(*), 1) AS vpip,
               round(100.0 * sum(pfr) / count(*), 1) AS pfr,
               round(100.0 * (sum(net) / avg(bb)) / count(*), 1) AS bb100
        FROM hero GROUP BY 1 ORDER BY 1
    """, [game, EPOCH, FOREVER]).fetchall()
