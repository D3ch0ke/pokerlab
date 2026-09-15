"""What the pool actually does, measured from every villain action on file.

The replayer needs villain ranges, and a range is two separate claims: how
*wide* it is, and *which* hands are in it. Those two are knowable to very
different degrees, and this module keeps them apart:

* **Width is measured.** Every preflop decision every villain made is in the
  database whether or not they showed a hand, so an action frequency rests on
  thousands of observations. A player is dealt a uniformly random hand, so the
  share of hands on which they take an action *is* the width of the range they
  take it with.

* **Holdings are barely observed.** Cards are only visible at showdown -- 4,801
  villain hands out of 9,845 -- and that subset is biased: it over-represents
  hands that flopped well enough to keep paying, and under-represents the ones
  folded on the flop. It tells you what is at the TOP of a range, never where
  the bottom ends.

Nothing here corrects the showdown bias, because it cannot be corrected from
this data. It is labelled instead, and the consumer decides what to do with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

from ..ranges.notation import canonical
from ..stats.core import EPOCH, FOREVER, MIN_N, NL5

#: Every villain preflop decision, with the state it faced, the villain's own
#: previous action in the hand (a limper facing a raise is not a cold caller),
#: and the cards if they were ever shown. Hero is excluded: this is the pool.
_DECISIONS = """
WITH pf AS (
    SELECT a.hand_id, a.idx, a.name, a.position, a.verb, a.is_hero,
           -- COALESCE: the window is empty for a hand's first action, and a
           -- NULL silently reads as "false" in every comparison below.
           COALESCE(SUM(CASE WHEN a.verb = 'Raises to' THEN 1 ELSE 0 END) OVER w, 0)
               AS raises_before,
           COALESCE(SUM(CASE WHEN a.verb = 'Calls' THEN 1 ELSE 0 END) OVER w, 0)
               AS callers_before,
           last_value(CASE WHEN a.verb = 'Raises to' THEN a.position END IGNORE NULLS) OVER w
               AS opener_pos
    FROM actions a JOIN hands h USING (hand_id)
    WHERE a.street = 'PRE-FLOP' AND NOT a.dead
      AND a.verb NOT IN ('Posts SB', 'Posts BB', 'Posts Ante')
      AND h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
    WINDOW w AS (PARTITION BY a.hand_id ORDER BY a.idx
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
own AS (
    SELECT *, lag(verb) OVER (PARTITION BY hand_id, name ORDER BY idx) AS prior_verb
    FROM pf WHERE NOT is_hero
)
SELECT f.name, f.position, f.verb, f.raises_before, f.callers_before, f.opener_pos,
       f.prior_verb, s.shown
FROM own f LEFT JOIN seats s ON s.hand_id = f.hand_id AND s.name = f.name
WHERE f.position IS NOT NULL
"""


def spot_of(raises_before: int, callers_before: int) -> str:
    """The decision class a preflop action belongs to."""
    if raises_before == 0 and callers_before == 0:
        return "RFI"
    if raises_before == 1:
        return "vs_open"
    if raises_before >= 2:
        return "vs_3bet"
    return "vs_limp"


def cell_key(position: str, spot: str, opener: str | None, prior: str | None = None) -> str:
    """Chart lookup key: facing an open, the opener's seat is part of the spot.

    `prior` is the player's own earlier action in the hand. A limper facing a
    raise and a cold caller share a position and a spot but not a range, so a
    second decision gets its own cell.
    """
    base = f"{position}_vs_{opener}" if spot == "vs_open" and opener else position
    tag = {"Calls": "~after_call", "Raises to": "~after_raise"}.get(prior or "", "")
    return base + tag


@dataclass(slots=True)
class Cell:
    """One (position, spot, action) node, aggregated over the whole pool."""

    position: str
    spot: str
    verb: str
    opportunities: int = 0
    taken: int = 0
    shown: dict[str, int] = field(default_factory=dict)   # canonical hand -> times seen

    @property
    def frequency(self) -> float | None:
        """Share of hands dealt here on which the pool takes this action.

        None below MIN_N: a frequency from a handful of spots is not a width.
        """
        if self.opportunities < MIN_N:
            return None
        return self.taken / self.opportunities

    @property
    def shown_n(self) -> int:
        return sum(self.shown.values())


@dataclass(slots=True)
class Pool:
    cells: dict[tuple[str, str, str], Cell]
    by_villain: dict[tuple[str, str, str, str], Cell]
    since: datetime
    until: datetime
    rates: dict[str, StreetRates] = field(default_factory=dict)
    villain_rates: dict[str, "VillainRates"] = field(default_factory=dict)

    def keep_for(self, villain: str | None, street: str, verb: str
                 ) -> tuple[float | None, int, str, str]:
        """How much of the villain's range survives `verb` on `street`.

        The pool's rate for that street is the prior; the named villain's own
        postflop record, pooled over streets because per-street counts are
        thin, pulls it toward what THEY do, with `VILLAIN_PRIOR` pseudo-counts
        so a handful of hands moves it a little and a hundred moves it most of
        the way. Returns (keep, n, what happened, where the number came from).
        """
        rates = self.rates.get(street)
        if rates is None:
            return None, 0, "", ""
        keep, n, what = rates.keep_for(verb)
        if keep is None:
            return None, 0, what, ""
        own = self.villain_rates.get(villain or "")
        k, m = (own.counts(verb) if own else (0, 0))
        if m < VILLAIN_MIN_N:
            return keep, n, what, f"pool rate over {n:,} spots"
        shrunk = (k + VILLAIN_PRIOR * keep) / (m + VILLAIN_PRIOR)
        return (shrunk, m, what,
                f"{villain}'s own {k}/{m} postflop, shrunk toward the pool's {keep:.0%}")

    def cell(self, position: str, spot: str, verb: str) -> Cell:
        return self.cells.get((position, spot, verb), Cell(position, spot, verb))

    def villain_cell(self, name: str, position: str, spot: str, verb: str) -> Cell | None:
        """That specific player's own cell, or None if too thin to use.

        Names are stable across the exports and the regulars recur in hundreds
        of hands, so a *frequency* for a named villain is often well sampled.
        Their holdings never are -- the best-observed villain showed 53 hands --
        so this is only ever consulted for width.
        """
        c = self.by_villain.get((name, position, spot, verb))
        return c if c and c.opportunities >= MIN_N else None

    def hands_seen(self, name: str) -> int:
        return sum(c.opportunities for (n, *_), c in self.by_villain.items() if n == name)


def build(con, since: datetime = EPOCH, until: datetime = FOREVER,
          game: str = NL5) -> Pool:
    rows = con.execute(_DECISIONS, [game, since, until]).fetchall()

    cells: dict[tuple[str, str, str], Cell] = {}
    by_villain: dict[tuple[str, str, str, str], Cell] = {}
    verbs = ("Folds", "Calls", "Raises to", "Checks", "Bets")

    for name, position, verb, raises, callers, opener, prior, shown in rows:
        spot = spot_of(raises, callers)
        key = cell_key(position, spot, opener, prior)
        hand = canonical(shown.split()) if shown else None

        # An opportunity counts once per possible action, so every action in
        # the same node shares one denominator and the frequencies sum to 1.
        for v in verbs:
            for store, k in ((cells, (key, spot, v)),
                             (by_villain, (name, key, spot, v))):
                c = store.get(k)
                if c is None:
                    c = store[k] = Cell(key, spot, v)
                c.opportunities += 1
                if v == verb:
                    c.taken += 1
                    if hand:
                        c.shown[hand] = c.shown.get(hand, 0) + 1

    return Pool(cells, by_villain, since, until)


#: What villains do postflop, by street. Far better sampled than showdowns:
#: a fold is visible even though the folded cards are not, so these rates rest
#: on every villain response on file rather than the quarter that showed down.
_POSTFLOP = """
WITH ctx AS (
    SELECT a.street, a.verb, a.is_hero,
           COALESCE(SUM(CASE WHEN a.verb IN ('Bets', 'Raises to') THEN 1 ELSE 0 END) OVER w, 0)
               AS bets_before
    FROM actions a JOIN hands h USING (hand_id)
    WHERE h.game_name = ? AND a.street <> 'PRE-FLOP'
      AND h.played_at >= ? AND h.played_at < ?
    WINDOW w AS (PARTITION BY a.hand_id, a.street ORDER BY a.idx
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
)
SELECT street,
       SUM(CASE WHEN bets_before >= 1 AND verb <> 'Folds' THEN 1 ELSE 0 END) AS continued,
       SUM(CASE WHEN bets_before >= 1 AND verb = 'Raises to' THEN 1 ELSE 0 END) AS raised,
       SUM(CASE WHEN bets_before >= 1 THEN 1 ELSE 0 END) AS faced,
       SUM(CASE WHEN bets_before = 0 AND verb = 'Bets' THEN 1 ELSE 0 END) AS bet,
       SUM(CASE WHEN bets_before = 0 THEN 1 ELSE 0 END) AS unopened
FROM ctx WHERE NOT is_hero
GROUP BY street
"""


@dataclass(frozen=True, slots=True)
class StreetRates:
    """How often the pool takes each postflop action, and off what sample.

    These are the shares a range is cut to after the fact. Continuing against
    a bet is a wide action; raising one is a narrow one, and treating both as
    "villain did not fold" would leave a raiser's range three times too wide.
    """

    street: str
    continue_rate: float
    raise_rate: float
    faced: int
    bet_rate: float
    unopened: int

    def keep_for(self, verb: str) -> tuple[float | None, int, str]:
        """The share of a range to keep, given the action the villain took."""
        if verb == "Raises to":
            return self.raise_rate, self.faced, "raised a bet"
        if verb == "Calls":
            return self.continue_rate, self.faced, "called a bet"
        if verb == "Bets":
            return self.bet_rate, self.unopened, "bet an unopened street"
        return None, 0, "checked"          # a check narrows almost nothing


#: A named villain's own postflop record counts from this many decisions on;
#: below it the pool rate stands alone. Ten faced bets is enough to notice a
#: player who never folds; it is not enough to overrule the pool, which is
#: what the prior weight is for.
VILLAIN_MIN_N = 10
#: Pseudo-count weight of the pool rate when blending a villain's own record.
VILLAIN_PRIOR = 15.0


@dataclass(frozen=True, slots=True)
class VillainRates:
    """One player's postflop actions, all streets together."""

    name: str
    continued: int
    raised: int
    faced: int
    bet: int
    unopened: int

    def counts(self, verb: str) -> tuple[int, int]:
        """(times took `verb`, opportunities to)."""
        if verb == "Raises to":
            return self.raised, self.faced
        if verb == "Calls":
            return self.continued, self.faced
        if verb == "Bets":
            return self.bet, self.unopened
        return 0, 0


_VILLAIN_POSTFLOP = _POSTFLOP.replace(
    "SELECT a.street, a.verb, a.is_hero,", "SELECT a.name, a.street, a.verb, a.is_hero,").replace(
    "SELECT street,\n", "SELECT name,\n").replace("GROUP BY street", "GROUP BY name")


def villain_postflop_rates(con, since: datetime = EPOCH, until: datetime = FOREVER,
                           game: str = NL5) -> dict[str, VillainRates]:
    out: dict[str, VillainRates] = {}
    for name, cont, raised, faced, bet, unopened in con.execute(
            _VILLAIN_POSTFLOP, [game, since, until]).fetchall():
        if (faced or 0) + (unopened or 0) < VILLAIN_MIN_N:
            continue
        out[name] = VillainRates(name, cont or 0, raised or 0, faced or 0, bet or 0, unopened or 0)
    return out


def postflop_rates(con, since: datetime = EPOCH, until: datetime = FOREVER,
                   game: str = NL5) -> dict[str, StreetRates]:
    out: dict[str, StreetRates] = {}
    for street, cont, raised, faced, bet, unopened in con.execute(
            _POSTFLOP, [game, since, until]).fetchall():
        if faced < MIN_N or unopened < MIN_N:
            continue
        out[street] = StreetRates(street, cont / faced, raised / faced, faced,
                                  bet / unopened, unopened)
    return out


@lru_cache(maxsize=4)
def cached(db_path: str, since: datetime = EPOCH, until: datetime = FOREVER) -> Pool:
    """Build once per process: the scan is a few seconds over 125k actions."""
    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    try:
        pool = build(con, since, until)
        pool.rates = postflop_rates(con, since, until)
        pool.villain_rates = villain_postflop_rates(con, since, until)
        return pool
    finally:
        con.close()
