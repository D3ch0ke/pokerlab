"""Assigning a villain a range at a decision node.

This is the open problem in the whole tool. Hero's cards are known; the
villain's are not, and a solve is only as good as the ranges fed into it. The
answer here is not to pick the "right" range -- there isn't one -- but to
build the range out of layers whose provenance is separately recorded, and
then to report the decision across a BAND of widths rather than at a point.

The layers, strongest evidence first:

1. **Width, measured.** ``pool.Cell.frequency`` -- how often the pool takes
   this action here, over every villain decision on file. A player is dealt a
   uniformly random hand, so this frequency IS the width of their range.
   Where the specific villain has enough of their own spots, their frequency
   is used instead of the pool's.
2. **Top of range, observed.** Hands villains were actually shown holding in
   this node. Real cards, but a biased sample: showdowns over-represent hands
   that flopped well. Used only to ADD hands, never to exclude them.
3. **Filler, assumed.** The remaining width taken in order from
   ``ranges.strength`` -- the chart's own tiers first, raw equity below them.
   This is the weakest layer and the one the band exists to stress.

What the band does NOT test: composition at a fixed width. Two ranges that are
both 40% wide but built differently can still disagree, and varying the width
does not explore that. Where the verdict is close, that limitation matters
more than the band does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..ranges.chart import Chart, chart_for
from ..ranges.notation import Range, canonical, combos, deal_combos
from ..ranges.strength import equity_on_board, ordering, rank_of
from ..solver.bridge import available as solver_available, equity_table
from ..stats.core import MIN_N
from .pool import Cell, Pool, cell_key

TOTAL_COMBOS = 1326.0
#: Allowance for the ordering assumption, as a share of the measured width.
#: Not a confidence interval -- the frequency is measured far more precisely
#: than this. It is a deliberate, stated judgement about layer 3.
ORDERING_ALLOWANCE = 0.25
#: Below this many shown hands the observed layer is recorded but not leaned on.
MIN_SHOWN = 10
#: Showdowns at which observation earns half the range; the rest comes from
#: the ordering. A stated judgement about how fast thin evidence should count,
#: not a measured quantity.
OBSERVED_HALF = 100.0


@dataclass(frozen=True, slots=True)
class Layer:
    name: str          # "width" | "observed" | "filler" | "narrowing"
    method: str        # "measured" | "observed" | "reference" | "assumed"
    detail: str
    n: int | None = None

    def __str__(self) -> str:
        n = f", n={self.n:,}" if self.n is not None else ""
        return f"{self.name}: {self.detail} [{self.method}{n}]"


@dataclass(slots=True)
class Assignment:
    """One villain range, with every layer that went into it recorded."""

    villain: str
    label: str                       # human-readable node, e.g. "BB vs BTN open, calls"
    range: Range
    layers: list[Layer] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    measured_width: float | None = None    # the frequency the width came from
    width_scale: float = 1.0
    """How far this assignment sits from the measured width.

    Carried so that everything downstream can be stressed by the same factor.
    A band that widened only the preflop range while a postflop narrowing did
    the real work would report a stability it had not tested.
    """

    @property
    def width_pct(self) -> float:
        return self.range.pct

    @property
    def confidence(self) -> str:
        """The weakest layer governs. Never better than the filler allows."""
        methods = {layer.method for layer in self.layers}
        if "assumed" in methods:
            return "low"
        if "reference" in methods:
            return "reference-low"
        if self.measured_width is None:
            return "low"
        return "measured"

    def spec(self) -> str:
        return self.range.to_spec()


@dataclass(slots=True)
class Band:
    """The same node at three widths. The point is whether the answer moves."""

    tight: Assignment
    base: Assignment
    loose: Assignment

    def __iter__(self):
        return iter((("tight", self.tight), ("base", self.base), ("loose", self.loose)))


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval: sane at the extremes where normal-approx is not."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _build_range(target_combos: float, seed_hands: dict[str, int],
                 chart_range: Range | None, evidence: float = OBSERVED_HALF,
                 order: tuple[str, ...] | None = None) -> tuple[Range, list[str]]:
    """Fill `target_combos`: partly from what was seen, the rest from ordering.

    The trap this avoids: the number of DISTINCT hands seen at showdown is not
    the width of a range. A villain who opens the button 26% of the time is
    not opening a fixed 26% of hands -- they mix, so over 2,777 spots the union
    of hands they ever open is far wider than 26%. Counting distinct showdown
    hands as range membership produced a 33% range for a villain measured at
    13%, which is simply a contradiction.

    So counts are read as RELATIVE frequencies instead. A hand shown twice as
    often per combo as another is taken to be played about twice as often, and
    the measured width sets the scale. Even that only earns as much of the
    range as the sample deserves: with `shown_n` showdowns, observation
    determines `shown_n / (shown_n + evidence)` of the width and the ordering
    fills the rest. At the half-way constant that is a third of the range from
    50 showdowns and two thirds from 200.
    """
    shown_n = sum(seed_hands.values())
    share = shown_n / (shown_n + evidence) if shown_n else 0.0
    weights: dict[str, float] = {}
    total = 0.0

    # Layer 2: observed, most-played first. Counts are per combo, so a pair
    # seen 6 times is not mistaken for being twice as likely as a suited hand
    # seen 4 times -- there are simply more ways to be dealt it.
    surprises = []
    by_rate = sorted(seed_hands.items(), key=lambda kv: -kv[1] / combos(kv[0]))
    obs_target = share * target_combos
    for hand, _count in by_rate:
        if total >= obs_target:
            break
        take = min(1.0, (obs_target - total) / combos(hand))
        weights[hand] = round(take, 3)
        total += combos(hand) * take
        if chart_range is not None and chart_range.freq(hand) == 0:
            surprises.append(hand)

    # Layer 3: the rest. Chart membership leads, because it encodes playability
    # that raw equity does not; below the widest chart range the filler order
    # takes over — by default the strength ordering, or one the caller built
    # from what the pool actually shows for this kind of action.
    filler = order if order is not None else tuple(r.hand for r in ordering())
    for pass_ in (0, 1):
        for hand in filler:
            if total >= target_combos:
                break
            have = weights.get(hand, 0.0)
            if have >= 1.0:
                continue
            in_chart = chart_range is not None and chart_range.freq(hand) > 0
            if pass_ == 0 and not in_chart:
                continue
            take = min(1.0 - have, (target_combos - total) / combos(hand))
            weights[hand] = round(have + take, 3)
            total += combos(hand) * take

    return Range({h: w for h, w in weights.items() if w > 0}), surprises


Step = tuple[str, str, str | None]      # (spot, verb, opener)


def _step_width(pool: Pool, villain: str, position: str, step: Step,
                prior: str | None) -> tuple[float | None, Cell, str]:
    """Measured frequency of one preflop decision, with the cell it came from.

    Falls back to the chart's width when the pool is too thin there, and says
    which in the returned provenance string.
    """
    spot, verb, opener = step
    key = cell_key(position, spot, opener, prior)
    pool_cell = pool.cell(key, spot, verb)
    own = pool.villain_cell(villain, key, spot, verb)
    cell = own or pool_cell
    if cell.frequency is not None:
        return cell.frequency, cell, "own" if own else "pool"
    chart_key = cell_key(position, spot, opener)
    chart = chart_for(spot, chart_key)
    rng = chart.action_range(chart_key, _chart_action(verb)) if chart else None
    if rng and rng.n_combos:
        return rng.n_combos / TOTAL_COMBOS, cell, f"chart:{chart.id}"
    return None, cell, ""


def assign(pool: Pool, villain: str, position: str, spot: str, verb: str,
           opener: str | None = None, width_scale: float = 1.0,
           path: tuple[Step, ...] = ()) -> Assignment:
    """Build one villain range for one preflop node.

    `(spot, verb, opener)` is the villain's LAST preflop decision and `path`
    the ones before it. A range is the product of every decision that led to
    it: a limper who called a raise holds P(limp) x P(call | limped, raised)
    of all hands, and an opener who called a 3-bet is not on their opening
    range at all. Composition comes from the last decision's showdowns.

    `width_scale` multiplies the measured frequency; the band uses it to walk
    the same node tighter and looser without changing anything else.
    """
    prior = path[-1][1] if path else None
    key = cell_key(position, spot, opener, prior)
    chart_key = cell_key(position, spot, opener)   # charts know nothing of chains
    label = _label(position, spot, verb, opener, path)

    pool_cell = pool.cell(key, spot, verb)
    own = pool.villain_cell(villain, key, spot, verb)
    cell: Cell = own or pool_cell
    layers: list[Layer] = []
    notes: list[str] = []

    freq = cell.frequency
    if freq is None:
        # Nothing measured. Fall back to the chart's own width, and say so.
        chart = chart_for(spot, chart_key)
        chart_range = chart.action_range(chart_key, _chart_action(verb)) if chart else None
        if chart_range and chart_range.n_combos:
            freq = chart_range.n_combos / TOTAL_COMBOS
            layers.append(Layer("width", "reference",
                                f"no measured frequency here (n={cell.opportunities}, "
                                f"below MIN_N={MIN_N}); took the width from "
                                f"{chart.id}", cell.opportunities))
        else:
            notes.append(
                f"Neither the pool ({cell.opportunities} spots) nor any chart covers "
                f"{label}. No range can be assigned here and nothing below is graded.")
            return Assignment(villain, label, Range(), layers, notes)
    elif own is not None:
        layers.append(Layer(
            "width", "measured",
            f"{villain} takes this action {freq:.1%} of the time here "
            f"(pool: {pool_cell.frequency:.1%})" if pool_cell.frequency is not None
            else f"{villain} takes this action {freq:.1%} of the time here",
            cell.opportunities))
    else:
        layers.append(Layer("width", "measured",
                            f"the pool takes this action {freq:.1%} of the time here",
                            cell.opportunities))
        if pool.hands_seen(villain):
            notes.append(
                f"{villain} has only {pool.hands_seen(villain):,} decisions on file and "
                f"none of them make {MIN_N} in this exact node, so this is the pool's "
                f"width, not theirs.")

    # Earlier decisions in the same hand multiply the width. Each is measured
    # in its own cell, conditioned on what the villain had already done.
    for i, step in enumerate(path):
        s_freq, s_cell, how = _step_width(pool, villain, position, step,
                                          path[i - 1][1] if i else None)
        if s_freq is None:
            notes.append(
                f"Nothing measures {_label(position, *step)} ({s_cell.opportunities} spots) "
                f"and no chart covers it, so the range that led to {label} cannot be "
                f"built and nothing below is graded.")
            return Assignment(villain, label, Range(), layers, notes)
        layers.append(Layer(
            "width", "reference" if how.startswith("chart") else "measured",
            f"earlier in the hand, {_label(position, *step)}: "
            f"{s_freq:.1%} of hands{' (' + how.split(':')[1] + ')' if how.startswith('chart') else ''}",
            s_cell.opportunities))
        freq *= s_freq
    if path:
        layers.append(Layer("width", "measured",
                            f"width is the product of every decision: {freq:.1%} of all hands",
                            cell.opportunities))

    target = max(0.0, min(1.0, freq * width_scale)) * TOTAL_COMBOS

    seed = pool_cell.shown if pool_cell.shown_n >= MIN_SHOWN else {}
    chart = chart_for(spot, chart_key)
    chart_range = chart.action_range(chart_key, _chart_action(verb)) if chart else None
    rng, surprises = _build_range(target, seed, chart_range)

    # Layer 2, observed holdings. Sets composition within a width it never moves.
    if seed:
        share = pool_cell.shown_n / (pool_cell.shown_n + OBSERVED_HALF)
        layers.append(Layer(
            "observed", "observed",
            f"{len(seed)} distinct hands seen at showdown here, read as relative "
            f"frequencies and given {share:.0%} of the width",
            pool_cell.shown_n))
        notes.append(
            "Showdown holdings are biased toward hands that flopped well enough to keep "
            "paying, so they mark the TOP of a range and never its bottom. They set which "
            "hands are in it and in what proportion, never how wide it is.")
        if surprises:
            notes.append(
                "In the range only because villains were shown holding them here, not "
                "because any chart plays them: " + ", ".join(surprises[:12])
                + (f", and {len(surprises) - 12} more" if len(surprises) > 12 else ""))
    elif pool_cell.shown_n:
        layers.append(Layer("observed", "observed",
                            f"only {pool_cell.shown_n} showdowns here, too few to correct with",
                            pool_cell.shown_n))

    if chart_range and chart_range.n_combos:
        layers.append(Layer(
            "filler", "reference",
            f"remaining width taken from {chart.id} first (confidence: "
            f"{chart.confidence}), then by measured equity"))
        gap = chart_range.n_combos / TOTAL_COMBOS - (freq or 0)
        if abs(gap) > 0.08:
            notes.append(
                f"The chart plays {chart_range.pct:.0f}% here and the pool measurably "
                f"plays {freq:.1%}. The width used is the measured one; the chart only "
                f"orders which hands fill it.")
    else:
        layers.append(Layer("filler", "assumed",
                            "no chart covers this node; width filled purely by "
                            "measured equity vs a random hand"))

    return Assignment(villain, label, rng, layers, notes, measured_width=freq,
                      width_scale=width_scale)


def band(pool: Pool, villain: str, position: str, spot: str, verb: str,
         opener: str | None = None, allowance: float = ORDERING_ALLOWANCE,
         path: tuple[Step, ...] = ()) -> Band:
    """The node at three widths: measured, and either side of it.

    The spread is the wider of the sampling error in the frequency (usually
    small -- these are thousands of observations) and a flat allowance for the
    ordering being wrong, which is not measurable and is stated as a judgement.
    """
    base = assign(pool, villain, position, spot, verb, opener, path=path)
    if not base.range.weights or base.measured_width is None:
        return Band(base, base, base)

    key = cell_key(position, spot, opener, path[-1][1] if path else None)
    cell = pool.villain_cell(villain, key, spot, verb) or pool.cell(key, spot, verb)
    lo, hi = _wilson(cell.taken, cell.opportunities)
    p = base.measured_width

    tight_scale = min(lo / p, 1 - allowance) if p else 1.0
    loose_scale = max(hi / p, 1 + allowance) if p else 1.0

    return Band(
        assign(pool, villain, position, spot, verb, opener, width_scale=tight_scale, path=path),
        base,
        assign(pool, villain, position, spot, verb, opener, width_scale=loose_scale, path=path),
    )


def narrow(assignment: Assignment, board: tuple[str, ...], keep: float,
           dead: tuple[str, ...] = ()) -> Assignment:
    """Cut a range to its strongest `keep` share ON THIS BOARD.

    Ranking is by equity against a random hand on the cards that are out, not
    by preflop rank -- that is what lets a flush draw survive a continue and a
    small pair not. It still assumes villains continue with their best hands
    and nothing else, which is the crudest step in the whole chain and is
    tagged accordingly.
    """
    if not assignment.range.weights or keep >= 1.0:
        return assignment

    blocked = set(board) | set(dead)
    equity = _equities(assignment.range, board)
    scored: list[tuple[float, str, float]] = []
    for hand, weight in assignment.range.weights.items():
        live = [c for c in deal_combos(hand) if not blocked & set(c)]
        if not live:
            continue
        eq = sum(equity(c) for c in live) / len(live)
        scored.append((eq, hand, weight * len(live) / len(deal_combos(hand))))

    scored.sort(reverse=True)
    target = sum(combos(h) * w for _, h, w in scored) * keep
    weights: dict[str, float] = {}
    total = 0.0
    for eq, hand, weight in scored:
        if total >= target:
            break
        room = (target - total) / combos(hand)
        weights[hand] = round(min(weight, room), 3)
        total += combos(hand) * weights[hand]

    out = Assignment(
        assignment.villain, assignment.label, Range({h: w for h, w in weights.items() if w > 0}),
        list(assignment.layers), list(assignment.notes), assignment.measured_width,
        assignment.width_scale)
    out.layers.append(Layer(
        "narrowing", "assumed",
        f"kept the strongest {keep:.0%} by measured equity on {' '.join(board)}"))
    out.notes.append(
        "Postflop narrowing assumes villains continue with their strongest hands by "
        "board equity and fold the rest. Real players continue with draws they should "
        "fold and fold pairs they should not, so this is the crudest link in the chain.")
    return out


def _equities(rng: Range, board: tuple[str, ...]):
    """combo -> equity vs a random hand on this board.

    The engine enumerates every runout exactly in well under a second. The
    pure-Python evaluator it replaces took minutes on a river range -- 34
    million hand evaluations to rank 377 hands -- and only remains as the
    path for a machine without the binary.
    """
    if solver_available():
        from .node import combo as engine_name
        table = equity_table(rng.to_spec(), board)
        return lambda c: table.get(engine_name(c), 0.0)
    return lambda c: equity_on_board(c, board)


def _chart_action(verb: str) -> str:
    return {"Raises to": "raise", "Calls": "call", "Bets": "raise"}.get(verb, "fold")


def _label(position: str, spot: str, verb: str, opener: str | None,
           path: tuple[Step, ...] = ()) -> str:
    action = {"Raises to": "raises", "Calls": "calls", "Checks": "checks",
              "Folds": "folds", "Bets": "bets"}.get(verb, verb.lower())
    where = {"RFI": "first in", "vs_open": f"vs {opener or '?'} open",
             "vs_3bet": "vs a 3-bet", "vs_limp": "vs a limp"}[spot]
    if path:
        earlier = ", ".join(_label(position, *step) for step in path)
        return f"{earlier}, then {where}, {action}"
    return f"{position} {where}, {action}"
