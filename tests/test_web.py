"""Every dashboard page renders against the real database.

Skips when the database has not been built. No solver is involved: the
verdict store is pointed at an empty directory, so the pages are exercised in
their "nothing graded yet" state plus one synthetic verdict.
"""

from __future__ import annotations

import pytest

from pokerlab.db.load import DEFAULT_DB
from pokerlab.replay.grader import Store

pytestmark = pytest.mark.skipif(not DEFAULT_DB.exists(), reason="database not built")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from pokerlab.web import state
    from pokerlab.web.app import build_app
    application = build_app(DEFAULT_DB)
    state._state["store"] = Store(tmp_path)
    return application


def _ok(response, *needles: str) -> str:
    assert response.status_code == 200
    body = response.body.decode()
    for n in needles:
        assert n in body, n
    return body


def test_overview_carries_its_sample_and_interval(app):
    from pokerlab.web.dashboard import overview
    body = _ok(overview(days=0), "bb/100", "at 95%", "Monthly trend")
    assert "Nothing graded" in body


def test_hands_browser_filters_by_node_and_texture(app):
    from pokerlab.web.dashboard import hands
    _ok(hands(days=0, node="srp_pfa", tex="paired"), "SRP, you raised", "paired")
    body = _ok(hands(days=0, graded="mistakes"))
    assert "No hands match" in body


def test_stat_pages_render(app):
    from pokerlab.web import dashboard
    _ok(dashboard.postflop(days=0), "By street", "Solver c-bet baseline", "Texture is the flop only")
    _ok(dashboard.preflop(), "hand-authored", "defence by opener")
    _ok(dashboard.grades_page(), "Grade a batch", "Nothing graded yet")


def test_trainer_and_ranges_still_serve(app):
    from pokerlab.web.app import index, ranges
    _ok(index(), "/train/answer")
    _ok(ranges(), "confidence")


def test_replay_shows_a_stored_verdict(app):
    from pokerlab.web import state
    from pokerlab.web.dashboard import hands
    from pokerlab.web.replay import replay
    from pokerlab.replay.hand import load as load_hand
    from pokerlab.replay.node import decisions
    from tests.test_grader import _rec

    con = state.con()
    hid = con.execute("SELECT hand_id FROM hands WHERE saw_flop AND hero_cards IS NOT NULL "
                      "ORDER BY played_at DESC LIMIT 1").fetchone()[0]
    hand = load_hand(con, hid)
    d = next((d for d in decisions(hand) if d.solvable), None)
    if d is None:
        pytest.skip("newest flop hand has no solvable decision")
    state.store().put(_rec(hand_id=hid, idx=d.idx, street=d.street, loss=12.0, approved=False))

    _ok(replay(hid, step=d.idx), "Re-solve live", "loses EV")
    _ok(hands(days=0, graded="mistakes"), hid, "1 mistake")


def test_villains_page_lists_the_regulars(app):
    from pokerlab.web.dashboard import villains_page
    body = _ok(villains_page(days=0, min_hands=200), "their bb/100", "suppressed")
    assert "/hands?days=0&villain=" in body


def test_villain_search_ignores_the_hand_floor(app):
    from pokerlab.web import state
    from pokerlab.web.dashboard import villains_page
    thin = state.con().execute(
        "SELECT name, count(*) FROM seats WHERE NOT is_hero GROUP BY name "
        "HAVING count(*) BETWEEN 5 AND 20 LIMIT 1").fetchone()
    if thin is None:
        pytest.skip("no thin villain to search for")
    body = _ok(villains_page(q=thin[0][:4].upper()), thin[0])
    assert 'players matching' in body
    _ok(villains_page(q="zzzz-nobody"), "No villain matches")


def test_villains_sort_by_any_column_keeps_unsampled_last(app):
    import re
    from pokerlab.web.dashboard import _VCOLS, villains_page
    for key, _, _, _ in _VCOLS:
        _ok(villains_page(days=0, min_hands=100, sort=key), f"sort={key}&dir=")
    body = _ok(villains_page(days=0, min_hands=100, sort="fold3b", dir="asc"))
    # 3-bet, fold-to-3-bet, limp and c-bet are the four bare rate cells in a row
    # (VPIP and PFR carry a bar). After the first "--" in fold-to-3-bet, no
    # measured rate may follow.
    rows = re.findall(r"</td><td>(--|[\d.]+%)</td><td>(--|[\d.]+%)</td><td>(--|[\d.]+%)</td><td>(--|[\d.]+%)</td>", body)
    f3b = [row[1] for row in rows]
    if "--" in f3b:
        assert all(x == "--" for x in f3b[f3b.index("--"):])
    measured = [float(x[:-1]) for x in f3b if x != "--"]
    assert measured == sorted(measured)


