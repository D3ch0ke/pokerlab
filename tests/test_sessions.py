"""Session reconstruction and the tilt null model."""

import random
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from pokerlab.db.load import SCHEMA
from pokerlab.stats.sessions import (
    SESSION_GAP, Session, detectable_shift, sessions, tilt_tests,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_session_splits_on_a_long_gap():
    con = duckdb.connect()
    con.execute(SCHEMA.read_text())
    cols = [c[0] for c in con.execute("DESCRIBE hands").fetchall()]
    base = {c: 0 for c in cols} | {
        "game_name": "NLHE 0.02/0.05 6 Max", "hero": "D", "bb": 5,
        "saw_flop": False, "showdown": False, "hero_won": False,
    }
    times = [NOW, NOW + timedelta(minutes=2), NOW + timedelta(minutes=90)]
    con.executemany(
        f"INSERT INTO hands VALUES ({','.join('?' * len(cols))})",
        [tuple((base | {"hand_id": f"h{i}", "played_at": t, "hero_net": 5})[c] for c in cols)
         for i, t in enumerate(times)])
    s = sessions(con)
    assert [x.hands for x in s] == [2, 1]


def test_peak_and_giveback():
    s = Session(NOW, [10.0, 20.0, -15.0])   # runs 10, 30, 15
    assert s.peak == 30 and s.final == 15 and s.given_back == 15


def test_peak_position_is_normalised():
    assert Session(NOW, [5.0, -1.0, -1.0]).peak_position == 0.0
    assert Session(NOW, [1.0, 1.0, 1.0]).peak_position == 1.0


def test_a_memoryless_player_is_not_flagged():
    """The null model must not accuse someone who only ever ran hot and cold."""
    rng = random.Random(1)
    sess = [Session(NOW, [rng.gauss(0.05, 18) for _ in range(120)]) for _ in range(120)]
    tests = tilt_tests(sess, trials=200, seed=2)
    assert tests, "expected the test to run at all"
    assert not any(t.significant for t in tests)


def test_real_tilt_is_detected():
    """Power check: the test is worthless if it cannot find an effect that is there.

    These players deteriorate sharply once they are up — exactly the pattern
    the null model is meant to catch.
    """
    rng = random.Random(3)
    sess = []
    for _ in range(120):
        nets, run = [], 0.0
        for _ in range(120):
            # once ahead, play badly on purpose
            nets.append(rng.gauss(-6.0 if run > 50 else 1.0, 18))
            run += nets[-1]
        sess.append(Session(NOW, nets))
    tests = tilt_tests(sess, trials=200, seed=4)
    assert any(t.significant for t in tests), \
        "a deliberately tilting player must be flagged, or the test proves nothing"


def test_too_few_sessions_returns_nothing_rather_than_guessing():
    assert tilt_tests([Session(NOW, [100.0])], trials=10) == []


def test_detectable_shift_shrinks_with_sample_size():
    assert detectable_shift(100) > detectable_shift(10_000)
    assert detectable_shift(1_183) == pytest.approx(5.4, abs=0.3)
