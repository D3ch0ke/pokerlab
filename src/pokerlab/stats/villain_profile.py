"""One opponent in depth: where they open, how they defend, what they do on
each street, what they turn over, and how you have done against them.

Built in Python over the action stream of the hands you shared, because
the street-by-street logic (who had bet before, whether they had already
checked, whether they were the aggressor) is a state machine, and a state
machine reads better than four nested window functions. A villain with
1,400 shared hands is ~25k action rows; that walks in well under a second.

Every rate is a `Stat` and suppresses itself below `MIN_N`, as everywhere.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from ..ranges.strength import _card, evaluate
from .core import NL5, Stat

POSITIONS = ("UTG", "HJ", "CO", "BTN", "SB", "BB")
STREETS = ("FLOP", "TURN", "RIVER")
CATEGORY = ("no pair", "one pair", "two pair", "trips", "straight", "flush",
            "full house", "quads", "straight flush")

_HANDS_SQL = """
SELECT h.hand_id, h.played_at, h.board, h.bb, h.hero_net, h.hero_cards, h.hero_pos,
       h.saw_flop, h.showdown, h.total_pot, s.position, s.net, s.shown, s.stack
FROM seats s JOIN hands h USING (hand_id)
WHERE s.name = ? AND (NOT s.is_hero OR ?) AND h.game_name = ?
  AND h.played_at >= ? AND h.played_at < ?
ORDER BY h.played_at, h.hand_id
"""

_ACTIONS_SQL = """
SELECT a.hand_id, a.idx, a.street, a.name, a.position, a.is_hero, a.verb,
       a.announced, a.effective, a.contributed, a.pot_before, a.dead
FROM actions a JOIN seats s ON s.hand_id = a.hand_id AND s.name = ? AND (NOT s.is_hero OR ?)
JOIN hands h ON h.hand_id = a.hand_id
WHERE h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
ORDER BY a.hand_id, a.idx
"""


@dataclass(slots=True)
class Counter:
    made: int = 0
    opp: int = 0

    def add(self, hit: bool) -> None:
        self.opp += 1
        self.made += bool(hit)

    @property
    def stat(self) -> Stat:
        return Stat(self.made, self.opp)


@dataclass(slots=True)
class Seat:
    """The villain from one position."""
    dealt: int = 0
    rfi: Counter = field(default_factory=Counter)          # raised first in, unopened pot
    vs_open_fold: Counter = field(default_factory=Counter)  # first action facing one raise
    vs_open_call: Counter = field(default_factory=Counter)
    vs_open_3bet: Counter = field(default_factory=Counter)


@dataclass(slots=True)
class Street:
    """The villain's actions on one postflop street."""
    cbet: Counter = field(default_factory=Counter)       # aggressor, nobody has bet: bets
    lead: Counter = field(default_factory=Counter)       # not aggressor, nobody has bet: bets
    fold_to_bet: Counter = field(default_factory=Counter)
    raise_bet: Counter = field(default_factory=Counter)
    check_raise: Counter = field(default_factory=Counter)  # checked, then faced a bet: raised
    bets: int = 0
    raises: int = 0
    calls: int = 0
    checks: int = 0
    folds: int = 0

    @property
    def aggression(self) -> Stat:
        """Bets and raises as a share of every action taken on the street."""
        return Stat(self.bets + self.raises,
                    self.bets + self.raises + self.calls + self.checks + self.folds)


@dataclass(slots=True)
class Shown:
    hand_id: str
    played_at: datetime
    position: str | None
    cards: tuple[str, ...]
    board: tuple[str, ...]
    category: str          # made hand with the full board
    river_verb: str        # their last river action, "" if none
    preflop: str           # "open" | "3-bet" | "call" | "limp" | "blind" | ""
    net_bb: float
    hero_cards: tuple[str, ...]
    hero_net_bb: float


@dataclass(slots=True)
class HandRow:
    hand_id: str
    played_at: datetime
    position: str | None
    hero_pos: str | None
    hero_cards: tuple[str, ...]
    board: tuple[str, ...]
    pot_bb: float
    hero_net_bb: float
    villain_net_bb: float
    contested: bool        # both put money in voluntarily
    showdown: bool


