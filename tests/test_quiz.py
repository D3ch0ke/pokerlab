"""Quiz sampling and grading."""

import pytest

from pokerlab.ranges.chart import load
from pokerlab.trainer.quiz import Quiz, Spot


@pytest.fixture
def quiz():
    # No observations: the drill must still work for a player with no history.
    return Quiz([load("rfi_6max_100bb")], obs=[], seed=5)


def test_works_with_no_history(quiz):
    spot = quiz.next_spot()
    assert spot.position in load("rfi_6max_100bb").positions
    v = quiz.grade(spot, "fold")
    assert v.your_freq is None
    assert "not had this spot" in v.note


def test_grading_follows_the_chart(quiz):
    chart = load("rfi_6max_100bb")
    premium = Spot(chart.id, chart.label, "UTG", "AA", chart.spot)
    junk = Spot(chart.id, chart.label, "UTG", "72o", chart.spot)
    assert quiz.grade(premium, "raise").correct
    assert not quiz.grade(premium, "fold").correct
    assert quiz.grade(junk, "fold").correct
    assert not quiz.grade(junk, "raise").correct


def test_session_score_tracks_grading(quiz):
    chart = load("rfi_6max_100bb")
    aa = Spot(chart.id, chart.label, "UTG", "AA", chart.spot)
    quiz.grade(aa, "raise")
    quiz.grade(aa, "fold")
    assert quiz.session.asked == 2 and quiz.session.right == 1
    assert quiz.session.accuracy == 0.5


def test_sampling_is_combo_weighted(quiz):
    """Offsuit hands are 12 combos to a pair's 6, so they must come up more."""
    from collections import Counter
    counts = Counter()
    for _ in range(3000):
        counts[quiz._uniform_spot().hand] += 1
    pairs = sum(v for k, v in counts.items() if len(k) == 2)
    offsuit = sum(v for k, v in counts.items() if k.endswith("o"))
    assert offsuit > pairs


# --- grading against the full action distribution ------------------------

VS_OPEN = "vs_open_6max_100bb"


@pytest.fixture
def defence():
    return Quiz([load(VS_OPEN)], obs=[], seed=5)


@pytest.mark.parametrize("hand", ["76s", "K5s", "J8s"])
def test_a_pure_call_is_correct_and_folding_it_is_not(defence, hand):
    """Regression: the trainer graded only the raise range, so a hand the
    chart calls 100% of the time scored CORRECT when folded -- it was
    teaching hero to fold his entire big-blind defence."""
    chart = load(VS_OPEN)
    assert chart.prescribed("BB_vs_BTN", hand) == {"raise": 0.0, "call": 1.0, "fold": 0.0}
    spot = Spot(chart.id, chart.label, "BB_vs_BTN", hand, chart.spot)
    assert defence.grade(spot, "call").correct
    assert not defence.grade(spot, "fold").correct
    assert not defence.grade(spot, "raise").correct


def test_verdict_carries_the_whole_distribution(defence):
    chart = load(VS_OPEN)
    spot = Spot(chart.id, chart.label, "BB_vs_BTN", "AA", chart.spot)
    v = defence.grade(spot, "raise")
    assert v.prescribed == {"raise": 1.0, "call": 0.0, "fold": 0.0}
    assert v.best == "raise" and v.accepted == ("raise",) and v.chart_freq == 1.0


def test_a_genuine_mix_accepts_either_action():
    from pokerlab.ranges.chart import Chart
    from pokerlab.ranges.notation import Range
    mixed = Chart(id="t", label="t", spot="RFI", assumption="gto", confidence="reference",
                  source="test", version=1, actions=("raise", "fold"),
                  ranges={"BTN": {"raise": Range.parse({"AA": 0.5})}})
    q = Quiz([mixed], obs=[], seed=1)
    spot = Spot("t", "t", "BTN", "AA", "RFI")
    assert q.grade(spot, "raise").mixed
    assert q.grade(spot, "raise").correct and q.grade(spot, "fold").correct


# --- scenario / position filter ------------------------------------------

def test_filter_restricts_the_scenario():
    from pokerlab.trainer.quiz import Filter
    q = Quiz([load("rfi_6max_100bb"), load(VS_OPEN)], obs=[], seed=3)
    for _ in range(50):
        assert q.next_spot(Filter(spot="vs_open")).context == "vs_open"
    for _ in range(50):
        assert q.next_spot(Filter(spot="RFI")).context == "RFI"


def test_filter_restricts_the_position():
    from pokerlab.trainer.quiz import Filter
    q = Quiz([load("rfi_6max_100bb"), load(VS_OPEN)], obs=[], seed=3)
    for _ in range(50):
        spot = q.next_spot(Filter(spot="vs_open", position="BB_vs_BTN"))
        assert (spot.context, spot.position) == ("vs_open", "BB_vs_BTN")


def test_an_empty_filter_says_so_instead_of_sampling_anyway():
    from pokerlab.trainer.quiz import Filter, NoSpots
    q = Quiz([load("rfi_6max_100bb")], obs=[], seed=3)
    with pytest.raises(NoSpots):
        q.next_spot(Filter(spot="vs_3bet"))
    with pytest.raises(NoSpots):
        q.next_spot(Filter(position="BB_vs_BTN"))


def test_progress_keys_keep_their_three_part_shape():
    """quiz.next_spot() filters due cards on key.count('|') == 2."""
    chart = load("rfi_6max_100bb")
    q = Quiz([chart], obs=[], seed=1)
    q.grade(Spot(chart.id, chart.label, "UTG", "AA", chart.spot), "raise")
    assert list(q.progress.cards) == ["rfi_6max_100bb|UTG|AA"]
