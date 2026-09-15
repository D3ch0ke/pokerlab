"""Poker range notation: text <-> the 169 canonical hands <-> 1326 combos.

A *canonical hand* is one of the 169 strategically distinct starting hands:
"AA", "AKs", "AKo". A *combo* is one of the 1326 actual dealt pairs. Pairs
are 6 combos, suited 4, offsuit 12 -- so a range's combo count, not its hand
count, is what matters when comparing frequencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations

RANKS = "AKQJT98765432"
_ORDER = {r: i for i, r in enumerate(RANKS)}
SUITS = "cdhs"

_PAIR = re.compile(r"^([2-9TJQKA])\1$")
_HAND = re.compile(r"^([2-9TJQKA])([2-9TJQKA])([so])$")


def canonical(cards: list[str] | str) -> str:
    """['Qs','Qc'] -> 'QQ'; ['Ah','Kh'] -> 'AKs'; ['Ah','Kd'] -> 'AKo'."""
    if isinstance(cards, str):
        cards = cards.split()
    (r1, s1), (r2, s2) = cards[0], cards[1]
    if r1 == r2:
        return r1 + r2
    hi, lo = (r1, r2) if _ORDER[r1] < _ORDER[r2] else (r2, r1)
    return f"{hi}{lo}{'s' if s1 == s2 else 'o'}"


def combos(hand: str) -> int:
    """How many of the 1326 dealt combinations this canonical hand covers."""
    if _PAIR.match(hand):
        return 6
    return 4 if hand.endswith("s") else 12


def all_hands() -> list[str]:
    """All 169, in grid order: pairs on the diagonal, suited above it."""
    out = []
    for i, hi in enumerate(RANKS):
        for j, lo in enumerate(RANKS):
            if i == j:
                out.append(hi + lo)
            elif i < j:
                out.append(hi + lo + "s")
            else:
                out.append(lo + hi + "o")
    return out


def grid_index(hand: str) -> tuple[int, int]:
    """(row, col) in the conventional 13x13 chart: suited above the diagonal."""
    if _PAIR.match(hand):
        i = _ORDER[hand[0]]
        return i, i
    m = _HAND.match(hand)
    if not m:
        raise ValueError(f"not a canonical hand: {hand!r}")
    hi, lo, kind = m.groups()
    a, b = _ORDER[hi], _ORDER[lo]
    return (a, b) if kind == "s" else (b, a)


def _pairs_from(rank: str) -> list[str]:
    return [r + r for r in RANKS[: _ORDER[rank] + 1]]


def _expand_token(token: str) -> set[str]:
    token = token.strip()
    if not token or token == "*":
        return set(all_hands()) if token == "*" else set()

    # "77+" / "AJo+" / "KTs+": everything at least this strong on the axis.
    if token.endswith("+"):
        base = token[:-1]
        if _PAIR.match(base):
            return set(_pairs_from(base[0]))
        m = _HAND.match(base)
        if not m:
            raise ValueError(f"bad range token: {token!r}")
        hi, lo, kind = m.groups()
        return {
            f"{hi}{RANKS[k]}{kind}"
            for k in range(_ORDER[hi] + 1, _ORDER[lo] + 1)
        }

    # "A5s-A2s" / "99-66": an explicit span.
    if "-" in token:
        left, right = (t.strip() for t in token.split("-", 1))
        if _PAIR.match(left) and _PAIR.match(right):
            lo_i, hi_i = sorted((_ORDER[left[0]], _ORDER[right[0]]))
            return {RANKS[i] * 2 for i in range(lo_i, hi_i + 1)}
        ml, mr = _HAND.match(left), _HAND.match(right)
        if not (ml and mr and ml.group(1) == mr.group(1) and ml.group(3) == mr.group(3)):
            raise ValueError(f"bad range span: {token!r}")
        hi, kind = ml.group(1), ml.group(3)
        lo_i, hi_i = sorted((_ORDER[ml.group(2)], _ORDER[mr.group(2)]))
        return {f"{hi}{RANKS[i]}{kind}" for i in range(lo_i, hi_i + 1)}

    if _PAIR.match(token) or _HAND.match(token):
        return {token}
    raise ValueError(f"bad range token: {token!r}")


@dataclass(slots=True)
class Range:
    """Canonical hand -> frequency in [0, 1]."""

    weights: dict[str, float] = field(default_factory=dict)

    @classmethod
    def parse(cls, spec: str | dict[str, float]) -> "Range":
        """From "77+, AQo+, A5s-A2s" or {"77+": 1.0, "AJo": 0.5}."""
        weights: dict[str, float] = {}
        items = spec.items() if isinstance(spec, dict) else ((t, 1.0) for t in spec.split(","))
        for token, freq in items:
            for hand in _expand_token(token):
                weights[hand] = max(weights.get(hand, 0.0), float(freq))
        return cls(weights)

    def __contains__(self, hand: str) -> bool:
        return self.weights.get(hand, 0.0) > 0

    def freq(self, hand: str) -> float:
        return self.weights.get(hand, 0.0)

    @property
    def n_combos(self) -> float:
        return sum(combos(h) * w for h, w in self.weights.items())

    @property
    def pct(self) -> float:
        """Share of all 1326 combos, which is how ranges are usually quoted."""
        return round(100 * self.n_combos / 1326, 1)

    def to_spec(self) -> str:
        """Compact, deterministic text form. Round-trips through parse()."""
        return ", ".join(
            h if w == 1.0 else f"{h}:{w:g}"
            for h, w in sorted(self.weights.items(), key=lambda kv: _sort_key(kv[0]))
            if w > 0
        )


def _sort_key(hand: str) -> tuple:
    if _PAIR.match(hand):
        return (0, _ORDER[hand[0]], 0)
    hi, lo, kind = _HAND.match(hand).groups()
    return (1 if kind == "s" else 2, _ORDER[hi], _ORDER[lo])


def deal_combos(hand: str) -> list[tuple[str, str]]:
    """The concrete card pairs making up a canonical hand."""
    if _PAIR.match(hand):
        return [(hand[0] + a, hand[0] + b) for a, b in combinations(SUITS, 2)]
    hi, lo, kind = _HAND.match(hand).groups()
    if kind == "s":
        return [(hi + s, lo + s) for s in SUITS]
    return [(hi + a, lo + b) for a in SUITS for b in SUITS if a != b]
