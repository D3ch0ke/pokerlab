"""Seat -> position labels.

Only SB/BB/BTN are tagged in the files; the rest are derived from seat order
relative to the button. 636 NL5 hands are 3-handed or heads-up, so this must
handle short tables, not just 6-max.
"""

from __future__ import annotations

from .parse.model import Hand, Seat

#: Positions counting backwards from the button.
_LATE = ["BTN", "CO", "HJ", "UTG", "UTG1", "UTG2", "UTG3"]

ORDER = ["SB", "BB", "UTG3", "UTG2", "UTG1", "UTG", "HJ", "CO", "BTN"]


def positions(hand: Hand) -> dict[str, str]:
    """``{player name: position}``. Empty if the button cannot be located."""
    seats = sorted(hand.seats, key=lambda s: s.seat_no)
    btn = next((i for i, s in enumerate(seats) if "BTN" in s.tags), None)
    if btn is None:
        return {}

    # Rotate so the button is last; preflop action then runs left to right.
    rotated: list[Seat] = seats[btn + 1:] + seats[: btn + 1]
    n = len(rotated)

    if n == 2:  # heads-up: the button is the small blind
        return {rotated[1].name: "SB", rotated[0].name: "BB"}

    out = {rotated[0].name: "SB", rotated[1].name: "BB"}
    for offset, seat in enumerate(reversed(rotated[2:])):
        out[seat.name] = _LATE[offset] if offset < len(_LATE) else f"UTG{offset}"
    return out
