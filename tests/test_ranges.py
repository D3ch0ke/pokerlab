"""Range notation, buckets and charts."""

import pytest

from pokerlab.ranges.buckets import bucket, buckets
from pokerlab.ranges.chart import available, load
from pokerlab.ranges.notation import (
    Range, all_hands, canonical, combos, deal_combos, grid_index,
)


def test_the_169_hands_cover_exactly_1326_combos():
    hands = all_hands()
    assert len(hands) == 169
    assert len(set(hands)) == 169
    assert sum(combos(h) for h in hands) == 1326


@pytest.mark.parametrize("cards,expected", [
    (["Qs", "Qc"], "QQ"), (["Ah", "Kh"], "AKs"), (["Ah", "Kd"], "AKo"),
    (["2c", "Th"], "T2o"), (["7d", "7h"], "77"), (["5s", "4s"], "54s"),
])
def test_canonical_orders_by_rank_and_suitedness(cards, expected):
    assert canonical(cards) == expected


def test_every_hand_has_a_distinct_grid_cell():
    assert len({grid_index(h) for h in all_hands()}) == 169


@pytest.mark.parametrize("spec,expected", [
    ("77+", {"77", "88", "99", "TT", "JJ", "QQ", "KK", "AA"}),
    ("A5s-A2s", {"A5s", "A4s", "A3s", "A2s"}),
    ("AJo+", {"AJo", "AQo", "AKo"}),
    ("KQs", {"KQs"}),
    ("99-66", {"66", "77", "88", "99"}),
])
def test_notation_expansion(spec, expected):
    assert set(Range.parse(spec).weights) == expected


def test_range_roundtrips_through_its_own_text_form():
    r = Range.parse("77+, AQo+, A5s-A2s, KTs+")
    assert Range.parse(r.to_spec()).weights == r.weights


def test_frequencies_are_preserved():
    r = Range.parse({"AA": 1.0, "AJo": 0.5})
    assert r.freq("AA") == 1.0 and r.freq("AJo") == 0.5
    assert r.freq("72o") == 0.0
    assert "72o" not in r


def test_percentage_is_combo_weighted_not_hand_weighted():
    # One pair (6 combos) must outweigh one offsuit hand (12) correctly.
    assert Range.parse("AA").n_combos == 6
    assert Range.parse("AKo").n_combos == 12
    assert Range.parse("AKs").n_combos == 4


@pytest.mark.parametrize("hand,n", [("AA", 6), ("AKs", 4), ("AKo", 12)])
def test_deal_combos_matches_the_count(hand, n):
    dealt = deal_combos(hand)
    assert len(dealt) == n == combos(hand)
    assert len({frozenset(c) for c in dealt}) == n


def test_buckets_partition_all_169_hands():
    grouped = buckets()
    assert sum(len(v) for v in grouped.values()) == 169
    assert len({h for v in grouped.values() for h in v}) == 169


@pytest.mark.parametrize("hand,expected", [
    ("AA", "pairs: premium (JJ+)"), ("55", "pairs: small (22-66)"),
    ("A3s", "suited ace: wheel (A5s-A2s)"), ("K4o", "offsuit king: weak"),
    ("76s", "suited connector"), ("QJo", "offsuit broadway"),
])
def test_bucket_assignment(hand, expected):
    assert bucket(hand) == expected


def test_charts_declare_their_provenance():
    """A chart you cannot audit is a chart you should not trust."""
    for chart in available():
        assert chart.source and chart.confidence and chart.assumption
        assert chart.version >= 1


def test_rfi_chart_widens_with_position():
    chart = load("rfi_6max_100bb")
    pcts = [chart.action_range(p, "raise").pct for p in ("UTG", "HJ", "CO", "BTN")]
    assert pcts == sorted(pcts), f"opening ranges must widen toward the button: {pcts}"


def test_prescribed_frequencies_sum_to_one():
    chart = load("rfi_6max_100bb")
    for hand in ("AA", "72o", "KTs"):
        assert sum(chart.prescribed("BTN", hand).values()) == pytest.approx(1.0)
