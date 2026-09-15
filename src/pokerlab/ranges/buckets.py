"""Strategic buckets over the 169 hands.

Per-hand leak detection is a statistical fiction at this volume: after
recency weighting, an individual (position, hand) cell holds n = 2-4
observations. Grouping hands that play alike puts n in the tens or hundreds,
which is enough to say something true. The quiz still drills individual
hands; only the *diagnosis* is bucketed.
"""

from __future__ import annotations

import re

from .notation import RANKS, all_hands

_ORDER = {r: i for i, r in enumerate(RANKS)}
_PAIR = re.compile(r"^([2-9TJQKA])\1$")
_HAND = re.compile(r"^([2-9TJQKA])([2-9TJQKA])([so])$")
BROADWAY = set("AKQJT")


def bucket(hand: str) -> str:
    if _PAIR.match(hand):
        r = _ORDER[hand[0]]
        return "pairs: premium (JJ+)" if r <= 3 else (
            "pairs: mid (77-TT)" if r <= 7 else "pairs: small (22-66)")

    hi, lo, kind = _HAND.match(hand).groups()
    suited = kind == "s"
    gap = _ORDER[lo] - _ORDER[hi] - 1

    if hi == "A":
        if lo in BROADWAY:
            return f"{'suited' if suited else 'offsuit'} ace: broadway (ATx+)"
        if lo in "5432":
            return "suited ace: wheel (A5s-A2s)" if suited else "offsuit ace: weak"
        return f"{'suited' if suited else 'offsuit'} ace: middling (A9-A6)"

    if hi in BROADWAY and lo in BROADWAY:
        return f"{'suited' if suited else 'offsuit'} broadway"

    if hi == "K":
        return f"{'suited' if suited else 'offsuit'} king: weak"

    if suited:
        if gap == 0:
            return "suited connector"
        return "suited one-gapper" if gap == 1 else "suited gapper (2+)"

    return "offsuit junk"


def buckets() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for hand in all_hands():
        out.setdefault(bucket(hand), []).append(hand)
    return out
