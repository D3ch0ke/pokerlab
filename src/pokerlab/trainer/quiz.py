"""Preflop drill: spots sampled from where hero actually goes wrong.

Sampling is deliberately mixed. Pure leak-weighting would only ever show you
your worst spots, which trains a distorted picture of the range as a whole,
so a share of questions is drawn uniformly from the chart.

Grading reads the chart's *whole* action distribution. It used to read only the
raise range and treat everything else as a fold, which meant a hand the chart
called 100% of the time was scored correct when folded -- the drill taught the
opposite of the chart across every call-heavy node it covered.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..ranges.chart import Chart
from ..ranges.notation import all_hands, combos
from ..stats.ranges import Observation, observed_range
from .leakweight import bucket_leaks
from .progress import Progress

#: Share of questions drawn from the leak distribution rather than uniformly.
LEAK_SHARE = 0.7
#: Share reserved for spaced-repetition cards that have come due.
DUE_SHARE = 0.35
#: An action taken at least this often is a live option; two of them is a mix.
MIX_BAND = 0.2


class NoSpots(LookupError):
    """The scenario/position filter matches nothing.

    Raised rather than quietly widening the filter: a drill that silently
    serves spots you did not ask for is worse than one that says it is empty.
    """


@dataclass(frozen=True, slots=True)
class Spot:
    chart_id: str
    label: str
    position: str
    hand: str
    context: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.chart_id, self.position, self.hand)


@dataclass(frozen=True, slots=True)
class Filter:
    """Which corner of the chart set to drill. Empty means everything."""

    spot: str | None = None       # chart.spot: "RFI" | "vs_open" | "vs_3bet" | "vs_limp"
    position: str | None = None
    mode: str = ""                # "" | "struggling" (2+ lapses) | "due" (interval elapsed)

    def allows_chart(self, chart: Chart) -> bool:
        if self.spot and chart.spot != self.spot:
            return False
        return not self.position or self.position in chart.ranges

    def allows(self, spot: Spot) -> bool:
        if self.spot and spot.context != self.spot:
            return False
        return not self.position or spot.position == self.position


@dataclass(frozen=True, slots=True)
class Verdict:
    correct: bool
    mixed: bool
    prescribed: dict[str, float]      # the chart's full distribution for this hand
    best: str                         # highest-frequency action
    accepted: tuple[str, ...]         # every action graded correct
    chart_freq: float                 # how often the chart takes the action you chose
    your_freq: float | None           # hero's own RAISE frequency; see note
    your_n: float
    note: str

    @property
    def summary(self) -> str:
        return " · ".join(f"{a} {f:.0%}" for a, f in self.prescribed.items() if f > 0)


@dataclass
class Session:
    asked: int = 0
    right: int = 0
    seen: dict[tuple[str, str, str], int] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.right / self.asked if self.asked else 0.0


class Quiz:
    def __init__(self, charts: list[Chart], obs: list[Observation],
                 now: datetime | None = None, seed: int | None = None,
                 progress: Progress | None = None):
        self.charts = {c.id: c for c in charts}
        self.obs = obs
        self.now = now or datetime.now(timezone.utc)
        self.rng = random.Random(seed)
        self.session = Session()
        self.progress = progress or Progress()
        self._observed: dict[tuple[str, str], object] = {}
        self._weights = self._build_weights()

    def _build_weights(self) -> list[tuple[Spot, float]]:
        """Weight each (chart, position, hand) by this player's error mass.

        Bucket-level leaks are spread across the hands in that bucket, in
        proportion to combos, so a leak on offsuit kings surfaces K7o and
        K5o rather than one arbitrary representative.
        """
        from ..ranges.buckets import bucket as bucket_of

        out: list[tuple[Spot, float]] = []
        for chart in self.charts.values():
            leaks = {(leak.position, leak.bucket): leak.mass
                     for leak in bucket_leaks(self.obs, chart, half_life=1e9)}
            for position in chart.positions:
                for hand in all_hands():
                    mass = leaks.get((position, bucket_of(hand)), 0.0)
                    if mass <= 0:
                        continue
                    spot = Spot(chart.id, chart.label, position, hand, chart.spot)
                    out.append((spot, mass * combos(hand)))
        return out

    def scenarios(self) -> dict[str, list[str]]:
        """Chart spot -> the positions any loaded chart actually covers for it."""
        out: dict[str, set[str]] = {}
        for chart in self.charts.values():
            out.setdefault(chart.spot, set()).update(chart.positions)
        return {spot: sorted(positions) for spot, positions in sorted(out.items())}

    def _uniform_spot(self, filt: Filter = Filter()) -> Spot:
        charts = [c for c in self.charts.values() if filt.allows_chart(c)]
        if not charts:
            raise NoSpots(f"no chart covers {filt.spot or 'any spot'} / "
                          f"{filt.position or 'any position'}")
        chart = self.rng.choice(charts)
        positions = [p for p in chart.positions
                     if not filt.position or p == filt.position]
        position = self.rng.choice(positions)
        # Weight by combos so the drill matches the frequency hands are dealt.
        hands = all_hands()
        hand = self.rng.choices(hands, weights=[combos(h) for h in hands])[0]
        return Spot(chart.id, chart.label, position, hand, chart.spot)

    def _from_cards(self, cards, filt: Filter) -> Spot | None:
        for card in cards:
            chart_id, position, hand = card.key.split("|")
            chart = self.charts.get(chart_id)
            if chart is None:
                continue
            spot = Spot(chart_id, chart.label, position, hand, chart.spot)
            if filt.allows(spot):
                return spot
        return None

    def next_spot(self, filt: Filter = Filter()) -> Spot:
        if filt.mode in ("struggling", "due"):
            # A narrowed mode draws only from history; nothing fresh is
            # sampled behind it, so an empty mode says so.
            if filt.mode == "struggling":
                cards = [c for c in self.progress.cards.values()
                         if c.lapses >= 2 and c.key.count("|") == 2]
                self.rng.shuffle(cards)
            else:
                cards = [c for c in self.progress.due() if c.key.count("|") == 2]
            spot = self._from_cards(cards, filt)
            if spot is None:
                raise NoSpots(f"no {filt.mode} spot matches this filter")
            self.session.seen[spot.key] = self.session.seen.get(spot.key, 0) + 1
            return spot
        # Due cards come first: re-testing something you just got wrong is
        # worth more than a fresh question from the leak distribution.
        due = [c for c in self.progress.due() if c.key.count("|") == 2]
        if due and self.rng.random() < DUE_SHARE:
            for card in due:
                chart_id, position, hand = card.key.split("|")
                chart = self.charts.get(chart_id)
                if chart is None:
                    continue
                spot = Spot(chart_id, chart.label, position, hand, chart.spot)
                if filt.allows(spot):
                    self.session.seen[spot.key] = self.session.seen.get(spot.key, 0) + 1
                    return spot
        weights = [(s, w) for s, w in self._weights if filt.allows(s)]
        if weights and self.rng.random() < LEAK_SHARE:
            spots, masses = zip(*weights)
            spot = self.rng.choices(spots, weights=masses)[0]
        else:
            spot = self._uniform_spot(filt)
        self.session.seen[spot.key] = self.session.seen.get(spot.key, 0) + 1
        return spot

    def your_history(self, spot: Spot) -> tuple[float | None, float]:
        """Hero's own frequency for this exact spot, and how often it came up."""
        chart = self.charts[spot.chart_id]
        key = (spot.chart_id, spot.position)
        if key not in self._observed:
            self._observed[key] = observed_range(
                self.obs, spot.position, chart.spot, now=self.now, half_life=1e9)
        seen = self._observed[key]
        n = sum(1 for o in self.obs
                if o.position == spot.position and o.spot == chart.spot and o.hand == spot.hand)
        return (seen.freq(spot.hand) if n else None), n

    def grade(self, spot: Spot, action: str) -> Verdict:
        chart = self.charts[spot.chart_id]
        dist = chart.prescribed(spot.position, spot.hand)
        best = max(dist, key=lambda a: dist[a])
        # Two live options mean the chart itself is indifferent; accept either.
        live = tuple(a for a in dist if dist[a] > MIX_BAND)
        accepted = live if len(live) > 1 else (best,)
        correct = action in accepted

        # `your_freq` stays a RAISE frequency: observed_range counts "Raises to"
        # and hero's measured baselines were derived that way. So the note is
        # compared against the chart's raise leg, never against `chart_freq`.
        raise_freq = dist.get("raise", 0.0)
        yours, n = self.your_history(spot)
        if yours is None:
            note = "you have not had this spot in your history"
        elif abs(yours - raise_freq) < 0.25:
            note = f"your raising here matches the chart ({yours:.0%} over {n} hands)"
        else:
            way = "more" if yours > raise_freq else "less"
            note = (f"you raise this {yours:.0%} of the time ({n} hands) — "
                    f"{way} than the chart's {raise_freq:.0%}")

        self.session.asked += 1
        self.session.right += correct
        self.progress.review("|".join(spot.key), correct)
        return Verdict(correct, len(accepted) > 1, dist, best, accepted,
                       dist.get(action, 0.0), yours, n, note)
