"""Session reconstruction and tilt testing.

Built to answer "I climb, then give it back" — a belief almost every player
holds. The honest test is not whether give-back happens (it always does: a
peak is by definition a maximum, so play after it regresses) but whether it
happens *more than chance*. So every trajectory statistic here is compared
against a null model built by reshuffling the player's own hands.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .core import NL5

#: Wall-clock gap that ends a session. Across tables, since tilt is a property
#: of the player, not of a table.
SESSION_GAP = timedelta(minutes=30)


#: The per-session stop agreed in Sep 2026: two buy-ins, 200bb at NL5.
STOP_BB = 200.0


@dataclass(slots=True)
class Session:
    start: datetime
    nets: list[float]      # per hand, in bb
    hand_ids: list[str] = field(default_factory=list)
    times: list[datetime] = field(default_factory=list)
    tables: int = 0
    net_eur: float = 0.0

    @property
    def end(self) -> datetime:
        return self.times[-1] if self.times else self.start

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    def stop_crossed(self, stop: float = STOP_BB) -> int | None:
        """Index of the first hand at which the running result reached -stop."""
        run = 0.0
        for i, x in enumerate(self.nets):
            run += x
            if run <= -stop:
                return i
        return None

    def played_past_stop(self, stop: float = STOP_BB) -> tuple[int, float] | None:
        """(hands, minutes) played after the stop was crossed, if it was."""
        i = self.stop_crossed(stop)
        if i is None or not self.times:
            return None
        return len(self.nets) - i - 1, (self.times[-1] - self.times[i]).total_seconds() / 60

    @property
    def hands(self) -> int:
        return len(self.nets)

    @property
    def final(self) -> float:
        return sum(self.nets)

    @property
    def peak(self) -> float:
        run = best = 0.0
        for x in self.nets:
            run += x
            best = max(best, run)
        return best

    @property
    def peak_position(self) -> float:
        """How far through the session the high-water mark occurred, 0-1."""
        run = best = 0.0
        at = 0
        for i, x in enumerate(self.nets):
            run += x
            if run > best:
                best, at = run, i
        return at / max(len(self.nets) - 1, 1)

    @property
    def given_back(self) -> float:
        return self.peak - self.final


def sessions(con, game: str = NL5) -> list[Session]:
    rows = con.execute(
        "SELECT played_at, hero_net / bb, hand_id, table_id, hero_net "
        "FROM hands WHERE game_name = ? ORDER BY played_at",
        [game]).fetchall()
    out: list[Session] = []
    cur: list[float] = []
    ids: list[str] = []
    times: list[datetime] = []
    tables: set[str] = set()
    eur = 0.0
    start = prev = None

    def close():
        out.append(Session(start, cur, ids, times, len(tables), round(eur / 100, 2)))

    for at, net, hid, table, cents in rows:
        if prev and at - prev > SESSION_GAP:
            close()
            cur, ids, times, tables, eur, start = [], [], [], set(), 0.0, at
        start = start or at
        cur.append(net)
        ids.append(hid)
        times.append(at)
        tables.add(table or "")
        eur += cents
        prev = at
    if cur:
        close()
    return out


#: Family-wise error rate across all trajectory statistics tested together.
FAMILY_ALPHA = 0.10


@dataclass(slots=True)
class TiltTest:
    name: str
    observed: float
    null_median: float
    null_lo: float
    null_hi: float
    percentile: float
    family_size: int = 1

    @property
    def threshold(self) -> float:
        """Bonferroni-corrected one-tail percentile threshold.

        Five statistics tested at an uncorrected 5%/95% would flag roughly
        40% of players who do nothing wrong at all. Since the entire purpose
        of this module is to avoid manufacturing a finding, the threshold
        divides by the number of tests in the family.
        """
        return 100 * (FAMILY_ALPHA / 2) / max(self.family_size, 1)

    @property
    def significant(self) -> bool:
        return self.percentile >= 100 - self.threshold or self.percentile <= self.threshold


def _stats(sess: list[Session], min_peak: float) -> dict[str, float] | None:
    peaked = [s for s in sess if s.peak >= min_peak]
    if len(peaked) < 10:
        return None
    return {
        "sessions peaking": len(peaked),
        "bb given back": statistics.median(s.given_back for s in peaked),
        "give-back % of peak": 100 * statistics.median(s.given_back / s.peak for s in peaked),
        "peak position %": 100 * statistics.median(s.peak_position for s in peaked),
        "% ending below half peak":
            100 * sum(1 for s in peaked if s.final < s.peak / 2) / len(peaked),
    }


def tilt_tests(sess: list[Session], min_peak: float = 50, trials: int = 2000,
               seed: int = 42) -> list[TiltTest]:
    """Compare real session trajectories against memoryless reshuffles.

    The null keeps session lengths and the per-hand result distribution and
    destroys only the *order* — so any excess give-back must come from hands
    influencing each other, which is what tilt would mean.
    """
    observed = _stats(sess, min_peak)
    if observed is None:
        return []

    pool = [x for s in sess for x in s.nets]
    rng = random.Random(seed)
    draws: dict[str, list[float]] = {k: [] for k in observed}
    for _ in range(trials):
        shuffled = [Session(s.start, [pool[rng.randrange(len(pool))] for _ in s.nets])
                    for s in sess]
        sim = _stats(shuffled, min_peak)
        if sim:
            for k, v in sim.items():
                draws[k].append(v)

    out = []
    for name, value in observed.items():
        v = sorted(draws[name])
        if not v:
            continue
        out.append(TiltTest(
            name=name, observed=value, null_median=statistics.median(v),
            null_lo=v[int(0.05 * len(v))], null_hi=v[int(0.95 * len(v))],
            percentile=100 * sum(1 for x in v if x < value) / len(v),
            family_size=len(observed),
        ))
    return out


def detectable_shift(n: int, rate: float = 0.35) -> float:
    """Smallest change in a percentage stat this sample could detect, in points.

    Reported alongside every null result: "no effect found" is only meaningful
    next to the size of effect that would have been found.
    """
    return 2 * 1.96 * math.sqrt(rate * (1 - rate) / n) * 100
