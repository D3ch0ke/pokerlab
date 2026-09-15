"""Phase 1 gate: the money model must reproduce every NL5 hand on disk.

Skipped when the exports are not present, so the suite still runs anywhere.
"""

from pathlib import Path

import pytest

from pokerlab.parse.betclic import parse_hand
from pokerlab.parse.corpus import NL5, collect, default_sources

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def nl5_hands():
    sources = default_sources(ROOT)
    if not sources:
        pytest.skip("no Betclic exports present")
    blocks = collect(sources, games=(NL5,))
    if not blocks:
        pytest.skip("no NL5 hands found")
    return [parse_hand(b) for b in blocks.values()]


def test_every_hand_reconciles(nl5_hands):
    failures = []
    for h in nl5_hands:
        contributed = sum(h.contributions.values())
        if (h.uncalled < 0
                or contributed - h.uncalled != h.total_pot
                or sum(h.collected.values()) != h.total_pot - h.rake
                or sum(h.net(s.name) for s in h.seats) != -h.rake):
            failures.append(h.hand_id)
    assert not failures, f"{len(failures)} of {len(nl5_hands)} failed, e.g. {failures[:3]}"


def test_every_hand_has_exactly_one_hero(nl5_hands):
    assert all(h.hero is not None for h in nl5_hands)
    # One screen name across the whole corpus, whatever it is.
    assert len({h.hero.name for h in nl5_hands}) == 1
