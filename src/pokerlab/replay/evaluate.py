"""Put the solver's answer beside the line hero actually took.

The output is deliberately not a score. It is three answers to the same
question, computed against a tight, a measured and a loose villain range, and
the finding is usually whether they agree. When they do, the range assumption
did not matter and the verdict is worth something. When they do not, that IS
the result: the decision was never determined by the cards, only by a read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..ranges.chart import chart_for
from ..ranges.notation import Range, canonical
from ..solver.bridge import Solution, Spot, SolverError, available, solve
from ..stats.ranges import Observation, observed_range
from .node import Decision
from .pool import Pool, spot_of
from .villain import Assignment, Layer, band, narrow

#: An EV gap smaller than this share of the pot is not a mistake, it is solver
#: noise plus the arbitrariness of the tree's bet sizes.
TOLERANCE_PCT_POT = 0.02
#: Hero's own range must contain the hand hero actually held, or there is
#: nothing to grade. Forced in at this weight when the measured range omits it.
FORCED_WEIGHT = 1.0
#: Largest tree the replayer will allocate. A flop node with 100bb behind in a
#: limped pot reaches many gigabytes; without a ceiling the machine pages
#: instead of answering. Measured 2026-09-12: the median c-bet spot sits at
#: SPR 13 and its full tree needs 2.5-4.8 GB, solving in 30-60 s; at 2 GB two
#: thirds of them were being answered on a retreat instead. One solve at a
#: time on an 18 GB machine.
MEMORY_BUDGET = 6_000_000_000

#: Betclic NL5, measured from the hands on file: 5.5% of the pot on flopped
#: pots, never more than €1.00. Rake-free solves overstate every marginal
#: bet and call, at a table where the house takes 30 bb/100.
RAKE_RATE = 0.055
RAKE_CAP = 100

#: Ordered retreats when a tree will not fit. Each one makes the model coarser
#: in a different way, so the one that was used is reported with the verdict
#: rather than folded silently into the answer. The flop raise is kept as long
#: as possible: removing it moved solver c-bet frequency by up to 50 points on
#: real spots, where dropping turn and river raises rarely moved it.
FALLBACKS = (
    (lambda s: {}, ""),
    (lambda s: {"raise_sizes": {"flop": s.raise_sizes.get("flop") or ["2.5x"],
                                "turn": [], "river": []}},
     "no turn or river raises in the tree — the flop raise is kept, later "
     "streets are bet, call or fold only"),
    (lambda s: {"raise_sizes": {"flop": [], "turn": [], "river": []}},
     "no raises in the tree — hero and villain can bet, call or fold, but the "
     "option to raise is absent, which understates aggressive lines"),
    (lambda s: {"raise_sizes": {"flop": [], "turn": [], "river": []}, "compress_memory": True},
     "no raises in the tree, and 16-bit compressed storage — coarser strategy "
     "numbers on top of the missing raise branches"),
)


@dataclass(slots=True)
class Read:
    """One pass at the decision, against one villain range."""

    width: str                     # "tight" | "base" | "loose"
    assignment: Assignment
    solution: Solution | None = None
    error: str | None = None
    hero_action: str | None = None       # as the solver names it
    freq: dict[str, float] = field(default_factory=dict)
    ev: dict[str, float] = field(default_factory=dict)
    substituted: bool = False
    degraded: str = ""             # how the tree was cut down to fit, if it was
    digest: str = ""               # the cache key of the solve this read came from

    @property
    def best(self) -> str | None:
        return max(self.ev, key=self.ev.get) if self.ev else None

    @property
    def ev_loss(self) -> float | None:
        """Chips hero's actual action gave up against the node's best action."""
        if not self.ev or self.hero_action not in self.ev:
            return None
        return max(self.ev.values()) - self.ev[self.hero_action]

    def approved(self, pot: int) -> bool | None:
        loss = self.ev_loss
        if loss is None:
            return None
        return loss <= TOLERANCE_PCT_POT * max(pot, 1)

    @property
    def hero_freq(self) -> float | None:
        return self.freq.get(self.hero_action) if self.hero_action else None


@dataclass(slots=True)
class Verdict:
    decision: Decision
    reads: list[Read]
    hero_range: Range
    hero_range_note: str
    bb: int

    @property
    def usable(self) -> list[Read]:
        return [r for r in self.reads if r.ev]

    @property
    def stable(self) -> bool | None:
        """Does the verdict survive every plausible villain range?"""
        calls = [r.approved(self.decision.starting_pot) for r in self.usable]
        return None if not calls or None in calls else len(set(calls)) == 1

    @property
    def headline(self) -> str:
        usable = self.usable
        if not usable:
            return "not graded"
        approved = [r.approved(self.decision.starting_pot) for r in usable]
        if self.stable is False:
            return "depends on the range"
        return "holds up" if approved[0] else "loses EV"

    def loss_bb(self, read: Read) -> float | None:
        loss = read.ev_loss
        return None if loss is None else loss / self.bb

    @property
    def trustworthy(self) -> bool:
        return bool(self.usable) and all(
            r.solution is not None and r.solution.trustworthy for r in self.usable)

    @property
    def warnings(self) -> list[str]:
        seen: list[str] = []
        for r in self.reads:
            if r.degraded and r.degraded not in seen:
                seen.append(f"tree reduced to fit memory: {r.degraded}")
            for w in (r.solution.warnings if r.solution else []):
                if w not in seen:
                    seen.append(w)
            if r.error and r.error not in seen:
                seen.append(r.error)
        return seen


def _match(step: str, names: list[str]) -> str | None:
    """Resolve hero's action against the names the solved node offers.

    Mirrors the resolution the Rust side does for path steps, so what gets
    graded is the same node the path walked to.
    """
    if step in names:
        return step
    verb, _, wanted = step.partition("~")
    if verb in names:
        return verb
    if not wanted:
        return None
    target = float(wanted)
    best, gap = None, float("inf")
    for name in names:
        head, _, amount = name.partition(" ")
        if not amount:
            continue
        if head != verb and not (head == "allin" and verb in ("bet", "raise")):
            continue
        d = abs(float(amount) - target)
        if d < gap:
            best, gap = name, d
    return best


def hero_range(observations: list[Observation], decision: Decision, hand,
               now: datetime | None = None) -> tuple[Range, str]:
    """The range hero arrives at this node with -- measured from hero's own play.

    This is the one range in the whole tool that is not an assumption. Hero's
    cards are in the file for every hand, not just the ones that reached
    showdown, so there is no selection bias to correct for: it is simply what
    hero did with each hand in this spot.
    """
    from .node import _villain_preflop

    seat = hand.seat_of[hand.hero]
    spot, verb, opener = _villain_preflop(hand, hand.hero)
    key = f"{seat.position}_vs_{opener}" if spot == "vs_open" and opener else seat.position
    rng = observed_range(observations, key, spot, action_verbs=(verb,), now=now)
    note = (f"measured from hero's own {spot} decisions as {key} "
            f"({rng.pct:.0f}% of hands, recency-weighted)")

    if rng.n_combos < 40:
        chart = chart_for(spot, key)
        fallback = chart.action_range(key, "raise") if chart else Range()
        if fallback.n_combos:
            rng, note = fallback, (
                f"too few of hero's own {spot} decisions as {key} to form a range; "
                f"fell back to {chart.id} ({chart.confidence})")

    # Hero held what hero held. A range that excludes it grades nothing.
    hero_hand = canonical(list(hand.hero_cards))
    if rng.freq(hero_hand) <= 0:
        rng = Range({**rng.weights, hero_hand: FORCED_WEIGHT})
        note += (f"; {hero_hand} was not in it and has been forced in — hero held it, "
                 f"so the measured range is incomplete here")
    return rng, note


#: Strongest-first, for picking what a villain's street amounted to when they
#: acted more than once on it. Raising after calling is a raise.
_STRENGTH = ("Raises to", "Bets", "Calls", "Checks", "Folds")


def _villain_range(pool: Pool, decision: Decision, hand, width: str) -> Assignment:
    """The villain's preflop range, narrowed once per street they have played.

    How much it narrows depends on what they DID, not merely on their having
    survived: at NL5 a villain calls 49% of flop bets and raises 7.9% of them,
    so scoring a raise as "did not fold" would leave a raiser's range six times
    too wide -- which is exactly what the first version of this did.
    """
    b = band(pool, decision.villain, decision.villain_position,
             decision.villain_spot, decision.villain_verb, opener=decision.opener,
             path=decision.villain_path[:-1])
    assignment = {"tight": b.tight, "base": b.base, "loose": b.loose}[width]

    order = ["FLOP", "TURN", "RIVER"]
    frames = hand.frames()
    for street in order[: order.index(decision.street)]:
        verbs = [f.action.verb for f in frames
                 if f.action.street == street and f.action.name == decision.villain]
        if not verbs:
            continue
        strongest = min(verbs, key=lambda v: _STRENGTH.index(v)
                        if v in _STRENGTH else len(_STRENGTH))
        keep, n, what, source = pool.keep_for(decision.villain, street, strongest)
        if keep is None:
            continue
        # Stress the narrowing by the same factor as the width. Otherwise the
        # band widens the part that is measured and leaves untouched the part
        # that is assumed -- and here the assumed part usually dominates.
        keep = min(1.0, keep * assignment.width_scale)
        if keep >= 1.0:
            continue
        if not assignment.range.weights:
            break                 # nothing left to narrow; the read will say so
        board = hand.board_at(street)
        assignment = narrow(assignment, board, keep, dead=hand.hero_cards)
        assignment.layers[-1] = Layer(
            "narrowing", "assumed",
            f"{decision.villain} {what} on the {street.lower()}; kept the strongest "
            f"{keep:.0%} by equity on {' '.join(board)}, from a measured "
            f"{keep / max(assignment.width_scale, 1e-6):.0%} for that action "
            f"({source}), stressed by this band member", n)
    return assignment


def evaluate(pool: Pool, observations: list[Observation], hand, decision: Decision,
             widths: tuple[str, ...] = ("tight", "base", "loose"),
             max_iterations: int = 200, timeout: int = 300,
             now: datetime | None = None) -> Verdict:
    """Solve one decision against the band and grade hero's actual action."""
    if not decision.solvable:
        raise ValueError(decision.reason)
    if not available():
        raise SolverError("solver binary not built — cd solver-cli && cargo build --release")

    hrange, note = hero_range(observations, decision, hand, now=now)
    hero_step = decision.hero_step()
    hero_is_oop = _hero_is_oop(hand, decision)
    reads: list[Read] = []

    for width in widths:
        assignment = _villain_range(pool, decision, hand, width)
        if not assignment.range.weights:
            reads.append(Read(width, assignment, error="no range could be assigned"))
            continue

        spot = build_spot(hrange, assignment.range, decision, hero_is_oop,
                          max_iterations=max_iterations)
        sol, degraded, error, digest = _solve_within_budget(spot, timeout)
        if sol is None:
            reads.append(Read(width, assignment, error=error))
            continue

        action = _match(hero_step, sol.root_actions)
        reads.append(Read(
            width, assignment, solution=sol, hero_action=action,
            freq=sol.strategy_for(decision.hero_combo),
            ev=sol.ev_for(decision.hero_combo),
            substituted=any("was played as" in w for w in sol.warnings),
            degraded=degraded, digest=digest,
        ))

    return Verdict(decision, reads, hrange, note, bb=hand.bb)


