"""Aggregate stored verdicts: where the big blinds go, and the solver baseline.

Everything here reads `VerdictRecord`s written by `replay.grader`. Two rules
carried over from the rest of the tool: a rate is never shown without its n,
and a verdict that is not trustworthy (unconverged, substituted size, tree cut
down) contributes to the counts but never to an EV figure.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from ..replay.grader import VerdictRecord
from .core import MIN_N

#: Under this many graded decisions a bucket's EV loss is shown as a count only.
MIN_GRADED = 15


def _ok(rec: VerdictRecord) -> bool:
    """Counts toward an EV figure: trustworthy, stable across the band if a
    band was run, and the base read produced an EV loss."""
    b = rec.base
    return (b is not None and rec.trustworthy and rec.stable is not False
            and b.ev_loss is not None and not b.degraded)


def in_window(recs: list[VerdictRecord], since: datetime, until: datetime) -> list[VerdictRecord]:
    lo, hi = since.isoformat(), until.isoformat()
    return [r for r in recs if lo <= r.played_at < hi]


@dataclass(slots=True)
class Bucket:
    key: str
    graded: int = 0
    usable: int = 0
    loss_bb: float = 0.0          # summed EV given up, in bb, over usable decisions
    mistakes: int = 0             # usable decisions that lost more than tolerance
    unstable: int = 0             # band disagreed
    untrusted: int = 0

    @property
    def loss_per_decision(self) -> float | None:
        return self.loss_bb / self.usable if self.usable >= MIN_GRADED else None

    @property
    def mistake_rate(self) -> float | None:
        return self.mistakes / self.usable if self.usable >= MIN_GRADED else None


def by(recs: list[VerdictRecord], key) -> list[Bucket]:
    """Group verdicts by `key(rec)`; ordered by total EV lost, largest first."""
    buckets: dict[str, Bucket] = {}
    for r in recs:
        k = str(key(r))
        b = buckets.setdefault(k, Bucket(k))
        b.graded += 1
        if r.stable is False:
            b.unstable += 1
        if not r.trustworthy:
            b.untrusted += 1
        if _ok(r):
            b.usable += 1
            loss = r.loss_bb() or 0.0
            b.loss_bb += loss
            if r.base and r.base.approved is False:
                b.mistakes += 1
    return sorted(buckets.values(), key=lambda b: -b.loss_bb)


def preferred(read, pot: int) -> tuple[str, bool]:
    """The action the solver would be seen to take, and whether it is a clear
    preference.

    The highest-EV action is not the answer when the solver mixes: a 91/9
    call/raise split means the two EVs tie, and which one edges ahead by a
    fraction of a chip is noise. So the most frequent action is reported,
    unless some other action beats it by more than the tolerance — then that
    action is reported and the preference is marked clear.
    """
    from ..replay.evaluate import TOLERANCE_PCT_POT
    if not read or not read.ev:
        return "", False
    top = max(read.ev, key=read.ev.get)
    usual = max(read.freq, key=read.freq.get) if read.freq else top
    tol = TOLERANCE_PCT_POT * max(pot, 1)
    if read.ev[top] - read.ev.get(usual, read.ev[top]) > tol:
        return top, True
    return usual, False


def texture_label(rec: VerdictRecord) -> str:
    t = rec.texture
    if t.get("paired"):
        return "paired"
    return "wet" if t.get("wet") else "dry"


def biggest(recs: list[VerdictRecord], n: int = 10) -> list[VerdictRecord]:
    ok = [r for r in recs if _ok(r) and r.base and r.base.approved is False]
    return sorted(ok, key=lambda r: -(r.loss_bb() or 0))[:n]


@dataclass(slots=True)
class Totals:
    graded: int
    usable: int
    unstable: int
    untrusted: int
    loss_bb: float
    hands: int                     # distinct hands with at least one graded decision
    solve_seconds: float

    @property
    def loss_per_100_hands(self) -> float | None:
        """EV given up per 100 *graded* hands — comparable to a bb/100 winrate
        only over the hands that were graded, which the caller must say."""
        return 100 * self.loss_bb / self.hands if self.hands else None


def totals(recs: list[VerdictRecord]) -> Totals:
    ok = [r for r in recs if _ok(r)]
    return Totals(
        graded=len(recs), usable=len(ok),
        unstable=sum(1 for r in recs if r.stable is False),
        untrusted=sum(1 for r in recs if not r.trustworthy),
        loss_bb=sum(r.loss_bb() or 0 for r in ok),
        hands=len({r.hand_id for r in recs}),
        solve_seconds=sum(r.seconds for r in recs),
    )


# --------------------------------------------------------------------------
# solver c-bet baseline by texture
# --------------------------------------------------------------------------

@dataclass(slots=True)
class CbetCell:
    texture: str
    n: int
    solver: float | None          # range-weighted solver bet frequency, mean over spots
    hero: float | None            # share of these same spots where hero bet
    spots: list[str] = field(default_factory=list)

    @property
    def gap(self) -> float | None:
        return None if self.solver is None or self.hero is None else self.hero - self.solver


def cbet_baseline(recs: list[VerdictRecord], node: str = "srp_pfa",
                  group=texture_label) -> list[CbetCell]:
    """What the solver c-bets with the range hero actually arrives with, beside
    what hero did, on the same flops.

    Both numbers come from the same graded spots, so the comparison is not
    contaminated by which hands got graded. `solver` is the solver's bet
    frequency over hero's whole range at that node, averaged across spots;
    `hero` is the fraction of those spots hero bet. Only first-of-street
    decisions with no bet in front qualify — that is what a c-bet is.
    """
    cells: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for r in recs:
        if r.node != node or r.street != "FLOP" or not r.first_of_street:
            continue
        b = r.base
        if b is None or not r.trustworthy or not b.range_freq:
            continue
        solver_bet = sum(p for a, p in b.range_freq.items() if a.split()[0] in ("bet", "allin"))
        hero_bet = r.hero_verb in ("Bets", "Raises to")
        cells[group(r)].append((solver_bet, hero_bet))
    out = []
    for tex, rows in cells.items():
        n = len(rows)
        if n >= MIN_N:
            out.append(CbetCell(tex, n, round(sum(s for s, _ in rows) / n, 3),
                                round(sum(1 for _, h in rows if h) / n, 3)))
        else:
            out.append(CbetCell(tex, n, None, None))
    return sorted(out, key=lambda c: -c.n)