@pytest.fixture()
def scratch_state(app, tmp_path, monkeypatch):
    """Marks and drill progress on disk in tmp, so a test never touches the real ones."""
    from pokerlab.trainer.marks import Marks
    from pokerlab.trainer.progress import Progress
    from pokerlab.web import drill, state
    state._state["marks"] = Marks(tmp_path / "reviewed.json")
    state._state["postflop_progress"] = Progress()
    state._state.pop("postflop_tally", None)
    monkeypatch.setattr(drill, "prog_path", lambda: tmp_path / "postflop_progress.json")
    return state


def _stored_on_newest_hand(state, **kw):
    from pokerlab.replay.hand import load as load_hand
    from pokerlab.replay.node import decisions
    from tests.test_grader import _rec
    con = state.con()
    # The newest heads-up flop hand with a solvable decision; multiway ones are skipped.
    rows = con.execute(
        "SELECT h.hand_id, h.played_at FROM hands h JOIN (SELECT hand_id, count(DISTINCT name) n "
        "FROM actions WHERE street = 'FLOP' GROUP BY hand_id) f USING (hand_id) "
        "WHERE h.saw_flop AND h.hero_cards IS NOT NULL AND f.n = 2 "
        "ORDER BY h.played_at DESC LIMIT 20").fetchall()
    for hid, at in rows:
        hand = load_hand(con, hid)
        d = next((d for d in decisions(hand) if d.solvable), None)
        if d is not None:
            break
    else:
        pytest.skip("no recent hand has a solvable decision")
    rec = _rec(hand_id=hid, idx=d.idx, street=d.street, **kw)
    rec.played_at = at.isoformat()
    state.store().put(rec)
    return hid, d.idx


def test_review_page_lists_the_last_session_and_marks(scratch_state):
    from pokerlab.web.review import mark, review, session_at, session_key
    hid, idx = _stored_on_newest_hand(scratch_state, loss=12.0, approved=False)
    body = _ok(review(s=0, all=0), "Decisions, costliest first", hid, "loses EV")
    assert "Stop-loss" in body or "result" in body
    # marking removes it from the default list and greys it under "all"
    mark(key=f"{hid}:{idx}", back="/review")
    body = _ok(review(s=0, all=0))
    assert hid not in body
    body = _ok(review(s=0, all=1), hid, "dimrow")
    assert session_at(0) is not None and session_key(session_at(0)).isdigit()


def test_replay_review_bar_walks_the_session(scratch_state):
    from pokerlab.web.replay import replay
    from pokerlab.web.review import session_at, session_key
    hid, idx = _stored_on_newest_hand(scratch_state, loss=12.0, approved=False)
    key = session_key(session_at(0))
    _ok(replay(hid, step=idx, list=key), "1 of 1 in this session", "mark reviewed",
        "ranges at this node", "solver")


def test_postflop_drill_filters_scores_and_remembers(scratch_state):
    from pokerlab.web import drill
    hid, idx = _stored_on_newest_hand(scratch_state, loss=12.0, approved=False)
    key = f"{hid}:{idx}"
    empty = dict(street="", pos="", tex="", days="", mode="", villain="")
    body = _ok(drill.postflop_drill(node="", **empty), key, 'data-key="b"')
    _ok(drill.postflop_drill(node="4bet_caller", **empty), "Nothing to drill")
    # the wrong family costs 12 chips = 2.4bb at bb=5; the card lapses
    body = _ok(drill.postflop_answer(key=key, fam="bet", action="", node="", **empty),
               "Costs 2.4bb", "Your range", "Next spot")
    prog = scratch_state.postflop_progress()
    assert prog.cards[key].lapses == 1 and prog.cards[key].seen == 1
    _ok(drill.postflop_answer(key=key, fam="check", action="", node="", **empty), "Correct")
    assert prog.cards[key].right == 1
    assert drill._tally()["asked"] == 2 and drill._tally()["right"] == 1
    _ok(drill.postflop_drill(node="", **{**empty, "mode": "struggling"}), "Nothing to drill")


def test_leak_trends_render_in_equal_hand_periods(app):
    from pokerlab.web.leaks import leaks_page
    body = _ok(leaks_page(days=0), "Blind defence", "Donk bets", "then → now", "equal-hand periods")
    assert "z=" in body


def test_overview_has_node_breakdown_and_session_card(app):
    from pokerlab.web.dashboard import overview
    _ok(overview(days=0, worst="session"), "last session")


def test_preflop_drill_modes_and_curve(app):
    from pokerlab.web.app import index
    body = _ok(index(spot="vs_open", pos="", mode=""), "leak-weighted", 'data-key="r"')
    assert "/train/answer" in body