def build_spot(hrange: Range, vrange: Range, decision: Decision, hero_is_oop: bool,
               max_iterations: int = 200, action_path: tuple[str, ...] | None = None) -> Spot:
    """The solver's question for this decision, exactly as the grader asks it."""
    hero_spec, villain_spec = hrange.to_spec(), vrange.to_spec()
    return Spot(
        oop_range=hero_spec if hero_is_oop else villain_spec,
        ip_range=villain_spec if hero_is_oop else hero_spec,
        board=decision.board,
        starting_pot=decision.starting_pot,
        effective_stack=decision.effective_stack,
        bet_sizes=decision.bet_sizes, raise_sizes=decision.raise_sizes,
        max_iterations=max_iterations,
        action_path=decision.action_path if action_path is None else action_path,
        rake_rate=RAKE_RATE, rake_cap=RAKE_CAP,
    )


@dataclass(slots=True)
class Reconstruction:
    """The ranges a stored verdict was solved on, rebuilt without solving.

    Hero's range is recency-weighted to *now* rather than to the moment the
    grader ran, so it can drift a little from what was solved; the villain's
    is deterministic. `solution` is the cached solve when the rebuilt spot
    matches one on disk, else None — the page then shows ranges without the
    solver's per-combo strategy rather than solving again. A verdict that
    recorded its digest is found directly; older ones are matched by spot and
    the villain width they recorded.
    """

    hero_range: Range
    hero_note: str
    assignment: Assignment
    hero_is_oop: bool
    spot: Spot
    solution: Solution | None


