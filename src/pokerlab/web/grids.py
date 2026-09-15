"""13x13 range grids painted with a range's weights and, when a solve is at
hand, the solver's action mix per hand.

One renderer for every page that shows a range: the drill's answer, the
replayer's verdict panel, the villain read. A grid without a solve shades
each hand by how much of it is in the range; with one, each cell is split
into bands — bet/raise, call, check, fold — in the solver's proportions,
so the whole strategy is visible at once and hero's hand sits in context.
"""

from __future__ import annotations

import html
from collections import defaultdict

from ..ranges.notation import Range, all_hands, canonical, grid_index
from ..solver.bridge import Solution

#: Solver action name -> the band it paints.
FAMILY = {"bet": "bet", "raise": "bet", "allin": "bet", "call": "call",
          "check": "check", "fold": "fold"}
#: Paint order within a cell, most aggressive first.
ORDER = ("bet", "call", "check", "fold")
COLOUR = {"bet": "var(--raise)", "call": "var(--call)", "check": "var(--check)",
          "fold": "var(--foldbg)"}


def family(action: str) -> str:
    return FAMILY.get(action.split()[0], action)


def strategy_by_hand(sol: Solution, rng: Range | None = None) -> dict[str, dict[str, float]]:
    """Action -> frequency per canonical hand, averaged over its dealt combos.

    Card removal drops some combos from the solve; the mean is over the combos
    the solver actually held, which is the strategy for that hand on this board.
    """
    acc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    n: dict[str, int] = defaultdict(int)
    for row in sol.root_strategy:
        combo = row["hand"]
        hand = canonical([combo[:2], combo[2:]])
        if rng is not None and rng.freq(hand) <= 0:
            continue
        n[hand] += 1
        for action, p in row["actions"].items():
            acc[hand][action] += p
    return {h: {a: v / n[h] for a, v in acts.items()} for h, acts in acc.items()}


def _bands(mix: dict[str, float]) -> str:
    by_family: dict[str, float] = defaultdict(float)
    for action, p in mix.items():
        by_family[family(action)] += p
    stops, at = [], 0.0
    for fam in ORDER:
        share = by_family.get(fam, 0.0)
        if share <= 0:
            continue
        stops.append(f"{COLOUR[fam]} {100 * at:.0f}% {100 * (at + share):.0f}%")
        at += share
    if at < 0.999:
        stops.append(f"{COLOUR['fold']} {100 * at:.0f}% 100%")
    return f"background:linear-gradient(90deg,{','.join(stops)})"


def mix_text(mix: dict[str, float], label=lambda a: a) -> str:
    return " · ".join(f"{label(a)} {100 * p:.0f}%"
                      for a, p in sorted(mix.items(), key=lambda kv: -kv[1]) if p >= 0.005)


def range_grid(rng: Range, strategy: dict[str, dict[str, float]] | None = None,
               highlight: str | None = None, label=lambda a: a, compact: bool = False) -> str:
    """The grid. `strategy` paints the solver's mix; without it, cells are
    shaded by range weight. `highlight` outlines one hand."""
    cells: dict[tuple[int, int], str] = {}
    for hand in all_hands():
        w = rng.freq(hand)
        mix = (strategy or {}).get(hand)
        if w <= 0:
            style, title, cls = "", f"{hand}: not in range", "out"
        elif mix:
            style = _bands(mix) + f";opacity:{0.35 + 0.65 * min(w, 1):.2f}"
            title = f"{hand} ({w:.0%} of combos): {mix_text(mix, label)}"
            cls = "lit"
        else:
            style = f"background:var(--accent);opacity:{0.15 + 0.6 * min(w, 1):.2f}"
            title = f"{hand}: {w:.0%} of combos in range"
            cls = "lit"
        if hand == highlight:
            cls += " here"
        cells[grid_index(hand)] = (
            f'<td class="{cls}" style="{style}" title="{html.escape(title)}">'
            f'{hand}</td>')
    rows = "".join("<tr>" + "".join(cells[(r, c)] for c in range(13)) + "</tr>"
                   for r in range(13))
    return f'<table class="grid rg{" sm" if compact else ""}">{rows}</table>'


def legend(strategy: bool) -> str:
    if strategy:
        names = {"bet": "bet / raise", "call": "call", "check": "check", "fold": "fold"}
        items = "".join(f'<span><i class="sw" style="background:{COLOUR[f]}"></i>{names[f]}</span>'
                        for f in ORDER)
        return (f'<div class="legend">{items}<span>bands are the solver\'s mix for that '
                f'hand, left to right; faded cells are partly in the range; unlit cells are '
                f'hands not in it</span></div>')
    return ('<div class="legend"><span><i class="sw" style="background:var(--accent)"></i>'
            'in range</span><span>darker is more of the hand\'s combos</span></div>')
