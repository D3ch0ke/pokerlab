"""Hero's *observed* ranges, straight from the hands.

This is the empirical half of leak detection: what you actually did, per
position, which is then compared against a reference chart. Recency-weighted,
because an open from October 2025 is weak evidence about how you play now.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from ..ranges.notation import Range, canonical, combos
from .core import EPOCH, FOREVER, NL5

HALF_LIFE_DAYS = 90.0

# Hero's first preflop decision in each hand, with the state it faced.
# `raises_before`/`callers_before` classify the spot; `hero_cards` is the hand.
# Params: game, since, until.
_FIRST_DECISION = """
WITH pf AS (
    SELECT a.*, h.hero_pos, h.hero_cards, h.played_at,
           -- COALESCE: the window is empty for a hand's first action.
           COALESCE(SUM(CASE WHEN a.verb = 'Raises to' THEN 1 ELSE 0 END) OVER w, 0) AS raises_before,
           COALESCE(SUM(CASE WHEN a.verb = 'Calls' THEN 1 ELSE 0 END) OVER w, 0) AS callers_before,
           -- who opened, so vs-open spots can be keyed by the opener's seat
           last_value(CASE WHEN a.verb = 'Raises to' THEN a.position END IGNORE NULLS) OVER w
               AS opener_pos
    FROM actions a JOIN hands h USING (hand_id)
    WHERE a.street = 'PRE-FLOP' AND NOT a.dead
      AND a.verb NOT IN ('Posts SB', 'Posts BB', 'Posts Ante')
      AND h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
    WINDOW w AS (PARTITION BY a.hand_id ORDER BY a.idx
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
first_hero AS (
    SELECT *, row_number() OVER (PARTITION BY hand_id ORDER BY idx) AS rn
    FROM pf WHERE is_hero
)
SELECT hand_id, hero_pos, hero_cards, verb, raises_before, callers_before, played_at, opener_pos
FROM first_hero WHERE rn = 1 AND hero_cards IS NOT NULL
"""


@dataclass(slots=True)
class Observation:
    hand: str            # canonical, e.g. "AKs"
    position: str
    verb: str
    raises_before: int
    callers_before: int
    at: datetime
    opener: str | None = None

    @property
    def spot(self) -> str:
        """The decision class this hand belongs to."""
        if self.raises_before == 0 and self.callers_before == 0:
            return "RFI"
        if self.raises_before == 1:
            return "vs_open"
        if self.raises_before >= 2:
            return "vs_3bet"
        return "vs_limp"

    @property
    def key(self) -> str:
        """Chart lookup key. Facing an open, the opener's seat changes
        everything, so it is part of the key rather than averaged away."""
        if self.spot == "vs_open" and self.opener:
            return f"{self.position}_vs_{self.opener}"
        return self.position

    def weight(self, now: datetime, half_life: float = HALF_LIFE_DAYS) -> float:
        """Exponential recency decay. A leak you fixed should stop counting.

        A very large half-life effectively disables decay, which trades an
        honest picture of *today* for a bigger sample of an older player.
        """
        days = (now - self.at).total_seconds() / 86400
        return 0.5 ** (max(days, 0) / half_life)


def observations(con, since: datetime = EPOCH, until: datetime = FOREVER,
                 game: str = NL5) -> list[Observation]:
    rows = con.execute(_FIRST_DECISION, [game, since, until]).fetchall()
    return [
        Observation(canonical(cards), pos, verb, rb, cb, at, opener)
        for _, pos, cards, verb, rb, cb, at, opener in rows
        if pos
    ]


def observed_range(obs: list[Observation], position: str, spot: str,
                   action_verbs: tuple[str, ...] = ("Raises to",),
                   now: datetime | None = None, min_weight: float = 1.0,
                   half_life: float = HALF_LIFE_DAYS) -> Range:
    """Recency-weighted frequency with which hero took an action, per hand.

    The weight is a frequency in [0, 1]: how often hero raised *this* hand in
    *this* spot, not how many times. Hands seen too rarely are dropped, since
    one sample is not a strategy.
    """
    now = now or datetime.now(timezone.utc)
    took: dict[str, float] = {}
    total: dict[str, float] = {}
    for o in obs:
        if o.key != position or o.spot != spot:
            continue
        w = o.weight(now, half_life)
        total[o.hand] = total.get(o.hand, 0.0) + w
        if o.verb in action_verbs:
            took[o.hand] = took.get(o.hand, 0.0) + w
    return Range({
        hand: round(took.get(hand, 0.0) / t, 3)
        for hand, t in total.items() if t >= min_weight
    })


def sample_sizes(obs: list[Observation], now: datetime | None = None,
                 half_life: float = HALF_LIFE_DAYS) -> dict[tuple[str, str], float]:
    """Weighted n per (position, spot), so callers can suppress thin cells."""
    now = now or datetime.now(timezone.utc)
    out: dict[tuple[str, str], float] = {}
    for o in obs:
        key = (o.key, o.spot)
        out[key] = out.get(key, 0.0) + o.weight(now, half_life)
    return out
