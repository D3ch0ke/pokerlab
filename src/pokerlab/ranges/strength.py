"""A measured ordering of the 169 starting hands.

Cutting a range at a width -- "the pool defends 43% of hands here" -- needs an
ordering to cut along. This module provides one, and is explicit about the two
different things it is made of:

* **Equity vs a random hand**, measured by Monte Carlo with a fixed seed. This
  is a real number, but it is *not* playability: it ranks 22 above 76s because
  a pair beats two overcards at showdown, while every published chart plays
  76s in spots it folds 22. Equity alone would build ranges no human plays.

* **Chart nesting**, the tightest position that still opens the hand in
  ``rfi_6max_100bb``. That encodes playability judgement, but only down to
  the widest range any chart covers (BTN, ~44%), and only in four tiers.

So the ordering is the pair, in that order of precedence: chart tier first,
equity within a tier and for everything the charts never reach. Neither half
is asserted -- the equity is computed here and the tiers are read out of a
chart whose provenance is recorded. The weakness is the seam between them,
and it sits where it does least harm: below the widest chart, in the region
no reference covers at all.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from functools import cache
from itertools import combinations
from pathlib import Path

from .notation import RANKS, all_hands, deal_combos

DATA = Path(__file__).parent / "strength.json"
TRIALS = 8000
SEED = 20260910

#: Widest first: every position in the RFI chart, tightest to loosest.
_TIERS = ("UTG", "HJ", "CO", "BTN")

_RANK_ID = {r: 12 - i for i, r in enumerate(RANKS)}   # A=12 .. 2=0
_SUIT_ID = {s: i for i, s in enumerate("cdhs")}


def _card(text: str) -> int:
    """'Ah' -> a 0..51 integer, rank in the high bits."""
    return _RANK_ID[text[0]] * 4 + _SUIT_ID[text[1]]


def _straight(mask: int) -> int:
    """Highest card of a 5-straight in a rank bitmask, or -1."""
    for hi in range(12, 3, -1):
        need = 0
        for k in range(hi, hi - 5, -1):
            need |= 1 << k
        if mask & need == need:
            return hi
    if mask & (1 << 12) and mask & 0xF == 0xF:   # the wheel, A5432
        return 3
    return -1


def evaluate(cards: tuple[int, ...]) -> tuple:
    """Rank a 5-to-7 card holding. Bigger tuples beat smaller ones."""
    ranks = [c >> 2 for c in cards]
    counts = [0] * 13
    for r in ranks:
        counts[r] += 1
    suits = [0] * 4
    for c in cards:
        suits[c & 3] += 1

    flush = next((s for s in range(4) if suits[s] >= 5), -1)
    if flush >= 0:
        fr = sorted((c >> 2 for c in cards if c & 3 == flush), reverse=True)
        mask = 0
        for r in fr:
            mask |= 1 << r
        sf = _straight(mask)
        return (8, sf) if sf >= 0 else (5, tuple(fr[:5]))

    mask = 0
    for r in range(13):
        if counts[r]:
            mask |= 1 << r
    straight = _straight(mask)
    groups = sorted(((counts[r], r) for r in range(13) if counts[r]), reverse=True)

    if groups[0][0] == 4:
        kicker = max(r for _, r in groups if r != groups[0][1])
        return (7, (groups[0][1], kicker))
    if groups[0][0] == 3 and len(groups) > 1 and groups[1][0] >= 2:
        return (6, (groups[0][1], groups[1][1]))
    if straight >= 0:
        return (4, straight)
    if groups[0][0] == 3:
        return (3, (groups[0][1], *sorted((r for c, r in groups if c == 1), reverse=True)[:2]))
    pairs = [r for c, r in groups if c == 2]
    if len(pairs) >= 2:
        kicker = max(r for _, r in groups if r not in pairs[:2])
        return (2, (pairs[0], pairs[1], kicker))
    if len(pairs) == 1:
        return (1, (pairs[0], *sorted((r for c, r in groups if c == 1), reverse=True)[:3]))
    return (0, tuple(sorted(ranks, reverse=True)[:5]))


def equity_vs_random(hand: str, trials: int = TRIALS, seed: int = SEED) -> float:
    """Share of the pot this hand takes against one uniformly random hand.

    Ties count a half, as they do at the table. Every canonical hand is drawn
    from the same seeded stream, so the *relative* ordering is far more
    precise than each hand's own standard error suggests.
    """
    hero = tuple(_card(c) for c in deal_combos(hand)[0])
    rng = random.Random(seed)
    deck = [c for c in range(52) if c not in hero]
    won = 0.0
    for _ in range(trials):
        draw = rng.sample(deck, 9)
        board = tuple(draw[:5])
        villain = tuple(draw[5:7])
        mine, theirs = evaluate(hero + board), evaluate(villain + board)
        won += 1.0 if mine > theirs else 0.5 if mine == theirs else 0.0
    return won / trials


@cache
def _chart_tier() -> dict[str, int]:
    """Tightest RFI position that opens each hand; 4 means no chart opens it."""
    from .chart import load
    chart = load("rfi_6max_100bb")
    out: dict[str, int] = {}
    for hand in all_hands():
        out[hand] = next(
            (i for i, pos in enumerate(_TIERS) if chart.action_range(pos, "raise").freq(hand) > 0),
            len(_TIERS),
        )
    return out


def build(trials: int = TRIALS, seed: int = SEED) -> dict:
    """Recompute the equity table. Slow (tens of seconds) and rarely needed."""
    return {
        "trials": trials, "seed": seed,
        "source": "Monte Carlo equity against one uniformly random hand, "
                  "ties split. Computed by ranges/strength.py, not copied "
                  "from a published table.",
        "equity": {h: round(equity_vs_random(h, trials, seed), 5) for h in all_hands()},
    }


@cache
def equities() -> dict[str, float]:
    if not DATA.exists():
        raise FileNotFoundError(
            f"{DATA} is missing. Regenerate it with: "
            f"python -m pokerlab.ranges.strength")
    return json.loads(DATA.read_text())["equity"]


@cache
def provenance() -> dict:
    raw = json.loads(DATA.read_text())
    return {k: v for k, v in raw.items() if k != "equity"}


@dataclass(frozen=True, slots=True)
class Ranked:
    hand: str
    rank: int      # 0 = strongest
    tier: int      # tightest RFI position opening it; 4 = no chart does
    equity: float


@cache
def ordering() -> tuple[Ranked, ...]:
    """All 169 hands, strongest first, by chart tier then measured equity."""
    tiers, eq = _chart_tier(), equities()
    ordered = sorted(all_hands(), key=lambda h: (tiers[h], -eq[h]))
    return tuple(Ranked(h, i, tiers[h], eq[h]) for i, h in enumerate(ordered))


@cache
def rank_of() -> dict[str, int]:
    return {r.hand: r.rank for r in ordering()}


def top_by_combos(target: float, exclude: frozenset[str] = frozenset()) -> list[str]:
    """The strongest hands whose combos total roughly `target`.

    Stops at the hand that first reaches the target rather than overshooting
    past it, so a 43% range holds about 43% of combos and not 47%.
    """
    from .notation import combos
    out, total = [], 0.0
    for r in ordering():
        if r.hand in exclude:
            continue
        if total >= target:
            break
        out.append(r.hand)
        total += combos(r.hand)
    return out


def equity_on_board(cards: tuple[str, ...], board: tuple[str, ...],
                    trials: int = 400, seed: int = SEED) -> float:
    """A specific holding's equity against a random hand on a specific board.

    This is how a range is narrowed after the flop: by what a hand is actually
    worth on the cards that are out, not by how it ranked before them. It
    values draws roughly the way a caller does, which preflop rank cannot.

    Runouts are enumerated exactly on the river and turn; only the flop, where
    there are 990 of them per combo, falls back to sampling.
    """
    hero = tuple(_card(c) for c in cards)
    known = hero + tuple(_card(c) for c in board)
    if len(set(known)) != len(known):
        raise ValueError(f"{cards} collides with the board {board}")
    deck = [c for c in range(52) if c not in known]
    to_come = 5 - len(board)

    if to_come <= 1:
        won, total = 0.0, 0
        for extra in combinations(deck, to_come):
            full = tuple(_card(c) for c in board) + extra
            rest = [c for c in deck if c not in extra]
            for villain in combinations(rest, 2):
                mine, theirs = evaluate(hero + full), evaluate(villain + full)
                won += 1.0 if mine > theirs else 0.5 if mine == theirs else 0.0
                total += 1
        return won / total if total else 0.0

    rng = random.Random(seed)
    board_ids = tuple(_card(c) for c in board)
    won = 0.0
    for _ in range(trials):
        draw = rng.sample(deck, to_come + 2)
        full = board_ids + tuple(draw[:to_come])
        villain = tuple(draw[to_come:to_come + 2])
        mine, theirs = evaluate(hero + full), evaluate(villain + full)
        won += 1.0 if mine > theirs else 0.5 if mine == theirs else 0.0
    return won / trials


if __name__ == "__main__":
    import sys
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else TRIALS
    DATA.write_text(json.dumps(build(trials), indent=1) + "\n")
    print(f"wrote {DATA} ({trials} trials, seed {SEED})")
