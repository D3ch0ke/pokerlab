"""Hero's decision points, and what it takes to solve one.

Most decisions in a hand history cannot be handed to a postflop solver, and
the useful thing this module does is say *which* and *why* rather than solving
something adjacent and calling it the same question. Three cuts remove most of
them, each for a different reason:

* **Preflop** is not what a postflop solver models at all. Those nodes are
  graded against a chart instead, exactly as the trainer does.
* **Multiway pots** need a three-range equilibrium. `postflop-solver` is a
  two-player engine; there is no honest way to fake the third.
* **Streets hero never reached** are not decisions.

What survives is the heads-up postflop node, which is where the tool has
something to say.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ranges.notation import RANKS, SUITS, canonical
from .hand import Action, Frame, ReplayHand

_ORD = {r: i for i, r in enumerate(RANKS)}
#: How many distinct bet sizes a tree may carry per street. Memory grows
#: steeply in this number and a flop tree with three sizings reaches gigabytes.
MAX_SIZES = 2


def combo(cards: tuple[str, ...] | list[str]) -> str:
    """postflop-solver requires the higher rank first: 'Kc9c', never '9cKc'.

    For a pair it reports the higher SUIT first as well ('8s8d', 'AhAc'), and
    a strategy lookup is an exact string match -- '8d8s' silently found no row,
    which left every pocket-pair decision ungraded.
    """
    return "".join(sorted(cards, key=lambda c: (_ORD[c[0]], -SUITS.index(c[1]))))


@dataclass(slots=True)
class Decision:
    """One point where hero had to act, and everything a solve would need."""

    idx: int                       # index into hand.frames()
    frame: Frame
    action: Action                 # what hero actually did
    street: str
    board: tuple[str, ...]
    hero_combo: str | None
    solvable: bool
    reason: str = ""               # why not, when not
    villain: str | None = None
    villain_position: str | None = None
    villain_spot: str | None = None        # the villain's LAST preflop decision
    villain_verb: str | None = None
    opener: str | None = None
    villain_path: tuple[tuple[str, str, str | None], ...] = ()
    """Every preflop decision the villain made, in order, as (spot, verb, opener).

    A limper who then calls a raise, or an opener who calls a 3-bet, has a
    range that is the product of both decisions; the first alone is far too
    wide and, for the opener, simply the wrong range.
    """
    starting_pot: int = 0
    effective_stack: int = 0
    action_path: tuple[str, ...] = ()
    bet_sizes: dict[str, list[str]] = field(default_factory=dict)
    raise_sizes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        pot = f"{self.starting_pot / 100:.2f}" if self.starting_pot else "?"
        return f"{self.street.title()} · pot €{pot} · {self.action.verb.lower()}"

    def label_in(self, bb: int) -> str:
        """The same, in big blinds — what the player reads at the table."""
        pot = f"{self.starting_pot / bb:.1f}".rstrip("0").rstrip(".") if self.starting_pot else "?"
        return f"{self.street.title()} · pot {pot}bb · {self.action.verb.lower()}"

    def hero_step(self) -> str:
        """Hero's actual action, in the form the solver's path syntax takes."""
        return _step(self.action, self.frame)


def _step(a: Action, frame: Frame) -> str:
    """One action as a solver path step. Sizes go in as 'nearest to N chips'.

    `effective`, never `announced`: a shove past what the opponent can call is
    priced at what they can call, which is also the only size the tree holds.
    """
    if a.verb == "Checks":
        return "check"
    if a.verb == "Calls":
        return "call"
    if a.verb == "Folds":
        return "fold"
    if a.verb == "Bets":
        return f"bet~{a.effective}"
    if a.verb == "Raises to":
        return f"raise~{a.effective}"
    raise ValueError(f"no solver equivalent for {a.verb!r}")


def _street_sizes(hand: ReplayHand, street: str) -> tuple[list[str], list[str]]:
    """Bet and raise sizes taken from what the players actually did.

    Building the tree out of the hand's own sizes means the replayed line lands
    on real nodes instead of the nearest approximation, so a size warning from
    the solver means something when it does appear.
    """
    bets: list[str] = []
    raises: list[str] = []
    for f in hand.frames():
        a = f.action
        if a.street != street or not a.is_wager:
            continue
        if a.verb == "Bets" and a.pot_before > 0:
            pct = round(100 * a.effective / a.pot_before)
            size = f"{max(5, min(300, pct))}%"
            if size not in bets:
                bets.append(size)
        elif a.verb == "Raises to":
            prev = max(f.street_committed.values(), default=0)
            if prev > 0:
                mult = round(a.effective / prev, 1)
                size = f"{max(1.1, min(10.0, mult))}x"
                if size not in raises:
                    raises.append(size)
    return (bets or ["50%"])[:MAX_SIZES], (raises or ["2.5x"])[:MAX_SIZES]


def villain_preflop_path(hand: ReplayHand, villain: str) -> tuple[tuple[str, str, str | None], ...]:
    """Every preflop decision `villain` made: (spot class, verb, opener) each."""
    from .pool import spot_of

    raises = callers = 0
    opener: str | None = None
    path: list[tuple[str, str, str | None]] = []
    for a in hand.actions:
        if a.street != "PRE-FLOP" or a.is_blind or a.dead:
            continue
        if a.name == villain:
            path.append((spot_of(raises, callers), a.verb, opener))
        if a.verb == "Raises to":
            raises += 1
            opener = a.position
        elif a.verb == "Calls":
            callers += 1
    return tuple(path)


def _villain_preflop(hand: ReplayHand, villain: str) -> tuple[str, str, str | None]:
    """The villain's first preflop decision: spot class, verb, and the opener."""
    path = villain_preflop_path(hand, villain)
    return path[0] if path else ("RFI", "Folds", None)


