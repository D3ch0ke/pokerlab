"""Parser tests.

The fixtures are real hands, chosen because each one broke an earlier
version of the money model.
"""

from pathlib import Path

import pytest

from pokerlab.parse.betclic import parse_hand
from pokerlab.parse.model import Street, Verb
from pokerlab.positions import positions

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return parse_hand(FIXTURES.joinpath(name).read_text().split("*** HEADER ***", 1)[1])


@pytest.fixture
def shove():
    return load("effective_size_shove.txt")


@pytest.fixture
def dead_blind():
    return load("dead_small_blind.txt")


def test_effective_size_is_capped_by_the_shorter_stack(shove):
    """A bet is only as big as someone can call.

    Hero bets €2.87 into a €2.85 pot, but the only live opponent has €1.90
    behind. That is a ~67%-pot bet, not a ~100% overbet, and every downstream
    metric depends on getting this right.
    """
    bet = next(a for a in shove.actions if a.street is Street.FLOP and a.verb is Verb.BET)
    assert bet.announced == 287
    assert bet.effective == 190


def test_uncalled_portion_is_returned(shove):
    assert shove.uncalled == 287 - 190
    # Hero collected the pot, so the return is folded into their net.
    assert shove.net("Deshoke") == shove.collected["Deshoke"] - (
        shove.contributions["Deshoke"] - shove.uncalled)


def test_dead_blind_is_flagged_and_excluded_from_the_raise_baseline(dead_blind):
    """Two different players post a small blind; only one is the live SB."""
    posts = [a for a in dead_blind.actions if a.verb is Verb.POST_SB]
    assert [a.dead for a in posts] == [True, False]

    # The dead €0.02 does not count toward the shove's "raises to" baseline,
    # so the poster's total contribution is 0.02 + 0.99, not 0.99.
    dead_poster = posts[0].name
    assert dead_blind.contributions[dead_poster] == 101


@pytest.mark.parametrize("fixture", ["effective_size_shove.txt", "dead_small_blind.txt"])
def test_hand_reconciles_against_its_own_header(fixture):
    """Contributions minus the uncalled return must equal the stated pot."""
    h = load(fixture)
    assert h.uncalled >= 0
    assert sum(h.contributions.values()) - h.uncalled == h.total_pot
    assert sum(h.collected.values()) == h.total_pot - h.rake
    # Poker is zero sum apart from the house.
    assert sum(h.net(s.name) for s in h.seats) == -h.rake


def test_positions_cover_short_tables(dead_blind):
    pos = positions(dead_blind)
    assert pos[next(s.name for s in dead_blind.seats if "BTN" in s.tags)] == "BTN"
    assert set(pos.values()) == {"SB", "BB", "UTG", "HJ", "CO", "BTN"}


def test_hero_is_identified_by_tag(shove):
    assert shove.hero is not None and shove.hero.name == "Deshoke"
    assert shove.hole_cards["Deshoke"] == ["Qs", "Qc"]
