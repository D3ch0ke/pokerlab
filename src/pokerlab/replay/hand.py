"""Reconstruct one hand from the database, frame by frame.

The database stores the action stream and `pot_before`; everything a replayer
needs to draw a table -- stacks, per-street commitments, who has folded, how
much board is face up -- is derived here rather than stored, so it can never
drift from the action stream it is derived from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..parse.model import Street

STREETS = ("PRE-FLOP", "FLOP", "TURN", "RIVER")
#: How many board cards are face up once each street is dealt.
_BOARD_AT = {"PRE-FLOP": 0, "FLOP": 3, "TURN": 4, "RIVER": 5}

_HAND_SQL = """
SELECT hand_id, played_at, table_id, sb, bb, total_pot, rake, uncalled, n_players,
       hero, hero_pos, hero_net, hero_cards, hero_rake, saw_flop, showdown, hero_won,
       board, flop_wet, flop_suits, flop_high, flop_connect, flop_paired
FROM hands WHERE hand_id = ?
"""

_SEATS_SQL = """
SELECT seat_no, name, stack, position, is_hero, net, shown
FROM seats WHERE hand_id = ? ORDER BY seat_no
"""

_ACTIONS_SQL = """
SELECT idx, street, seat_no, name, position, is_hero, verb,
       announced, contributed, effective, pot_before, all_in, dead, secs
FROM actions WHERE hand_id = ? ORDER BY idx
"""


@dataclass(frozen=True, slots=True)
class Seat:
    seat_no: int
    name: str
    stack: int              # cents at the start of the hand, before blinds
    position: str | None
    is_hero: bool
    net: int                # cents, profit over the whole hand
    shown: tuple[str, ...]  # empty unless they showed at showdown


@dataclass(frozen=True, slots=True)
class Action:
    idx: int
    street: str
    seat_no: int
    name: str
    position: str | None
    is_hero: bool
    verb: str
    announced: int
    contributed: int
    effective: int
    pot_before: int
    all_in: bool
    dead: bool
    secs: float | None

    @property
    def is_blind(self) -> bool:
        return self.verb in ("Posts SB", "Posts BB", "Posts Ante")

    @property
    def is_wager(self) -> bool:
        return self.verb in ("Bets", "Raises to")

    def pct_pot(self) -> float | None:
        """Bet size as a share of the pot before it. Uses `effective`.

        Announced would call a 5.00 shove into a 2.00 pot against a villain
        with 1.00 behind a 250%-pot bet. It is a 50% bet.
        """
        if not self.is_wager or self.pot_before <= 0:
            return None
        return self.effective / self.pot_before


@dataclass(frozen=True, slots=True)
class Frame:
    """The table as it stood immediately BEFORE `action` was taken."""

    action: Action
    pot: int                     # chips in the middle, called and uncalled alike
    street_committed: dict[str, int]
    stacks: dict[str, int]
    folded: frozenset[str]
    board: tuple[str, ...]

    @property
    def to_call(self) -> int:
        """What the actor must put in to continue. 0 means they can check."""
        high = max(self.street_committed.values(), default=0)
        return max(0, high - self.street_committed.get(self.action.name, 0))

    @property
    def live(self) -> list[str]:
        return [n for n in self.stacks if n not in self.folded]


@dataclass(slots=True)
class ReplayHand:
    hand_id: str
    played_at: datetime
    table_id: str
    sb: int
    bb: int
    total_pot: int
    rake: int
    uncalled: int
    hero: str
    hero_pos: str | None
    hero_net: int
    hero_cards: tuple[str, ...]
    hero_rake: int
    saw_flop: bool
    showdown: bool
    hero_won: bool
    board: tuple[str, ...]
    texture: dict[str, object]
    seats: list[Seat] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)

    # -- derived views -----------------------------------------------------
    @property
    def seat_of(self) -> dict[str, Seat]:
        return {s.name: s for s in self.seats}

    def board_at(self, street: str) -> tuple[str, ...]:
        return self.board[: _BOARD_AT[street]]

    @property
    def streets(self) -> list[str]:
        seen = {a.street for a in self.actions}
        return [s for s in STREETS if s in seen]

    @property
    def preflop_aggressor(self) -> str | None:
        """Last preflop raiser. None in a limped pot, where c-bet, donk and
        stab are all undefined rather than zero."""
        return next((a.name for a in reversed(self.actions)
                     if a.street == "PRE-FLOP" and a.verb == "Raises to"), None)

    def frames(self) -> list[Frame]:
        """One frame per action, each holding the state just before it.

        Blind posts are frames too: the replayer draws them, and the pot they
        build is the pot the first real decision faces.
        """
        stacks = {s.name: s.stack for s in self.seats}
        committed: dict[str, int] = {}
        folded: set[str] = set()
        pot = 0
        street = "PRE-FLOP"
        out: list[Frame] = []

        for a in self.actions:
            if a.street != street:
                street, committed = a.street, {}
            out.append(Frame(
                action=a, pot=pot, street_committed=dict(committed),
                stacks=dict(stacks), folded=frozenset(folded),
                board=self.board_at(a.street),
            ))
            pot += a.contributed
            stacks[a.name] = stacks.get(a.name, 0) - a.contributed
            # A dead blind is money in the pot that does not raise the price
            # for anyone: it must not become the street's high-water mark.
            if not a.dead:
                committed[a.name] = committed.get(a.name, 0) + a.contributed
            if a.verb == "Folds":
                folded.add(a.name)
        return out

    def effective_stack(self, a: str, b: str) -> int:
        seats = self.seat_of
        return min(seats[a].stack, seats[b].stack)


def load(con, hand_id: str) -> ReplayHand:
    row = con.execute(_HAND_SQL, [hand_id]).fetchone()
    if row is None:
        raise KeyError(f"no hand {hand_id!r} in the database")
    (hid, at, table, sb, bb, pot, rake, uncalled, _n, hero, hero_pos, hero_net,
     hero_cards, hero_rake, saw_flop, showdown, hero_won, board,
     wet, suits, high, connect, paired) = row

    hand = ReplayHand(
        hand_id=hid, played_at=at, table_id=table or "", sb=sb, bb=bb,
        total_pot=pot, rake=rake, uncalled=uncalled, hero=hero, hero_pos=hero_pos,
        hero_net=hero_net, hero_cards=tuple((hero_cards or "").split()),
        hero_rake=hero_rake, saw_flop=saw_flop, showdown=showdown, hero_won=hero_won,
        board=tuple((board or "").split()),
        texture={"wet": wet, "suits": suits, "high": high,
                 "connect": connect, "paired": paired},
    )
    hand.seats = [
        Seat(no, name, stack, pos, is_hero, net, tuple((shown or "").split()))
        for no, name, stack, pos, is_hero, net, shown
        in con.execute(_SEATS_SQL, [hand_id]).fetchall()
    ]
    hand.actions = [
        Action(*r) for r in con.execute(_ACTIONS_SQL, [hand_id]).fetchall()
    ]
    return hand