@dataclass(slots=True)
class Profile:
    name: str
    hands: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    net_bb: float = 0.0                 # their result over the shared hands
    hero_net_bb: float = 0.0            # yours over the same hands
    contested: int = 0                  # hands where you both put money in
    hero_contested_bb: float = 0.0      # your result in those
    hero_contested_won: int = 0
    vpip: Counter = field(default_factory=Counter)
    pfr: Counter = field(default_factory=Counter)
    limp: Counter = field(default_factory=Counter)
    threebet: Counter = field(default_factory=Counter)
    cold_call: Counter = field(default_factory=Counter)     # calls an open, not from the blinds
    fold_to_3bet: Counter = field(default_factory=Counter)  # raised, faced a re-raise: folded
    fourbet: Counter = field(default_factory=Counter)
    open_sizes: list[float] = field(default_factory=list)   # bb, unopened pots only
    threebet_sizes: list[float] = field(default_factory=list)  # multiple of the open
    seats: dict[str, Seat] = field(default_factory=lambda: {p: Seat() for p in POSITIONS})
    streets: dict[str, Street] = field(default_factory=lambda: {s: Street() for s in STREETS})
    saw_flop: int = 0
    wtsd: Counter = field(default_factory=Counter)          # saw flop: reached showdown
    wsd: Counter = field(default_factory=Counter)           # reached showdown: won money
    shown: list[Shown] = field(default_factory=list)
    river_bets_shown: Counter = field(default_factory=Counter)  # river bet/raise shown: no pair
    recent: list[HandRow] = field(default_factory=list)
    biggest: list[HandRow] = field(default_factory=list)
    monthly: list[tuple[str, int]] = field(default_factory=list)

    @property
    def bb100(self) -> float:
        return 100 * self.net_bb / self.hands if self.hands else 0.0

    @property
    def hero_bb100(self) -> float:
        return 100 * self.hero_net_bb / self.hands if self.hands else 0.0

    @property
    def open_size(self) -> float | None:
        """Median, not mean: one shove would otherwise read as a 30bb open."""
        return _median(self.open_sizes)

    @property
    def threebet_size(self) -> float | None:
        return _median(self.threebet_sizes)

    @property
    def style(self) -> str:
        v, p = self.vpip.stat.pct, self.pfr.stat.pct
        if v is None or p is None:
            return ""
        loose = v >= 35
        passive = p < v * 0.5
        return {(False, False): "tight-aggressive", (False, True): "tight-passive",
                (True, False): "loose-aggressive", (True, True): "loose-passive"}[(loose, passive)]

    @property
    def shown_by_category(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = defaultdict(int)
        for s in self.shown:
            counts[s.category] += 1
        return [(c, counts[c]) for c in CATEGORY if counts[c]]


def _median(xs: list[float], least: int = 10) -> float | None:
    if len(xs) < least:
        return None
    ys = sorted(xs)
    mid = len(ys) // 2
    return round(ys[mid] if len(ys) % 2 else (ys[mid - 1] + ys[mid]) / 2, 1)


def _category(cards: tuple[str, ...], board: tuple[str, ...]) -> str:
    seven = tuple(_card(c) for c in cards + board)
    if len(seven) < 5:
        return "no board"
    return CATEGORY[evaluate(seven)[0]]


def profile(con, name: str, since: datetime, until: datetime, game: str = NL5,
            recent: int = 20, biggest: int = 10, hero: bool = False) -> Profile:
    """`hero=True` profiles your own seat with the same state machine, so a
    benchmark against the pool compares like with like. The hero-vs-villain
    fields (contested, hero_net) are then meaningless and left at zero."""
    p = Profile(name=name)
    hands = con.execute(_HANDS_SQL, [name, hero, game, since, until]).fetchall()
    if not hands:
        return p
    by_hand: dict[str, list[tuple]] = defaultdict(list)
    for row in con.execute(_ACTIONS_SQL, [name, hero, game, since, until]).fetchall():
        by_hand[row[0]].append(row)

    months: dict[str, int] = defaultdict(int)
    rows: list[HandRow] = []
    for (hid, at, board, bb, hero_net, hero_cards, hero_pos, saw_flop, showdown,
         total_pot, position, net, shown, _stack) in hands:
        p.hands += 1
        p.first_seen = p.first_seen or at
        p.last_seen = at
        p.net_bb += net / bb
        p.hero_net_bb += hero_net / bb
        months[f"{at:%Y-%m}"] += 1
        board_t = tuple((board or "").split())
        hero_t = tuple((hero_cards or "").split())

        acts = by_hand.get(hid, [])
        v_vpip = hero_vpip = False
        v_folded = False
        v_raised_pre = False
        preflop_line = "blind" if position in ("SB", "BB") else ""
        pfa = None
        river_verb = ""
        if position in p.seats:
            p.seats[position].dealt += 1

        # ---- preflop --------------------------------------------------
        raises_before = limpers_before = 0
        open_size = None
        first_done = False
        for (_h, _i, street, who, _pos, is_hero, verb, announced, effective,
             contributed, _pot, dead) in acts:
            if street != "PRE-FLOP":
                break
            if dead or verb in ("Posts SB", "Posts BB", "Posts Ante"):
                continue
            mine = who == name
            if is_hero and not hero and verb in ("Calls", "Raises to", "Bets"):
                hero_vpip = True
            if mine:
                if verb in ("Calls", "Raises to"):
                    v_vpip = True
                if verb == "Folds":
                    v_folded = True
                seat = p.seats.get(position or "")
                if not first_done:
                    first_done = True
                    unopened = raises_before == 0 and limpers_before == 0
                    if unopened and position != "BB" and seat is not None:
                        seat.rfi.add(verb == "Raises to")
                        if verb == "Raises to":
                            p.open_sizes.append((announced or effective) / bb)
                    if raises_before == 0:
                        p.limp.add(verb == "Calls" and position not in ("SB", "BB"))
                    if raises_before == 1:
                        p.threebet.add(verb == "Raises to")
                        if position not in ("SB", "BB"):
                            p.cold_call.add(verb == "Calls")
                        if seat is not None:
                            seat.vs_open_fold.add(verb == "Folds")
                            seat.vs_open_call.add(verb == "Calls")
                            seat.vs_open_3bet.add(verb == "Raises to")
                        if verb == "Raises to" and open_size:
                            p.threebet_sizes.append((announced or effective) / open_size)
                    preflop_line = {"Raises to": "3-bet" if raises_before >= 1 else "open",
                                    "Calls": "limp" if raises_before == 0 else "call",
                                    "Folds": "fold"}.get(verb, preflop_line)
                elif v_raised_pre and raises_before >= 2:
                    # They raised, someone re-raised, and this is their answer.
                    p.fold_to_3bet.add(verb == "Folds")
                    p.fourbet.add(verb == "Raises to")
                if verb == "Raises to":
                    v_raised_pre = True
            if verb == "Raises to":
                if raises_before == 0:
                    open_size = announced or effective
                raises_before += 1
                pfa = who
            elif verb == "Calls" and raises_before == 0:
                limpers_before += 1
        p.vpip.add(v_vpip)
        p.pfr.add(v_raised_pre)

        # ---- postflop -------------------------------------------------
        v_saw_flop = saw_flop and not v_folded
        if v_saw_flop:
            p.saw_flop += 1
        street_now = None
        bets_before = 0
        v_checked = False
        v_bet_this_street = False
        for (_h, _i, street, who, _pos, _is_hero, verb, *_rest) in acts:
            if street == "PRE-FLOP":
                continue
            if street != street_now:
                street_now, bets_before, v_checked, v_bet_this_street = street, 0, False, False
            st = p.streets.get(street)
            if who == name and st is not None:
                if street == "RIVER":
                    river_verb = verb
                if verb == "Bets":
                    st.bets += 1
                elif verb == "Raises to":
                    st.raises += 1
                elif verb == "Calls":
                    st.calls += 1
                elif verb == "Checks":
                    st.checks += 1
                elif verb == "Folds":
                    st.folds += 1
                    v_folded = True
                if bets_before == 0 and verb in ("Bets", "Checks"):
                    (st.cbet if pfa == name else st.lead).add(verb == "Bets")
                elif bets_before >= 1 and not v_bet_this_street and verb in ("Folds", "Calls", "Raises to"):
                    st.fold_to_bet.add(verb == "Folds")
                    st.raise_bet.add(verb == "Raises to")
                    if v_checked:
                        st.check_raise.add(verb == "Raises to")
                if verb == "Checks":
                    v_checked = True
                if verb in ("Bets", "Raises to"):
                    v_bet_this_street = True
            if verb in ("Bets", "Raises to"):
                bets_before += 1

        reached = showdown and not v_folded
        if v_saw_flop:
            p.wtsd.add(reached)
        if reached:
            p.wsd.add(net > 0)
        if shown:
            cards = tuple(shown.split())
            cat = _category(cards, board_t)
            p.shown.append(Shown(hid, at, position, cards, board_t, cat, river_verb,
                                 preflop_line, round(net / bb, 1), hero_t, round(hero_net / bb, 1)))
            if river_verb in ("Bets", "Raises to"):
                p.river_bets_shown.add(cat == "no pair")

        contested = v_vpip and hero_vpip
        if contested:
            p.contested += 1
            p.hero_contested_bb += hero_net / bb
            p.hero_contested_won += hero_net > 0
        rows.append(HandRow(hid, at, position, hero_pos, hero_t, board_t,
                            round(total_pot / bb, 1), round(hero_net / bb, 1),
                            round(net / bb, 1), contested, showdown))

    p.net_bb = round(p.net_bb, 1)
    p.hero_net_bb = round(p.hero_net_bb, 1)
    p.hero_contested_bb = round(p.hero_contested_bb, 1)
    p.recent = list(reversed(rows[-recent:]))
    p.biggest = sorted((r for r in rows if r.contested),
                       key=lambda r: -abs(r.hero_net_bb))[:biggest]
    p.shown.sort(key=lambda s: s.played_at, reverse=True)
    p.monthly = sorted(months.items())
    return p
