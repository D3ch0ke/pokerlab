"""Flop texture classification.

Postflop frequencies are close to meaningless pooled across boards: c-betting
70% is fine on A-K-4 rainbow and reckless on 9-8-7 two-tone. Every postflop
stat in this tool can be split by these dimensions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ranges.notation import RANKS

_ORD = {r: i for i, r in enumerate(RANKS)}          # 0 = ace
_VAL = {r: 14 - i for i, r in enumerate(RANKS)}     # 14 = ace


@dataclass(frozen=True, slots=True)
class Texture:
    paired: bool
    suitedness: str      # rainbow | two-tone | monotone
    high: str            # A | K | Q | J | T | low
    connectivity: str    # connected | gapped | disconnected

    @property
    def label(self) -> str:
        if self.paired:
            return f"paired ({self.high}-high)"
        return f"{self.suitedness} {self.connectivity}"

    @property
    def wet(self) -> bool:
        """Loosely: does this board hit a caller's range hard?"""
        return (self.suitedness != "rainbow" or self.connectivity == "connected") \
            and not self.paired


def classify(board: list[str] | str) -> Texture:
    cards = board.split() if isinstance(board, str) else list(board)
    flop = cards[:3]
    if len(flop) != 3:
        raise ValueError(f"need three flop cards, got {board!r}")

    ranks = [c[0] for c in flop]
    suits = [c[1] for c in flop]
    vals = sorted((_VAL[r] for r in ranks), reverse=True)

    paired = len(set(ranks)) < 3
    suitedness = {3: "rainbow", 2: "two-tone", 1: "monotone"}[len(set(suits))]
    top = ranks[[_ORD[r] for r in ranks].index(min(_ORD[r] for r in ranks))]
    high = top if top in "AKQJT" else "low"

    span = vals[0] - vals[2]
    if paired:
        connectivity = "paired"
    elif span <= 4:
        connectivity = "connected"
    elif span <= 7:
        connectivity = "gapped"
    else:
        connectivity = "disconnected"

    return Texture(paired, suitedness, high, connectivity)
