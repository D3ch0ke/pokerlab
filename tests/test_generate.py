"""Mechanically generated charts: determinism, provenance, and sanity."""

import json

import pytest

from pokerlab.ranges import generate
from pokerlab.ranges.chart import CHARTS_DIR, load
from pokerlab.ranges.notation import Range

AUTHORED = ("rfi_6max_100bb", "vs_open_6max_100bb", "iso_vs_limp_6max")


@pytest.mark.parametrize("chart_id", generate.GENERATED)
def test_generation_is_deterministic(chart_id):
    """Same table in, byte-identical JSON out -- otherwise the file on disk
    cannot be checked against the generator that claims to have made it."""
    assert generate.serialise(chart_id) == generate.serialise(chart_id)


@pytest.mark.parametrize("chart_id", generate.GENERATED)
def test_the_file_on_disk_is_what_the_generator_produces(chart_id):
    assert (CHARTS_DIR / f"{chart_id}.json").read_text() == generate.serialise(chart_id)


@pytest.mark.parametrize("chart_id", generate.GENERATED)
def test_generated_charts_load(chart_id):
    chart = load(chart_id)
    assert chart.id == chart_id and chart.ranges
    for position in chart.positions:
        assert chart.action_range(position, "raise").n_combos > 0
        for hand in ("AA", "72o"):
            assert sum(chart.prescribed(position, hand).values()) == pytest.approx(1.0)


@pytest.mark.parametrize("chart_id", generate.GENERATED)
def test_generated_charts_never_claim_to_be_solver_output(chart_id):
    chart = load(chart_id)
    assert chart.confidence == "derived"
    assert "NOT SOLVER OUTPUT" in chart.source
    assert "LIMITATION" in chart.source      # the nested-shape caveat must survive


@pytest.mark.parametrize("chart_id", AUTHORED)
def test_the_generator_refuses_to_touch_authored_charts(chart_id):
    with pytest.raises(KeyError):
        generate.build(chart_id)


def test_every_target_row_states_its_basis():
    table = generate.table()
    for chart_id in generate.GENERATED:
        for position, node in table[chart_id].items():
            assert node["basis"], f"{chart_id}/{position} has no stated basis"


def test_generated_widths_track_their_targets():
    for chart_id in generate.GENERATED:
        raw = json.loads((CHARTS_DIR / f"{chart_id}.json").read_text())
        for position, acts in raw["ranges"].items():
            node = generate.table()[chart_id][position]
            for action in ("raise", "call"):
                want = generate._target(node, position, action)
                got = Range.parse(acts[action]).n_combos
                # top_by_combos stops at the hand that first reaches the
                # target, so one hand of overshoot is expected.
                assert want <= got <= want + 12, f"{position}/{action}: {got} vs {want}"


def test_derived_vs_open_nodes_sit_inside_their_authored_neighbours():
    """HJ and SB facing an UTG open must be tighter than the CO and BTN
    nodes the authored chart already covers against the same opener."""
    derived, authored = load("vs_open_6max_100bb_derived"), load("vs_open_6max_100bb")

    def width(chart, node):
        return chart.action_range(node, "raise").pct + chart.action_range(node, "call").pct

    assert width(derived, "HJ_vs_UTG") < width(authored, "CO_vs_UTG")
    assert width(derived, "SB_vs_UTG") < width(authored, "SB_vs_HJ")


def test_vs_3bet_continues_less_than_it_opened():
    """A 4-bet-plus-call range that is wider than the opening range it came
    from would be arithmetically impossible."""
    rfi, three = load("rfi_6max_100bb"), load("vs_3bet_6max_100bb_derived")
    for position in three.positions:
        opened = rfi.action_range(position, "raise").n_combos
        kept = (three.action_range(position, "raise").n_combos
                + three.action_range(position, "call").n_combos)
        assert 0 < kept < opened
