"""Bankroll maths."""

import math
import random

import pytest

from pokerlab.bankroll import (
    LADDER, MOVE_UP_BUYINS, Measurement, bust_before_target, measure,
    readiness, risk_of_ruin,
)


@pytest.fixture
def sample():
    rng = random.Random(11)
    return [rng.gauss(0.05, 18.0) for _ in range(20_000)]


def test_buyin_is_100bb():
    assert LADDER[0].buyin == 5.0    # NL5
    assert LADDER[1].buyin == 10.0   # NL10


def test_measure_recovers_the_generating_parameters(sample):
    """Recovery is only ever within sampling error -- which is the point.

    At 20k hands and 180 bb/100 of variance the standard error on the winrate
    is ~12.7 bb/100, so asserting a tight bound here would be asserting
    something false about statistics.
    """
    m = measure(sample, [100.0] * len(sample))
    assert m.hands == len(sample)
    assert m.stdev == pytest.approx(180, rel=0.1)          # 18bb/hand -> 180bb/100
    assert abs(m.winrate - 5.0) < 3 * m.stderr             # 0.05bb/hand -> 5bb/100
    assert m.stderr == pytest.approx(m.stdev / math.sqrt(len(sample) / 100), rel=1e-6)
    assert m.max_drawdown > 0


def test_drawdown_is_peak_to_trough_not_first_to_last():
    # Ends up +10 overall, but dips 30 below an earlier peak on the way.
    m = measure([20.0, -30.0, 20.0], [100.0] * 3)
    assert m.max_drawdown == 30.0


def test_a_losing_player_is_ruined_with_certainty():
    assert risk_of_ruin(-1.0, 100.0, 10_000) == 1.0
    assert risk_of_ruin(0.0, 100.0, 10_000) == 1.0


def test_risk_of_ruin_falls_as_the_bankroll_grows():
    rors = [risk_of_ruin(5.0, 180.0, bi * 100) for bi in (10, 20, 40, 100)]
    assert rors == sorted(rors, reverse=True)
    assert all(0 <= r <= 1 for r in rors)


def test_risk_of_ruin_matches_the_closed_form():
    assert risk_of_ruin(5.0, 100.0, 4000) == pytest.approx(math.exp(-2 * 5 * 4000 / 100**2))


def test_bust_probability_falls_with_a_deeper_roll():
    losing = [-1.0] * 50 + [1.0] * 50
    shallow = bust_before_target(losing, 20, 40, trials=300, seed=3)
    deep = bust_before_target(losing, 200, 400, trials=300, seed=3)
    assert 0 <= deep <= shallow <= 1


def test_confidence_interval_widens_on_a_small_sample():
    wide = Measurement(200, 5.0, 180.0, 180.0 / math.sqrt(2), 0, 100)
    assert wide.ci[0] < 0 < wide.ci[1]
    assert not wide.proven_winner


def test_hands_to_resolve_scales_with_the_square_of_variance():
    low = Measurement(1000, 5, 90.0, 9, 0, 100).hands_to_resolve(5)
    high = Measurement(1000, 5, 180.0, 18, 0, 100).hands_to_resolve(5)
    assert high == pytest.approx(4 * low, rel=0.01)


def test_readiness_requires_every_criterion(sample):
    m = measure(sample, [100.0] * len(sample))
    thin = readiness(m, bankroll_eur=20.0, stake=LADDER[1], hands_at_stake=9_845)
    assert not thin.ready
    assert not thin.checks[f"bankroll >= {MOVE_UP_BUYINS} buy-ins"][0]

    rich = readiness(m, bankroll_eur=100_000.0, stake=LADDER[1], hands_at_stake=500_000)
    assert rich.ready
