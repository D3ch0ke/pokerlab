"""Verdict store, node classification, and the aggregations built on them.

Nothing here touches the solver: the records are synthetic, because what is
being tested is that a stored verdict comes back intact and that the
aggregates apply the tool's own rules -- no rate without its n, no EV from an
untrusted or unsettled verdict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pokerlab.replay.grader import VERSION, ReadRecord, Store, VerdictRecord, node_of
from pokerlab.replay.hand import Action, ReplayHand, Seat
from pokerlab.stats import grades
from pokerlab.stats.core import MIN_N


def _rec(hand_id="H1", idx=5, street="FLOP", node="srp_pfa", verb="Bets", step="bet 30",
         loss=0.0, approved=True, trustworthy=True, stable=True, degraded="",
         range_bet=0.5, texture=None, first=True, version=VERSION, bb=5) -> VerdictRecord:
    read = ReadRecord(width="base", hero_action=step, freq={"check": 0.4, "bet 30": 0.6},
                      ev={"check": 10.0, "bet 30": 10.0 - loss}, ev_loss=loss,
                      approved=approved, range_freq={"check": 1 - range_bet, "bet 30": range_bet},
                      degraded=degraded, converged=True, trustworthy=trustworthy)
    return VerdictRecord(
        hand_id=hand_id, idx=idx, played_at="2026-09-01T10:00:00+00:00", street=street,
        board=["Ah", "Kd", "7c"], texture=texture or {"wet": False, "paired": False},
        node=node, hero_pos="BTN", villain_pos="BB", hero_oop=False, hero_combo="QsQd",
        hero_verb=verb, hero_step=step, first_of_street=first, action_path=["check"],
        starting_pot=70, effective_stack=900, bb=bb, headline="holds up" if approved else "loses EV",
        stable=stable, trustworthy=trustworthy, warnings=[], reads=[read], version=version,
        graded_at="2026-09-12T00:00:00+00:00", seconds=30.0)


def test_store_round_trips_a_record(tmp_path: Path):
    store = Store(tmp_path)
    rec = _rec(loss=7.5, approved=False)
    store.put(rec)
    store.put(_rec(idx=9, street="TURN"))
    back = store.get("H1")
    assert set(back) == {5, 9}
    assert back[5] == rec
    assert back[5].loss_bb() == 1.5              # 7.5 chips at a 5-chip big blind
    assert len(store.all()) == 2


def test_store_hides_verdicts_from_an_older_model(tmp_path: Path):
    store = Store(tmp_path)
    store.put(_rec(version=VERSION - 1))
    store.put(_rec(hand_id="H2"))
    assert [r.hand_id for r in store.all()] == ["H2"]
    assert len(store.all(version=None)) == 2


def _hand(raisers: list[tuple[str, bool]]) -> ReplayHand:
    h = ReplayHand(hand_id="H", played_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                   table_id="T", sb=2, bb=5, total_pot=0, rake=0, uncalled=0, hero="Hero",
                   hero_pos="BTN", hero_net=0, hero_cards=("Ah", "Kd"), hero_rake=0,
                   saw_flop=True, showdown=False, hero_won=False, board=(), texture={})
    h.seats = [Seat(1, "Hero", 500, "BTN", True, 0, ()), Seat(2, "V", 500, "BB", False, 0, ())]
    h.actions = [Action(idx=i, street="PRE-FLOP", seat_no=1, name=n, position=None, is_hero=is_hero,
                        verb="Raises to", announced=0, contributed=0, effective=0, pot_before=0,
                        all_in=False, dead=False, secs=None) for i, (n, is_hero) in enumerate(raisers)]
    return h


def test_node_is_the_pot_and_heros_role_in_it():
    assert node_of(_hand([])) == "limped"
    assert node_of(_hand([("Hero", True)])) == "srp_pfa"
    assert node_of(_hand([("V", False)])) == "srp_caller"
    assert node_of(_hand([("V", False), ("Hero", True)])) == "3bet_pfa"
    assert node_of(_hand([("Hero", True), ("V", False)])) == "3bet_caller"
    assert node_of(_hand([("Hero", True), ("V", False), ("Hero", True)])) == "4bet_pfa"


def test_untrusted_and_unsettled_verdicts_count_but_never_cost():
    recs = [_rec(loss=5.0, approved=False),
            _rec(hand_id="H2", loss=50.0, approved=False, trustworthy=False),
            _rec(hand_id="H3", loss=50.0, approved=False, stable=False),
            _rec(hand_id="H4", loss=50.0, approved=False, degraded="no raises")]
    t = grades.totals(recs)
    assert (t.graded, t.usable, t.untrusted, t.unstable) == (4, 1, 1, 1)
    assert t.loss_bb == 1.0
    assert [r.hand_id for r in grades.biggest(recs)] == ["H1"]


def test_bucket_rates_are_suppressed_below_min_graded():
    few = [_rec(hand_id=f"H{i}", loss=5.0, approved=False) for i in range(grades.MIN_GRADED - 1)]
    b = grades.by(few, lambda r: r.street)[0]
    assert b.usable == grades.MIN_GRADED - 1
    assert b.mistake_rate is None and b.loss_per_decision is None
    assert b.loss_bb == (grades.MIN_GRADED - 1) * 1.0
    enough = few + [_rec(hand_id="HX", loss=0.0)]
    b = grades.by(enough, lambda r: r.street)[0]
    assert b.mistake_rate == (grades.MIN_GRADED - 1) / grades.MIN_GRADED


def test_cbet_baseline_compares_solver_and_hero_on_the_same_spots():
    dry = [_rec(hand_id=f"D{i}", verb="Bets" if i % 2 else "Checks",
                step="bet 30" if i % 2 else "check", range_bet=0.6) for i in range(MIN_N)]
    wet = [_rec(hand_id=f"W{i}", range_bet=0.3, texture={"wet": True, "paired": False})
           for i in range(MIN_N - 1)]
    # a non-c-bet decision at the same node must not leak into the baseline
    facing = [_rec(hand_id="F", first=False, range_bet=1.0)]
    cells = {c.texture: c for c in grades.cbet_baseline(dry + wet + facing)}
    assert cells["dry"].n == MIN_N
    assert cells["dry"].solver == 0.6
    assert cells["dry"].hero == 0.5
    assert round(cells["dry"].gap, 3) == -0.1
    assert cells["wet"].n == MIN_N - 1 and cells["wet"].solver is None


def test_two_graders_cannot_share_a_store(tmp_path: Path):
    import pytest
    from pokerlab.replay.grader import AlreadyRunning, Filter, Grader
    first = Grader(None, None, Store(tmp_path))
    first.run_sync([], Filter())                    # releases its lock on exit
    second = Grader(None, None, Store(tmp_path))
    second.run_sync([], Filter())
    (tmp_path / ".grading.lock").write_text(str(__import__("os").getpid()))
    with pytest.raises(AlreadyRunning):
        Grader(None, None, Store(tmp_path)).run_sync([], Filter())
