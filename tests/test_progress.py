"""Spaced repetition scheduling and persistence."""

import json

import pytest

from pokerlab.trainer.progress import MIN_EASE, START_EASE, STEPS, Card, Progress


def test_correct_answers_lengthen_the_interval():
    c, p = Card("k"), 0
    intervals = []
    for p in range(1, 7):
        c.review(True, p)
        intervals.append(c.interval)
    assert intervals[:2] == list(STEPS)
    assert intervals == sorted(intervals), "intervals must never shrink on success"


def test_a_lapse_resets_the_card_and_lowers_ease():
    c = Card("k")
    for i in range(4):
        c.review(True, i)
    grown = c.interval
    c.review(False, 5)
    assert c.interval == 1 and c.reps == 0 and c.lapses == 1
    assert c.interval < grown
    assert c.ease < START_EASE + 0.4


def test_ease_has_a_floor():
    c = Card("k")
    for i in range(40):
        c.review(False, i)
    assert c.ease == pytest.approx(MIN_EASE)


def test_due_ordering_puts_the_worst_remembered_first(tmp_path):
    p = Progress()
    for _ in range(6):
        p.review("good", True)
    for _ in range(6):
        p.review("bad", False)
    p.counter += 10_000  # everything is due
    assert p.due()[0].key == "bad"


def test_round_trips_through_disk(tmp_path):
    p = Progress()
    p.review("a|BTN|AKs", True)
    p.review("a|BTN|72o", False)
    path = tmp_path / "p.json"
    p.save(path)
    back = Progress.load(path)
    assert back.stats == p.stats
    assert back.cards["a|BTN|AKs"].reps == 1


def test_corrupt_progress_does_not_block_a_drill(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{not json")
    assert Progress.load(path).stats["answered"] == 0


def test_missing_file_starts_clean(tmp_path):
    assert Progress.load(tmp_path / "nope.json").cards == {}


def test_save_is_atomic(tmp_path):
    """A crash mid-write must not be able to destroy existing history."""
    path = tmp_path / "p.json"
    p = Progress()
    p.review("a|BTN|AA", True)
    p.save(path)
    assert not path.with_suffix(".tmp").exists()
    assert json.loads(path.read_text())["cards"]
