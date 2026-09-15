"""Parser for Betclic.fr (iPoker) hand-history exports.

Every rule below was validated by reconciling computed contributions against
the file's own ``Total Pot`` and ``Rake`` across all NL5 hands. See
``scripts/reconcile.py``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .model import (
    BLINDS, NOISE, WAGERS, Action, Hand, ParseError, Seat, Street, Verb,
)

_SECTION = re.compile(r"^\*\*\* ([A-Z &'\-]+) \*\*\*\s*(.*)$")
_KV = re.compile(r"^([A-Za-z &]+): (.*)$")
_SEAT = re.compile(r"^Seat (\d+): (.+?) \((€?)([\d.]+)\)(?: \[([^\]]+)\])?$")
_HOLE = re.compile(r"^(.+?): \[([2-9TJQKA][cdhs](?: [2-9TJQKA][cdhs])*)\]$")
_CARDS = re.compile(r"\[([2-9TJQKA][cdhs](?: [2-9TJQKA][cdhs])*)\]")
_SHOWS = re.compile(r"^(.+?) shows \[(.+?)\] \((.+?)\)")
# Side pots carry an ordinal: "wins 1st side pot of". Without it they are missed.
_WINS = re.compile(r"^(.+?) wins (?:main pot|\d+(?:st|nd|rd|th) side pot) of (?:€)?([\d.]+)$")
_ACTION = re.compile(
    r"^(\d{2}:\d{2}:\d{2}) - (.+?): "
    r"(Folds|Checks|Calls|Bets|Raises to|Posts SB|Posts BB|Posts Ante|Sits[a-z ]*|Reconnected|Disconnected)"
    r"(?: (?:€)?([\d.]+))?( and is all-in)?$"
)
_STREETS = {s.value: s for s in Street}


def _cents(text: str) -> int:
    """'€1.40' -> 140. Exact; never touches float."""
    whole, _, frac = text.replace("€", "").strip().partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def split_hands(text: str) -> list[str]:
    return [b for b in text.split("*** HEADER ***")[1:] if b.strip()]


def _sections(block: str) -> list[tuple[str, str, list[str]]]:
    out: list[tuple[str, str, list[str]]] = [("HEADER", "", [])]
    for line in block.splitlines():
        m = _SECTION.match(line)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip(), []))
        elif line.strip():
            out[-1][2].append(line)
    return out


def parse_hand(block: str) -> Hand:
    secs = _sections(block)
    header = {m.group(1): m.group(2) for line in secs[0][2] if (m := _KV.match(line))}

    try:
        sb_txt, bb_txt = header["Blinds"].split("/")
        hand = Hand(
            hand_id=header["Hand ID"],
            game_name=header["Game Name"],
            game_mode=header["Game Mode"],
            played_at=datetime.strptime(
                header["Date & Time"].replace(" (UTC)", ""), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc),
            table_id=header.get("Table ID", ""),
            sb=_cents(sb_txt),
            bb=_cents(bb_txt),
            total_pot=_cents(header["Total Pot"]),
            rake=_cents(header["Rake"]),
        )
    except KeyError as exc:
        raise ParseError(f"missing header field {exc}") from exc

    by_name: dict[str, Seat] = {}
    for _, _, lines in (s for s in secs if s[0] == "PLAYERS"):
        for line in lines:
            m = _SEAT.match(line)
            if not m:
                raise ParseError(f"unparsed seat line: {line!r}")
            # Tags share one bracket, space separated: "[BTN SB Hero]".
            tags = frozenset(m.group(5).split()) if m.group(5) else frozenset()
            seat = Seat(int(m.group(1)), m.group(2), _cents(m.group(4)), tags)
            hand.seats.append(seat)
            by_name[seat.name] = seat

    for _, _, lines in (s for s in secs if s[0] == "HOLE CARDS"):
        for line in lines:
            if m := _HOLE.match(line):
                hand.hole_cards[m.group(1)] = m.group(2).split()

    for name, extra, _ in secs:
        if (street := _STREETS.get(name)) and street is not Street.PREFLOP:
            if m := _CARDS.search(extra):
                hand.boards[street] = m.group(1).split()

    for _, _, lines in (s for s in secs if s[0] == "SHOWDOWN"):
        for line in lines:
            if m := _SHOWS.match(line):
                hand.shown[m.group(1)] = m.group(2).split()

    for _, _, lines in (s for s in secs if s[0] == "SUMMARY"):
        for line in lines:
            if m := _WINS.match(line):
                hand.collected[m.group(1)] = hand.collected.get(m.group(1), 0) + _cents(m.group(2))

    _parse_actions(hand, secs, by_name)
    return hand


def _parse_actions(hand: Hand, secs, by_name: dict[str, Seat]) -> None:
    stacks = {s.name: s.stack for s in hand.seats}
    folded: set[str] = set()

    for name, _, lines in secs:
        street = _STREETS.get(name)
        if street is None:
            continue
        live_wager: dict[str, int] = {}  # this street, excluding dead blinds

        for line in lines:
            m = _ACTION.match(line)
            if not m:
                if line.startswith("***") or not line.strip():
                    continue
                raise ParseError(f"unparsed action line: {line!r}")

            time, who, raw_verb, amt, allin = m.groups()
            if raw_verb.startswith(NOISE):
                continue
            if who not in by_name:
                raise ParseError(f"action by unseated player {who!r}")

            verb = Verb(raw_verb)
            act = Action(street, by_name[who].seat_no, who, verb, time, all_in=bool(allin))

            if verb is Verb.FOLD:
                folded.add(who)
            elif verb in WAGERS:
                if amt is None:
                    raise ParseError(f"{verb} without amount: {line!r}")
                value = _cents(amt)

                if verb in BLINDS:
                    # A blind posted by a player *not* tagged for it is dead
                    # money: it sits in the pot but does not count toward
                    # their raise-to baseline.
                    tag = "SB" if verb is Verb.POST_SB else "BB"
                    act.dead = tag not in by_name[who].tags
                    act.contributed = act.announced = act.effective = value
                    if not act.dead:
                        live_wager[who] = live_wager.get(who, 0) + value
                elif verb is Verb.RAISE:
                    act.announced = value
                    act.contributed = value - live_wager.get(who, 0)
                    act.effective = min(value, _cap(hand, who, stacks, live_wager, folded))
                    live_wager[who] = value
                else:  # Bets, Calls, Posts Ante
                    act.contributed = act.announced = value
                    live_wager[who] = live_wager.get(who, 0) + value
                    act.effective = (
                        min(live_wager[who], _cap(hand, who, stacks, live_wager, folded))
                        if verb is Verb.BET
                        else value
                    )

                stacks[who] -= act.contributed

            hand.actions.append(act)


def _cap(hand: Hand, actor: str, stacks: dict[str, int],
         live_wager: dict[str, int], folded: set[str]) -> int:
    """The most any still-live opponent could put in on this street.

    An all-in is only as big as someone can call: shoving 500 against a
    villain with 100 behind is a 100 bet.
    """
    return max(
        (live_wager.get(s.name, 0) + stacks[s.name]
         for s in hand.seats if s.name != actor and s.name not in folded),
        default=0,
    )


def parse_file(path) -> tuple[list[Hand], list[tuple[str, str]]]:
    """Return (hands, quarantine). Nothing is ever silently dropped."""
    hands, bad = [], []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for block in split_hands(fh.read()):
            try:
                hands.append(parse_hand(block))
            except (ParseError, ValueError, KeyError) as exc:
                bad.append((f"{type(exc).__name__}: {exc}", block))
    return hands, bad
