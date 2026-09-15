"""Hand reconstruction and decision-node extraction.

These are the parts that must never drift from the action stream: a replayer
that draws the wrong pot or the wrong stack is worse than no replayer, because
it looks right.
"""

from __future__ import annotations

import pytest

from pokerlab.replay.hand import ReplayHand, Seat, Action, STREETS
from pokerlab.replay.node import combo, decisions


def _action(idx, street, name, verb, contributed=0, effective=0, announced=0,
            pot_before=0, position=None, is_hero=False, dead=False, seat_no=1):
    return Action(idx=idx, street=street, seat_no=seat_no, name=name, position=position,
                  is_hero=is_hero, verb=verb, announced=announced, contributed=contributed,
                  effective=effective, pot_before=pot_before, all_in=False, dead=dead,
                  secs=None)


def _hand(actions, seats, board=(), hero="Hero", bb=5):
    from datetime import datetime, timezone
    h = ReplayHand(
        hand_id="H1", played_at=datetime(2026, 1, 1, tzinfo=timezone.utc), table_id="T",
        sb=2, bb=bb, total_pot=0, rake=0, uncalled=0, hero=hero, hero_pos="BTN",
        hero_net=0, hero_cards=("Ah", "Kd"), hero_rake=0, saw_flop=True, showdown=False,
        hero_won=False, board=board, texture={})
    h.seats, h.actions = seats, actions
    return h


HEADS_UP = [
    Seat(1, "Hero", 500, "BTN", True, 0, ()),
    Seat(2, "Villain", 300, "BB", False, 0, ()),
]


def test_frames_track_pot_stacks_and_folds():
    actions = [
        _action(0, "PRE-FLOP", "Villain", "Posts BB", 5, 5, 5, 0, seat_no=2),
        _action(1, "PRE-FLOP", "Hero", "Raises to", 15, 15, 15, 5, is_hero=True),
        _action(2, "PRE-FLOP", "Villain", "Calls", 10, 15, 15, 20, seat_no=2),
        _action(3, "FLOP", "Villain", "Checks", 0, 0, 0, 30, seat_no=2),
        _action(4, "FLOP", "Hero", "Bets", 20, 20, 20, 30, is_hero=True),
        _action(5, "FLOP", "Villain", "Folds", 0, 0, 0, 50, seat_no=2),
    ]
    frames = _hand(actions, HEADS_UP, board=("2c", "7d", "9s")).frames()

    assert [f.pot for f in frames] == [0, 5, 20, 30, 30, 50]
    # Stacks are what remained BEFORE each action, never after it.
    assert frames[1].stacks["Hero"] == 500
    assert frames[4].stacks["Hero"] == 485
    # Street commitments reset when the street does.
    assert frames[2].street_committed == {"Villain": 5, "Hero": 15}
    assert frames[3].street_committed == {}
    assert frames[5].folded == frozenset()
    assert frames[3].board == ("2c", "7d", "9s")
    assert frames[0].board == ()


def test_to_call_is_the_price_of_continuing():
    actions = [
        _action(0, "PRE-FLOP", "Villain", "Posts BB", 5, 5, 5, 0, seat_no=2),
        _action(1, "PRE-FLOP", "Hero", "Raises to", 15, 15, 15, 5, is_hero=True),
        _action(2, "PRE-FLOP", "Villain", "Calls", 10, 15, 15, 20, seat_no=2),
    ]
    frames = _hand(actions, HEADS_UP).frames()
    assert frames[1].to_call == 5        # hero faces the big blind
    assert frames[2].to_call == 10       # villain owes the difference, not the whole raise


def test_dead_blind_does_not_set_the_price():
    """A blind posted by a player not tagged for it is dead money.

    If it counted toward the street's high-water mark, everyone behind would
    appear to owe a call that the table never asked them for.
    """
    seats = HEADS_UP + [Seat(3, "Latecomer", 400, "CO", False, 0, ())]
    actions = [
        _action(0, "PRE-FLOP", "Villain", "Posts BB", 5, 5, 5, 0, seat_no=2),
        _action(1, "PRE-FLOP", "Latecomer", "Posts BB", 5, 5, 5, 5, seat_no=3, dead=True),
        _action(2, "PRE-FLOP", "Hero", "Calls", 5, 5, 5, 10, is_hero=True),
    ]
    frames = _hand(actions, seats).frames()
    assert frames[2].pot == 10                       # the dead blind is still in the pot
    assert frames[2].street_committed == {"Villain": 5}
    assert frames[2].to_call == 5                    # not 5 + the dead 5


def test_pct_pot_uses_effective_not_announced():
    """A shove past what anyone can call is priced at what they can call."""
    a = _action(0, "FLOP", "Hero", "Bets", 500, effective=100, announced=500,
                pot_before=200, is_hero=True)
    assert a.pct_pot() == 0.5


def test_preflop_aggressor_is_none_in_a_limped_pot():
    actions = [
        _action(0, "PRE-FLOP", "Villain", "Posts BB", 5, 5, 5, 0, seat_no=2),
        _action(1, "PRE-FLOP", "Hero", "Calls", 5, 5, 5, 5, is_hero=True),
        _action(2, "PRE-FLOP", "Villain", "Checks", 0, 0, 0, 10, seat_no=2),
    ]
    assert _hand(actions, HEADS_UP).preflop_aggressor is None


