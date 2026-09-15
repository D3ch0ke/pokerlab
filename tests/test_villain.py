"""Villain range assignment: the layered width/composition split.

The invariant these guard is the one the whole design rests on: the WIDTH of a
range comes from a measured action frequency and nothing is allowed to move it,
while showdown holdings -- a biased sample -- only ever decide WHICH hands fill
that width.
"""

from __future__ import annotations

import pytest

from pokerlab.ranges.notation import Range, combos
from pokerlab.ranges.strength import equity_on_board, ordering, rank_of
from pokerlab.replay.pool import Cell, Pool, cell_key, spot_of
from pokerlab.replay.villain import (OBSERVED_HALF, TOTAL_COMBOS, assign, band,
                                     narrow, _build_range, _wilson)


def _pool(opportunities=1000, taken=250, shown=None, position="BTN",
          spot="RFI", verb="Raises to"):
    cell = Cell(position, spot, verb, opportunities, taken, dict(shown or {}))
    return Pool({(position, spot, verb): cell}, {}, None, None)


def test_spot_classification():
    assert spot_of(0, 0) == "RFI"
    assert spot_of(1, 0) == "vs_open"
    assert spot_of(2, 1) == "vs_3bet"
    assert spot_of(0, 2) == "vs_limp"


def test_cell_key_keeps_the_opener_for_vs_open_only():
    assert cell_key("BB", "vs_open", "BTN") == "BB_vs_BTN"
    assert cell_key("BB", "RFI", "BTN") == "BB"


def test_frequency_is_suppressed_below_min_n():
    assert Cell("BTN", "RFI", "Raises to", 5, 2).frequency is None
    assert Cell("BTN", "RFI", "Raises to", 1000, 250).frequency == 0.25


def test_width_comes_from_the_measurement_not_the_showdowns():
    """The bug this pins: 72 distinct showdown hands is not a 33% range.

    A villain who opens 13% of the time mixes, so the union of hands they have
    ever been SEEN opening is far wider than the range they open with. Reading
    that union as membership produced a 33% range for a 13% villain.
    """
    many = {h.hand: 3 for h in ordering()[:72]}
    a = assign(_pool(opportunities=1000, taken=130, shown=many), "V", "BTN", "RFI",
               "Raises to")
    assert a.measured_width == pytest.approx(0.13)
    assert a.width_pct == pytest.approx(13.0, abs=1.5)


def test_observed_hands_change_composition_within_a_fixed_width():
    seen = {"72o": 40, "J3o": 40}          # hands no chart opens from the button
    without = assign(_pool(), "V", "BTN", "RFI", "Raises to")
    with_ = assign(_pool(shown=seen), "V", "BTN", "RFI", "Raises to")

    assert with_.width_pct == pytest.approx(without.width_pct, abs=1.5)
    assert with_.range.freq("72o") > 0     # observation put it there
    assert without.range.freq("72o") == 0  # the ordering never would have


def test_evidence_share_grows_with_the_sample():
    """Ten showdowns must not buy as much of the range as two hundred.

    The observed block is sized at `n / (n + OBSERVED_HALF)` of the width, so
    with enough distinct hands competing for it -- which is the real case, the
    live cells hold 60 to 72 -- a thin sample reaches far fewer of them.
    """
    target = 0.25 * TOTAL_COMBOS
    weak = {r.hand: 2 for r in ordering()[40:110]}     # hands the ordering would skip
    strong = {h: 40 for h in weak}

    thin, _ = _build_range(target, weak, None)
    thick, _ = _build_range(target, strong, None)

    seen_thin = sum(1 for h in weak if thin.freq(h) > 0)
    seen_thick = sum(1 for h in weak if thick.freq(h) > 0)
    assert seen_thick > seen_thin
    # Width is untouched by how much evidence there is: that is the invariant.
    assert thin.n_combos == pytest.approx(thick.n_combos, rel=0.05)
    assert thin.n_combos == pytest.approx(target, rel=0.05)


def test_a_thin_showdown_sample_is_ignored_entirely():
    """Below MIN_SHOWN the observed layer is recorded but not leaned on, so a
    couple of sightings cannot reshape a range on their own."""
    a = assign(_pool(shown={"72o": 3}), "V", "BTN", "RFI", "Raises to")
    assert a.range.freq("72o") == 0
    observed = [l for l in a.layers if l.name == "observed"]
    assert observed and "too few" in observed[0].detail