def decisions(hand: ReplayHand) -> list[Decision]:
    """Every point hero acted, marked solvable or not, with the reason."""
    frames = hand.frames()
    out: list[Decision] = []
    hero_combo = combo(hand.hero_cards) if len(hand.hero_cards) == 2 else None

    # Pot and stacks as each street opened: the tree is built from the street,
    # then walked forward to hero's decision inside it.
    street_start: dict[str, Frame] = {}
    for f in frames:
        street_start.setdefault(f.action.street, f)

    for i, f in enumerate(frames):
        a = f.action
        if not a.is_hero or a.is_blind:
            continue

        base = Decision(idx=i, frame=f, action=a, street=a.street,
                        board=f.board, hero_combo=hero_combo, solvable=False)

        if a.street == "PRE-FLOP":
            base.reason = ("preflop — a postflop solver does not model this node; "
                           "it is graded against a reference chart instead")
            out.append(base)
            continue
        if hero_combo is None:
            base.reason = "hero's cards are not in the file for this hand"
            out.append(base)
            continue

        opened = street_start[a.street]
        live = [n for n in opened.stacks if n not in opened.folded]
        if len(live) != 2:
            base.reason = (f"{len(live)}-way pot — the solver is a two-player engine, "
                           f"and a third range cannot be honestly faked")
            out.append(base)
            continue

        villain = next(n for n in live if n != hand.hero)
        vpath = villain_preflop_path(hand, villain)
        spot, verb, opener = vpath[-1] if vpath else ("RFI", "Folds", None)
        bets, raises = _street_sizes(hand, a.street)

        # Everything on this street before hero acted, as a solver path.
        try:
            path = tuple(_step(g.action, g) for g in frames[:i]
                         if g.action.street == a.street)
        except ValueError as exc:
            base.reason = str(exc)
            out.append(base)
            continue

        # The street's opening frame already carries every stack as it stood
        # then, so there is nothing to re-derive here.
        seats = hand.seat_of
        behind = {n: opened.stacks[n] for n in live}

        out.append(Decision(
            idx=i, frame=f, action=a, street=a.street, board=f.board,
            hero_combo=hero_combo, solvable=True, villain=villain,
            villain_position=seats[villain].position, villain_spot=spot,
            villain_verb=verb, opener=opener, villain_path=vpath,
            starting_pot=opened.pot,
            effective_stack=max(1, min(behind.values())),
            action_path=path,
            bet_sizes={"flop": bets, "turn": ["66%"], "river": ["75%"]}
            if a.street == "FLOP" else
            {"flop": ["50%"], "turn": bets, "river": ["75%"]}
            if a.street == "TURN" else
            {"flop": ["50%"], "turn": ["66%"], "river": bets},
            raise_sizes={s: raises for s in ("flop", "turn", "river")},
        ))
    return out
