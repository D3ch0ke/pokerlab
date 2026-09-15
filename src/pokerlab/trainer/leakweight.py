"""Where hero's observed play diverges from a reference chart.

Deviation is measured in *combos*, not in hands: folding AKs matters four
times as much as folding a single offsuit combo would suggest, and a pair is
six. Everything is recency-weighted, so a habit you have already dropped
stops driving your drills.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..ranges.chart import Chart
from ..ranges.notation import combos
from ..stats.ranges import HALF_LIFE_DAYS, Observation, observed_range, sample_sizes

#: Weighted observations required before a (position, spot) cell is judged.
MIN_CELL_WEIGHT = 40.0
#: Ignore differences smaller than this; charts are not that precise.
NOISE_FLOOR = 0.25


@dataclass(slots=True, frozen=True)
class Deviation:
    position: str
    hand: str
    observed: float      # hero's frequency for the charted action
    prescribed: float
    weight: float        # recency-weighted times hero faced this exact spot

    @property
    def gap(self) -> float:
        return self.observed - self.prescribed

    @property
    def kind(self) -> str:
        return "too loose" if self.gap > 0 else "too tight"

    @property
    def mass(self) -> float:
        """Deviation scaled by combos and by how often it actually comes up.

        This is the quantity the quiz samples on: a big error in a rare spot
        should not outrank a moderate error you make constantly.
        """
        return abs(self.gap) * combos(self.hand) * self.weight


def deviations(obs: list[Observation], chart: Chart, action: str = "raise",
               verbs: tuple[str, ...] = ("Raises to",),
               now: datetime | None = None) -> list[Deviation]:
    now = now or datetime.now(timezone.utc)
    sizes = sample_sizes(obs, now)
    out: list[Deviation] = []

    for position in chart.positions:
        if sizes.get((position, chart.spot), 0.0) < MIN_CELL_WEIGHT:
            continue  # too thin to accuse anyone of anything
        prescribed = chart.action_range(position, action)
        seen = observed_range(obs, position, chart.spot, verbs, now)

        per_hand: dict[str, float] = {}
        for o in obs:
            if o.key == position and o.spot == chart.spot:
                per_hand[o.hand] = per_hand.get(o.hand, 0.0) + o.weight(now)

        for hand, weight in per_hand.items():
            got, want = seen.freq(hand), prescribed.freq(hand)
            if abs(got - want) >= NOISE_FLOOR:
                out.append(Deviation(position, hand, round(got, 3), want, round(weight, 2)))

    return sorted(out, key=lambda d: d.mass, reverse=True)


def by_position(devs: list[Deviation]) -> dict[str, dict[str, float]]:
    """Total error mass per position, split into too-tight and too-loose."""
    out: dict[str, dict[str, float]] = {}
    for d in devs:
        cell = out.setdefault(d.position, {"too tight": 0.0, "too loose": 0.0})
        cell[d.kind] += d.mass
    return out


def sampling_weights(devs: list[Deviation]) -> dict[tuple[str, str], float]:
    """Quiz sampling distribution: P(spot) proportional to error mass."""
    total = sum(d.mass for d in devs) or 1.0
    return {(d.position, d.hand): d.mass / total for d in devs}


@dataclass(slots=True, frozen=True)
class BucketLeak:
    """A deviation aggregated to where the sample size supports a claim."""

    position: str
    bucket: str
    observed: float
    prescribed: float
    weight: float
    hands: int

    @property
    def gap(self) -> float:
        return self.observed - self.prescribed

    @property
    def kind(self) -> str:
        return "too loose" if self.gap > 0 else "too tight"

    @property
    def mass(self) -> float:
        return abs(self.gap) * self.weight


#: Weighted observations needed before a bucket is reported at all.
MIN_BUCKET_WEIGHT = 15.0


def bucket_leaks(obs: list[Observation], chart: Chart, action: str = "raise",
                 verbs: tuple[str, ...] = ("Raises to",),
                 now: datetime | None = None,
                 half_life: float = HALF_LIFE_DAYS) -> list[BucketLeak]:
    """Observed vs prescribed frequency per (position, bucket).

    Frequencies are combo-weighted within the bucket, so the answer is "what
    share of the combos you were dealt here did you raise", not an unweighted
    average over hand labels.
    """
    from ..ranges.buckets import bucket as bucket_of

    now = now or datetime.now(timezone.utc)
    sizes = sample_sizes(obs, now, half_life)
    out: list[BucketLeak] = []

    for position in chart.positions:
        if sizes.get((position, chart.spot), 0.0) < MIN_CELL_WEIGHT:
            continue
        prescribed = chart.action_range(position, action)

        took: dict[str, float] = {}
        total: dict[str, float] = {}
        want: dict[str, float] = {}
        names: dict[str, set[str]] = {}

        for o in obs:
            if o.key != position or o.spot != chart.spot:
                continue
            b, w = bucket_of(o.hand), o.weight(now, half_life)
            total[b] = total.get(b, 0.0) + w
            want[b] = want.get(b, 0.0) + w * prescribed.freq(o.hand)
            names.setdefault(b, set()).add(o.hand)
            if o.verb in verbs:
                took[b] = took.get(b, 0.0) + w

        for b, t in total.items():
            if t < MIN_BUCKET_WEIGHT:
                continue
            got, exp = took.get(b, 0.0) / t, want[b] / t
            if abs(got - exp) >= 0.10:
                out.append(BucketLeak(position, b, round(got, 3), round(exp, 3),
                                      round(t, 1), len(names[b])))

    return sorted(out, key=lambda leak: leak.mass, reverse=True)


@dataclass(slots=True, frozen=True)
class LeakTrend:
    """One bucket, then vs now, against the chart.

    Reporting a single recency-weighted number was actively misleading: the
    sample-size filter let through whichever leaks had *improved* most (their
    recent n survived) while hiding unchanged ones. Showing both halves with
    their raw counts makes the direction visible instead of inferred.
    """

    bucket: str
    chart: float
    early: float
    early_n: int
    recent: float
    recent_n: int

    @property
    def gap(self) -> float:
        return self.recent - self.chart

    @property
    def kind(self) -> str:
        return "too loose" if self.gap > 0 else "too tight"

    @property
    def delta(self) -> float:
        """Movement since the earlier period. Negative = tightening up."""
        return self.recent - self.early

    @property
    def mass(self) -> float:
        return abs(self.gap) * self.recent_n

    @property
    def trend(self) -> str:
        if self.early_n < MIN_TREND_N or abs(self.delta) < 0.08:
            return "flat"
        closing = abs(self.recent - self.chart) < abs(self.early - self.chart)
        return "improving" if closing else "worsening"


#: Raw observations needed in a half before it is quoted at all.
MIN_TREND_N = 25


def leak_trend(obs: list[Observation], chart: Chart, action: str = "raise",
               verbs: tuple[str, ...] = ("Raises to",), split_days: int = 90,
               now: datetime | None = None) -> list[LeakTrend]:
    """Per bucket, pooled across positions, raw (undecayed) counts per half.

    Positions are pooled because per-position halves are too thin to read.
    The chart expectation is still computed per observation, so it accounts
    for the position and hand actually held.
    """
    from ..ranges.buckets import bucket as bucket_of

    now = now or datetime.now(timezone.utc)
    cells: dict[str, dict[str, list[float]]] = {}

    for o in obs:
        if o.spot != chart.spot or o.key not in chart.ranges:
            continue
        half = "recent" if (now - o.at).days <= split_days else "early"
        cell = cells.setdefault(bucket_of(o.hand), {"early": [], "recent": [], "chart": []})
        cell[half].append(1.0 if o.verb in verbs else 0.0)
        cell["chart"].append(chart.action_range(o.position, action).freq(o.hand))

    out = []
    for name, cell in cells.items():
        if len(cell["recent"]) < MIN_TREND_N or len(cell["early"]) < MIN_TREND_N:
            continue
        out.append(LeakTrend(
            bucket=name,
            chart=round(sum(cell["chart"]) / len(cell["chart"]), 3),
            early=round(sum(cell["early"]) / len(cell["early"]), 3),
            early_n=len(cell["early"]),
            recent=round(sum(cell["recent"]) / len(cell["recent"]), 3),
            recent_n=len(cell["recent"]),
        ))
    return sorted(out, key=lambda t: t.mass, reverse=True)


@dataclass(slots=True, frozen=True)
class DefenceCell:
    opener: str
    n: int
    observed: float
    prescribed: float

    @property
    def gap(self) -> float:
        return self.observed - self.prescribed


def defence_curve(obs: list[Observation], chart: Chart, hero_pos: str = "BB",
                  order: tuple[str, ...] = ("UTG", "HJ", "CO", "BTN", "SB")) -> list[DefenceCell]:
    """How hero's defence frequency responds to the opener's seat.

    Deliberately reported as a *curve* rather than as per-spot deviations,
    because the shape is robust to the chart being imprecise. That defence
    should widen monotonically as the opener's seat gets later is ordinary
    poker theory, not a number I authored -- so a flat or inverted curve is a
    finding even if every absolute level here is somewhat off.
    """
    out = []
    for opener in order:
        key = f"{hero_pos}_vs_{opener}"
        sub = [o for o in obs if o.spot == chart.spot and o.key == key]
        if len(sub) < MIN_TREND_N or key not in chart.ranges:
            continue
        n = len(sub)
        observed = sum(1 for o in sub if o.verb in ("Raises to", "Calls")) / n
        prescribed = sum(chart.action_range(key, "raise").freq(o.hand)
                         + chart.action_range(key, "call").freq(o.hand) for o in sub) / n
        out.append(DefenceCell(opener, n, round(observed, 3), round(prescribed, 3)))
    return out


def curve_is_flat(cells: list[DefenceCell], min_spread: float = 0.20) -> bool:
    """True when defence barely responds to the opener's position."""
    if len(cells) < 3:
        return False
    return max(c.observed for c in cells) - min(c.observed for c in cells) < min_spread
