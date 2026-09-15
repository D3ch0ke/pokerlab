"""Normalised hand-history model.

Money is integer **cents** everywhere. Cents reconcile exactly against the
euro amounts in the files, and convert losslessly to milli-big-blinds
(1c at NL5 = 200 mbb), so nothing is ever a float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Street(StrEnum):
    PREFLOP = "PRE-FLOP"
    FLOP = "FLOP"
    TURN = "TURN"
    RIVER = "RIVER"


class Verb(StrEnum):
    FOLD = "Folds"
    CHECK = "Checks"
    CALL = "Calls"
    BET = "Bets"
    RAISE = "Raises to"
    POST_SB = "Posts SB"
    POST_BB = "Posts BB"
    POST_ANTE = "Posts Ante"


#: Verbs that move money into the pot.
WAGERS = frozenset({Verb.CALL, Verb.BET, Verb.RAISE, Verb.POST_SB, Verb.POST_BB, Verb.POST_ANTE})
#: Verbs that post a blind, live or dead.
BLINDS = frozenset({Verb.POST_SB, Verb.POST_BB})

#: Non-action table events. Present in the files, irrelevant to strategy.
NOISE = ("Sits", "Reconnected", "Disconnected")


class ParseError(Exception):
    """Raised when a hand cannot be fully parsed. Never swallowed."""


@dataclass(slots=True)
class Seat:
    seat_no: int
    name: str
    stack: int          # cents at the start of the hand, before blinds
    tags: frozenset[str]  # {"SB"}, {"BTN","SB","Hero"}, ...

    @property
    def is_hero(self) -> bool:
        return "Hero" in self.tags


@dataclass(slots=True)
class Action:
    street: Street
    seat_no: int
    name: str
    verb: Verb
    time: str
    announced: int = 0
    """Total street wager the player declared (``Raises to X`` is cumulative)."""
    contributed: int = 0
    """Chips this action actually added to the pot."""
    effective: int = 0
    """``announced`` capped at what a live opponent could still call.

    This is the number every %-pot metric uses. A 500 shove against a
    villain with 100 behind is a 100 bet, not a 500 one.
    """
    all_in: bool = False
    dead: bool = False
    """A blind posted by a player not tagged for it: dead money that does
    not count toward their raise-to baseline."""


@dataclass(slots=True)
class Hand:
    hand_id: str
    game_name: str
    game_mode: str
    played_at: datetime
    table_id: str
    sb: int
    bb: int
    total_pot: int
    rake: int

    seats: list[Seat] = field(default_factory=list)
    hole_cards: dict[str, list[str]] = field(default_factory=dict)
    boards: dict[Street, list[str]] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    shown: dict[str, list[str]] = field(default_factory=dict)
    collected: dict[str, int] = field(default_factory=dict)
    """Per player, chips won across main and side pots (net of rake)."""

    @property
    def hero(self) -> Seat | None:
        return next((s for s in self.seats if s.is_hero), None)

    @property
    def contributions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            if a.contributed:
                out[a.name] = out.get(a.name, 0) + a.contributed
        return out

    @property
    def uncalled(self) -> int:
        """Chips returned to the last aggressor.

        The files never state this, but in cash games ``Total Pot`` counts
        called money only, so the difference is exact.
        """
        return sum(self.contributions.values()) - self.total_pot

    def net(self, name: str) -> int:
        """Profit in cents: what a player collected minus what they risked."""
        contributed = self.contributions.get(name, 0)
        returned = self.uncalled if name == self._last_aggressor() else 0
        return self.collected.get(name, 0) - contributed + returned

    def _last_aggressor(self) -> str | None:
        best, who = 0, None
        for name, amt in self.contributions.items():
            if amt > best:
                best, who = amt, name
        return who
