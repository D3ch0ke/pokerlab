"""Blind-defence curve analysis."""

from datetime import datetime, timezone

import pytest

from pokerlab.ranges.chart import load
from pokerlab.stats.ranges import Observation
from pokerlab.trainer.leakweight import curve_is_flat, defence_curve

NOW = datetime.now(timezone.utc)
CHART = "vs_open_6max_100bb"


def obs_for(opener: str, hand: str, verb: str, n: int) -> list[Observation]:
    return [Observation(hand, "BB", verb, 1, 0, NOW, opener) for _ in range(n)]


def test_key_includes_the_opener_for_vs_open_spots():
    o = Observation("AKs", "BB", "Calls", 1, 0, NOW, "BTN")
    assert o.spot == "vs_open" and o.key == "BB_vs_BTN"


def test_key_ignores_the_opener_elsewhere():
    o = Observation("AKs", "BB", "Raises to", 0, 0, NOW, None)
    assert o.spot == "RFI" and o.key == "BB"


def test_curve_reports_one_cell_per_opener_with_enough_data():
    obs = sum((obs_for(p, "AKs", "Calls", 40) for p in ("UTG", "HJ", "CO", "BTN", "SB")), [])
    cells = defence_curve(obs, load(CHART))
    assert [c.opener for c in cells] == ["UTG", "HJ", "CO", "BTN", "SB"]
    assert all(c.n == 40 for c in cells)


def test_thin_cells_are_dropped_not_guessed():
    obs = obs_for("BTN", "AKs", "Calls", 3) + obs_for("SB", "AKs", "Calls", 40)
    assert [c.opener for c in defence_curve(obs, load(CHART))] == ["SB"]


def test_a_flat_curve_is_detected():
    """Defending the same fraction against everyone is the leak."""
    obs = sum((obs_for(p, "AKs", "Calls", 40) for p in ("UTG", "CO", "SB")), [])
    cells = defence_curve(obs, load(CHART))
    assert all(c.observed == 1.0 for c in cells)
    assert curve_is_flat(cells)


def test_a_responsive_curve_is_not_flagged():
    obs = (obs_for("UTG", "72o", "Folds", 40)      # defend 0% vs early
           + obs_for("CO", "72o", "Folds", 20) + obs_for("CO", "AKs", "Calls", 20)
           + obs_for("SB", "AKs", "Calls", 40))    # defend 100% vs late
    cells = defence_curve(obs, load(CHART))
    assert not curve_is_flat(cells)


def test_reference_defence_widens_with_later_openers():
    """A sanity check on the chart itself, not on hero."""
    chart = load(CHART)
    totals = []
    for opener in ("UTG", "HJ", "CO", "BTN", "SB"):
        key = f"BB_vs_{opener}"
        totals.append(chart.action_range(key, "raise").pct + chart.action_range(key, "call").pct)
    assert totals == sorted(totals), f"defence must widen toward late position: {totals}"