def test_combo_puts_the_higher_rank_first():
    """postflop-solver rejects '9cKc' outright: the rank order is not cosmetic.

    Suit order within a pair is not part of that rule -- the engine accepts
    'AdAh' and 'AhAd' alike -- so this pins the ordering the engine enforces
    and leaves alone the part it does not care about.
    """
    assert combo(("9c", "Kc")) == "Kc9c"
    assert combo(("Kc", "9c")) == "Kc9c"
    assert combo(("Ah", "Ad"))[0] == combo(("Ah", "Ad"))[2] == "A"
    assert combo(("Ah", "Ad")) == combo(("Ad", "Ah"))     # deterministic
    # pairs: postflop-solver names the higher suit first, and the lookup is exact
    assert combo(("8d", "8s")) == "8s8d"
    assert combo(("Ac", "Ah")) == "AhAc"


def _three_way_flop():
    seats = HEADS_UP + [Seat(3, "Third", 400, "CO", False, 0, ())]
    actions = [
        _action(0, "PRE-FLOP", "Villain", "Posts BB", 5, 5, 5, 0, seat_no=2),
        _action(1, "PRE-FLOP", "Hero", "Raises to", 15, 15, 15, 5, is_hero=True),
        _action(2, "PRE-FLOP", "Third", "Calls", 15, 15, 15, 20, seat_no=3),
        _action(3, "PRE-FLOP", "Villain", "Calls", 10, 15, 15, 35, seat_no=2),
        _action(4, "FLOP", "Villain", "Checks", 0, 0, 0, 45, seat_no=2),
        _action(5, "FLOP", "Third", "Checks", 0, 0, 0, 45, seat_no=3),
        _action(6, "FLOP", "Hero", "Bets", 20, 20, 20, 45, is_hero=True),
    ]
    return _hand(actions, seats, board=("2c", "7d", "9s"))


def test_multiway_is_refused_not_approximated():
    ds = {d.idx: d for d in decisions(_three_way_flop())}
    flop = ds[6]
    assert not flop.solvable
    assert "3-way" in flop.reason and "two-player" in flop.reason


def test_preflop_is_never_handed_to_a_postflop_solver():
    ds = {d.idx: d for d in decisions(_three_way_flop())}
    assert not ds[1].solvable
    assert "preflop" in ds[1].reason


def test_heads_up_flop_carries_a_solvable_spec():
    actions = [
        _action(0, "PRE-FLOP", "Villain", "Posts BB", 5, 5, 5, 0, seat_no=2),
        _action(1, "PRE-FLOP", "Hero", "Raises to", 15, 15, 15, 5, is_hero=True),
        _action(2, "PRE-FLOP", "Villain", "Calls", 10, 15, 15, 20, seat_no=2),
        _action(3, "FLOP", "Villain", "Bets", 15, 15, 15, 30, seat_no=2),
        _action(4, "FLOP", "Hero", "Calls", 15, 15, 15, 45, is_hero=True),
    ]
    hand = _hand(actions, HEADS_UP, board=("2c", "7d", "9s"))
    d = {x.idx: x for x in decisions(hand)}[4]

    assert d.solvable
    assert d.villain == "Villain"
    assert d.starting_pot == 30                  # the pot as the flop opened
    assert d.effective_stack == 285              # villain's 300 less the 15 called
    assert d.action_path == ("bet~15",)          # villain's bet, by nearest size
    assert d.hero_step() == "call"
    assert d.hero_combo == "AhKd"
    assert d.bet_sizes["flop"] == ["50%"]        # 15 into 30, taken from the hand itself


def test_villain_cards_are_not_in_the_frame_until_shown():
    """A replayer that reveals early turns every review into hindsight."""
    seats = [Seat(1, "Hero", 500, "BTN", True, 0, ("Ah", "Kd")),
             Seat(2, "Villain", 300, "BB", False, 0, ("Qs", "Qc"))]
    hand = _hand([_action(0, "FLOP", "Hero", "Checks", is_hero=True)], seats)
    # The seat row carries them, but rendering decides when they are visible;
    # this pins the data so the decision cannot be quietly reversed.
    assert hand.seat_of["Villain"].shown == ("Qs", "Qc")


def test_second_preflop_action_is_not_graded_against_the_opening_chart():
    """Hero opens, gets 3-bet, folds. The RFI chart speaks to the open only.

    Grading the fold against it reported "not in the chart's range" for every
    correct fold to a re-raise. The second node must say it is not judged.
    """
    from pokerlab.web.replay import _preflop_panel

    seats = [
        Seat(1, "SB", 500, "SB", False, 0, ()),
        Seat(2, "BB", 500, "BB", False, 0, ()),
        Seat(3, "Hero", 500, "BTN", True, 0, ()),
    ]
    actions = [
        _action(0, "PRE-FLOP", "SB", "Posts SB", 2, 2, 2, 0, seat_no=1),
        _action(1, "PRE-FLOP", "BB", "Posts BB", 5, 5, 5, 2, seat_no=2),
        _action(2, "PRE-FLOP", "Hero", "Raises to", 15, 15, 15, 7, is_hero=True, seat_no=3),
        _action(3, "PRE-FLOP", "SB", "Folds", 0, 0, 0, 22, seat_no=1),
        _action(4, "PRE-FLOP", "BB", "Raises to", 56, 61, 61, 22, seat_no=2),
        _action(5, "PRE-FLOP", "Hero", "Folds", 0, 0, 0, 78, is_hero=True, seat_no=3),
    ]
    hand = _hand(actions, seats)
    hand.hero_cards = ("Qh", "9d")
    ds = {d.idx: d for d in decisions(hand)}

    opened = _preflop_panel(hand, ds[2])
    folded = _preflop_panel(hand, ds[5])

    # The open is a chart decision and is scored as one.
    assert "Not graded" not in opened
    assert "Q9o" in opened
    # The fold to the 3-bet is not: no chart, no score, no estimate in its place.
    assert "Not graded" in folded
    assert "re-raise" in folded
    assert "Not in the chart" not in folded
    assert "Matches the chart" not in folded