def reconstruct(pool: Pool, observations: list[Observation], hand, decision: Decision,
                width: str = "base", degraded: str = "", digest: str = "",
                villain_pct: float | None = None) -> Reconstruction:
    from ..solver.bridge import cached
    from dataclasses import replace

    hrange, note = hero_range(observations, decision, hand)
    assignment = _villain_range(pool, decision, hand, width)
    oop = _hero_is_oop(hand, decision)
    spot = build_spot(hrange, assignment.range, decision, oop)
    # The verdict says which retreat it was solved on; ask for that tree.
    overrides = next((o for o, desc in FALLBACKS if desc == degraded), FALLBACKS[0][0])
    spot = replace(spot, **overrides(spot))
    sol = None
    if digest:
        sol = _load_digest(digest)
    if sol is None and assignment.range.weights:
        sol = cached(spot)
    if sol is None:
        from ..solver.bridge import find_cached
        sol = find_cached(decision.board, decision.starting_pot, decision.effective_stack,
                          decision.action_path, villain_pct, villain_is_oop=not oop)
    return Reconstruction(hrange, note, assignment, oop, spot, sol)


def _load_digest(digest: str) -> Solution | None:
    from ..solver.bridge import _load
    return _load(digest)


def _solve_within_budget(spot: Spot, timeout: int) -> tuple[Solution | None, str, str, str]:
    """Solve, retreating to a coarser tree rather than exhausting memory.

    Returns the solution, a description of the retreat if one was needed, the
    error if every step failed, and the cache digest of the tree that answered.
    A coarser tree is a different question from the one asked, so the
    description travels with the answer.
    """
    from dataclasses import replace

    last = ""
    for overrides, description in FALLBACKS:
        attempt = replace(spot, max_memory_bytes=MEMORY_BUDGET, **overrides(spot))
        try:
            return solve(attempt, timeout=timeout), description, "", attempt.digest
        except SolverError as exc:
            last = str(exc)
            if "budget" not in last:
                break        # not a size problem; a coarser tree will not help
    return None, "", last, ""


def _hero_is_oop(hand, decision: Decision) -> bool:
    """Who acts first on this street is who the solver calls out of position."""
    for f in hand.frames():
        if f.action.street == decision.street:
            return f.action.is_hero
    return False
