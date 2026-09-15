"""Board texture and postflop statistics."""

import pytest

from pokerlab.texture import classify


@pytest.mark.parametrize("board,paired,suits,high,connect", [
    ("Ah Kd 4c", False, "rainbow", "A", "disconnected"),
    ("9h 8h 7c", False, "two-tone", "low", "connected"),
    ("Qs Qd 3h", True, "rainbow", "Q", "paired"),   # s/d/h = three suits
    ("2h 5h 9h", False, "monotone", "low", "gapped"),
    ("Td 9s 8c", False, "rainbow", "T", "connected"),
])
def test_classification(board, paired, suits, high, connect):
    t = classify(board)
    assert (t.paired, t.suitedness, t.high, t.connectivity) == (paired, suits, high, connect)


def test_wetness_needs_a_draw_and_no_pair():
    assert classify("9h 8h 7c").wet          # connected and two-tone
    assert classify("2h 5h 9h").wet          # monotone
    assert not classify("Ah Kd 4c").wet      # rainbow, disconnected
    assert not classify("Qs Qd 3h").wet      # paired boards are never "wet" here


def test_extra_board_cards_are_ignored():
    """Texture is a property of the flop; the runout must not change it."""
    assert classify("9h 8h 7c 2d Kd") == classify("9h 8h 7c")


def test_a_short_board_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        classify("Ah Kd")


def test_label_is_stable_and_readable():
    assert classify("Qs Qd 3h").label == "paired (Q-high)"
    assert classify("9h 8h 7c").label == "two-tone connected"


def test_texture_dimension_is_validated():
    """A typo'd dimension must fail loudly, not silently group by nothing."""
    import duckdb

    from pokerlab.db.load import SCHEMA
    from pokerlab.stats.core import EPOCH, FOREVER
    from pokerlab.stats.postflop import by_texture
    con = duckdb.connect()
    con.execute(SCHEMA.read_text())
    with pytest.raises(ValueError):
        by_texture(con, EPOCH, FOREVER, "wetness")
