"""All-in equity: were you ahead when the money went in?

Separates "did I win" from "should I have won" — the only way to tell running
bad from playing bad. Requires the solver binary; every equity here is
computed, none estimated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..parse.betclic import parse_hand
from ..parse.corpus import NL5, collect, default_sources
from ..parse.model import Street
from ..ranges.notation import RANKS
from ..solver.bridge import Spot, SolverError, available, solve

_ORD = {r: i for i, r in enumerate(RANKS)}
_STREETS = [Street.PREFLOP, Street.FLOP, Street.TURN, Street.RIVER]


def _combo(cards: list[str]) -> str:
    """postflop-solver requires the higher rank first."""
    return "".join(sorted(cards, key=lambda c: (_ORD[c[0]], c[1])))


@dataclass(slots=True)
class AllIn:
    hand_id: str
    street: str
    hero: list[str]
    villain: list[str]
    board: tuple[str, ...]
    equity: float
    won: bool
    net_bb: float


@dataclass(slots=True)
class AllInSummary:
    spots: list[AllIn]

    @property
    def n(self) -> int:
        return len(self.spots)

    @property
    def avg_equity(self) -> float:
        return sum(s.equity for s in self.spots) / self.n if self.n else 0.0

    @property
    def expected_wins(self) -> float:
        return sum(s.equity for s in self.spots)

    @property
    def actual_wins(self) -> int:
        return sum(1 for s in self.spots if s.won)

    @property
    def luck(self) -> float:
        return self.actual_wins - self.expected_wins

    @property
    def sigmas(self) -> float:
        var = sum(s.equity * (1 - s.equity) for s in self.spots)
        return self.luck / math.sqrt(var) if var > 0 else 0.0


def collect_allins(game: str = NL5) -> AllInSummary:
    """Hands where hero was all-in postflop and exactly one villain showed.

    Multiway all-ins are excluded because equity against two ranges is not
    the same question, and all-ins that never reached showdown are absent by
    construction — so read this as a sample, not as every stack-off.
    """
    if not available():
        raise SolverError("solver binary not built — cd solver-cli && cargo build --release")

    out: list[AllIn] = []
    for block in collect(default_sources(), games=(game,)).values():
        h = parse_hand(block)
        hero = h.hero
        if not hero or hero.name not in h.shown or len(h.shown) != 2:
            continue
        allins = [a for a in h.actions if a.all_in]
        if not any(a.name == hero.name for a in allins):
            continue
        idx = min((_STREETS.index(a.street) for a in allins), default=0)
        if idx == 0:
            continue  # preflop all-in: no board to evaluate against
        board = tuple(c for s in _STREETS[1:idx + 1] for c in h.boards.get(s, []))
        if len(board) not in (3, 4):
            continue
        villain = next(n for n in h.shown if n != hero.name)
        try:
            eq = solve(Spot(
                oop_range=_combo(h.shown[hero.name]), ip_range=_combo(h.shown[villain]),
                board=board, starting_pot=100, effective_stack=1,
                bet_sizes={k: [] for k in ("flop", "turn", "river")},
                raise_sizes={k: [] for k in ("flop", "turn", "river")},
                max_iterations=1), cache=False).all_in_equity
        except SolverError:
            continue
        out.append(AllIn(
            h.hand_id, _STREETS[idx].value, h.shown[hero.name], h.shown[villain], board,
            eq, h.collected.get(hero.name, 0) > 0, h.net(hero.name) / h.bb))
    return AllInSummary(out)