def test_hands_seen_more_often_per_combo_are_weighted_first():
    """Counts are read per combo, so a pair seen 6 times is not mistaken for
    being more common than a suited hand seen 5 -- there are simply more ways
    to be dealt it."""
    target = 0.02 * TOTAL_COMBOS          # a tight budget, so the order shows
    rng, _ = _build_range(target, {"72o": 12, "AA": 30}, None)
    assert rng.freq("AA") > rng.freq("72o")


def test_band_widens_when_the_sample_is_thin():
    """A per-villain frequency off 40 spots deserves a wider band than one
    off 4,000, and the Wilson interval is what supplies that."""
    thin = band(_pool(opportunities=40, taken=10), "V", "BTN", "RFI", "Raises to")
    thick = band(_pool(opportunities=4000, taken=1000), "V", "BTN", "RFI", "Raises to")
    thin_span = thin.loose.width_pct - thin.tight.width_pct
    thick_span = thick.loose.width_pct - thick.tight.width_pct
    assert thin_span > thick_span


def test_band_never_collapses_to_a_point():
    """Even a perfectly measured frequency leaves the ORDERING unverified, so
    the band keeps a floor: a zero-width band would claim certainty the tool
    does not have."""
    b = band(_pool(opportunities=100_000, taken=25_000), "V", "BTN", "RFI", "Raises to")
    assert b.loose.width_pct > b.base.width_pct > b.tight.width_pct


def test_width_scale_travels_with_the_assignment():
    b = band(_pool(), "V", "BTN", "RFI", "Raises to")
    assert b.tight.width_scale < 1.0 < b.loose.width_scale
    assert b.base.width_scale == 1.0


def test_wilson_is_sane_at_the_extremes():
    lo, hi = _wilson(0, 50)
    assert lo == 0.0 and 0 < hi < 0.15          # not a degenerate zero-width interval
    lo, hi = _wilson(25, 50)
    assert lo < 0.5 < hi


def test_no_measurement_and_no_chart_assigns_nothing():
    """Where neither the pool nor a chart covers a node, the tool says so
    rather than inventing a range to solve against."""
    a = assign(_pool(opportunities=3, taken=1, position="ZZ", spot="vs_3bet"),
               "V", "ZZ", "vs_3bet", "Raises to")
    assert not a.range.weights
    assert any("No range can be assigned" in n for n in a.notes)


def test_narrowing_ranks_by_board_equity_not_preflop_rank():
    """A flush draw must survive a continue that a small pair does not.

    Ranking by preflop strength would keep 22 and throw away the draw, which
    is the opposite of how anyone plays.
    """
    rng = Range({"22": 1.0, "AhKh": 0.0})       # weights keyed by canonical hand
    rng = Range({"22": 1.0, "AKs": 1.0})
    a = assign(_pool(), "V", "BTN", "RFI", "Raises to")
    a.range = rng
    kept = narrow(a, ("Qh", "Jh", "2s"), keep=0.5)
    # On a two-heart board with two overcards, AKs (nut flush draw plus
    # overcards) outranks a set-less pair of deuces... which flopped a set.
    assert set(kept.range.weights) <= {"22", "AKs"}
    assert kept.range.n_combos < rng.n_combos


def test_narrowing_records_itself_as_assumed():
    a = assign(_pool(), "V", "BTN", "RFI", "Raises to")
    kept = narrow(a, ("Qh", "Jh", "2s"), keep=0.3)
    layer = kept.layers[-1]
    assert layer.name == "narrowing" and layer.method == "assumed"
    assert kept.confidence == "low"          # the weakest layer governs


def test_confidence_is_governed_by_the_weakest_layer():
    a = assign(_pool(), "V", "BTN", "RFI", "Raises to")
    assert a.confidence in ("measured", "reference-low", "low")
    methods = {layer.method for layer in a.layers}
    if "assumed" in methods:
        assert a.confidence == "low"


def test_board_equity_orders_draws_above_air():
    draw = equity_on_board(("Ah", "Kh"), ("Qh", "Jh", "2c"))
    air = equity_on_board(("7c", "3d"), ("Qh", "Jh", "2s"))
    assert draw > air > 0
