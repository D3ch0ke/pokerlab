"""The coach must never assert more than the data supports."""

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from pokerlab.coach.report import build
from pokerlab.db.load import SCHEMA

NOW = datetime.now(timezone.utc)

#: Column order is a schema detail; name the fields so the test does not break
#: every time a column is added.
DEFAULTS = {
    "hand_id": "h", "game_name": "NLHE 0.02/0.05 6 Max", "played_at": NOW,
    "table_id": "t", "sb": 2, "bb": 5, "total_pot": 100, "rake": 5, "uncalled": 0,
    "n_players": 6, "hero": "Hero", "hero_pos": "BTN", "hero_net": 25,
    "hero_cards": "As Kd", "hero_rake": 1, "saw_flop": True, "showdown": True,
    "hero_won": True, "board": "2c 3d 4h",
    "flop_paired": False, "flop_suits": "rainbow", "flop_high": "low",
    "flop_connect": "connected", "flop_wet": False,
}


@pytest.fixture
def db():
    con = duckdb.connect()
    con.execute(SCHEMA.read_text())
    return con


ACTION = {
    "hand_id": "h", "idx": 0, "street": "PRE-FLOP", "seat_no": 1, "name": "Hero",
    "position": "BTN", "is_hero": True, "verb": "Raises to", "announced": 15,
    "contributed": 15, "effective": 15, "pot_before": 7, "all_in": False,
    "dead": False, "secs": 1.5,
}


def add_hands(con, n: int, **overrides):
    """Insert hands *and* their actions.

    Every statistic in the tool is derived from the action stream, so a hand
    with no actions is correctly invisible -- inserting hands alone produces
    an empty report and tests nothing.
    """
    hcols = [c[0] for c in con.execute("DESCRIBE hands").fetchall()]
    acols = [c[0] for c in con.execute("DESCRIBE actions").fetchall()]
    hands, actions = [], []
    for i in range(n):
        row = DEFAULTS | {"hand_id": f"h{i}", "played_at": NOW - timedelta(hours=i)} | overrides
        hands.append(tuple(row[c] for c in hcols))
        act = ACTION | {"hand_id": f"h{i}"}
        actions.append(tuple(act[c] for c in acols))
    con.executemany(f"INSERT INTO hands VALUES ({','.join('?' * len(hcols))})", hands)
    con.executemany(f"INSERT INTO actions VALUES ({','.join('?' * len(acols))})", actions)


def test_reports_nothing_when_there_are_no_hands(db):
    md = build(db, days=7)
    assert "Nothing to report" in md
    assert "leak" not in md.lower()


def test_hedges_the_result_rather_than_declaring_a_verdict(db):
    add_hands(db, 60)
    assert "too few to read the result as a verdict" in build(db, days=7)


def test_states_that_biggest_losses_are_not_biggest_mistakes(db):
    add_hands(db, 40, hero_net=-500, hero_won=False)
    md = build(db, days=7)
    assert "not the same as the largest" in md
    assert "a correct call that loses belongs in this table too" in md
    # the EV section says where mistakes live, and is honest when there are none
    assert "## EV" in md


def test_says_so_when_no_leak_clears_the_evidence_bar(db):
    """Silence must be explained, or it reads as 'nothing is wrong'."""
    add_hands(db, 30)
    md = build(db, days=7)
    assert "statement about sample size, not about your play" in md


def test_output_disclaims_model_opinion(db):
    add_hands(db, 5)
    assert "Nothing here is a model's opinion" in build(db, days=7)
