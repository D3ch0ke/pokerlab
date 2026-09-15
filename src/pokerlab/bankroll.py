"""Bankroll measurement grounded in hero's own results.

Generic "40 buy-ins" advice assumes a generic variance. This module measures
the real one and is blunt about what the sample can and cannot establish --
in particular, whether hero can yet be distinguished from a losing player.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass

CONFIDENCE_Z = 1.96


@dataclass(frozen=True, slots=True)
class Stake:
    name: str
    bb_eur: float

    @property
    def buyin(self) -> float:
        """A standard 100bb buy-in."""
        return round(100 * self.bb_eur, 2)


LADDER = (Stake("NL5", 0.05), Stake("NL10", 0.10), Stake("NL25", 0.25), Stake("NL50", 0.50))

#: Strict cash-game thresholds, in buy-ins.
MOVE_UP_BUYINS = 40
MOVE_DOWN_BUYINS = 25
#: Hands at a stake before a positive result is treated as evidence of a
#: winrate. Pragmatic, not statistical -- see `hands_to_resolve`.
MOVE_UP_HANDS = 25_000


@dataclass(frozen=True, slots=True)
class Measurement:
    hands: int
    winrate: float        # bb/100
    stdev: float          # bb/100
    stderr: float         # bb/100
    max_drawdown: float   # bb
    avg_stack: float      # bb

    # Note on avg_stack: on Betclic you can only sit for 100bb and auto-rebuy
    # tops you back up, so this is always >= 100 and the excess is won pots.
    # It is NOT a buy-in decision, and measured variance barely moves with it:
    # 176 bb/100 at 100-125bb vs 183 overall. Do not read it as a leak.

    @property
    def ci(self) -> tuple[float, float]:
        return (self.winrate - CONFIDENCE_Z * self.stderr,
                self.winrate + CONFIDENCE_Z * self.stderr)

    @property
    def proven_winner(self) -> bool:
        """Is the whole confidence interval above zero?"""
        return self.ci[0] > 0

    def hands_to_resolve(self, precision: float = 5.0) -> int:
        """Hands needed to pin the winrate to +/- `precision` bb/100."""
        return int((CONFIDENCE_Z * self.stdev / precision) ** 2 * 100)


def measure(net_bb: list[float], stacks_bb: list[float]) -> Measurement:
    n = len(net_bb)
    sd100 = statistics.stdev(net_bb) * 10 if n > 1 else 0.0
    cum = peak = mdd = 0.0
    for x in net_bb:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return Measurement(
        hands=n,
        winrate=100 * sum(net_bb) / n if n else 0.0,
        stdev=sd100,
        stderr=sd100 / math.sqrt(n / 100) if n >= 100 else float("inf"),
        max_drawdown=mdd,
        avg_stack=statistics.mean(stacks_bb) if stacks_bb else 0.0,
    )


def risk_of_ruin(winrate: float, stdev: float, bankroll_bb: float) -> float:
    """Probability of losing it all, playing forever and never withdrawing.

    The standard exponential approximation. A losing player ruins with
    certainty, which is why a positive winrate is load-bearing here.
    """
    if winrate <= 0:
        return 1.0
    return min(1.0, math.exp(-2 * winrate * bankroll_bb / stdev ** 2))


def bust_before_target(net_bb: list[float], roll_bb: float, target_bb: float,
                       trials: int = 4000, seed: int = 7) -> float:
    """P(reaching zero before `target_bb`), bootstrapped from real hands.

    This models a deposit-and-cash-out cycle rather than an infinite
    bankroll, which is what a "put in EUR10, withdraw at EUR20" plan is.
    """
    rng = random.Random(seed)
    n = len(net_bb)
    busts = 0
    for _ in range(trials):
        stack = roll_bb
        for _ in range(50_000):
            stack += net_bb[rng.randrange(n)]
            if stack <= 0:
                busts += 1
                break
            if stack >= target_bb:
                break
    return busts / trials


@dataclass(frozen=True, slots=True)
class Readiness:
    stake: Stake
    bankroll_eur: float
    hands: int
    checks: dict[str, tuple[bool, str]]

    @property
    def ready(self) -> bool:
        return all(ok for ok, _ in self.checks.values())


def readiness(m: Measurement, bankroll_eur: float, stake: Stake,
              hands_at_stake: int) -> Readiness:
    buyins = bankroll_eur / stake.buyin if stake.buyin else 0
    return Readiness(stake, bankroll_eur, hands_at_stake, {
        f"bankroll >= {MOVE_UP_BUYINS} buy-ins": (
            buyins >= MOVE_UP_BUYINS,
            f"{buyins:.1f} buy-ins (EUR {bankroll_eur:.2f} / EUR {stake.buyin:.2f})"),
        f"{MOVE_UP_HANDS:,}+ hands at the stake below": (
            hands_at_stake >= MOVE_UP_HANDS,
            f"{hands_at_stake:,} hands"),
        "positive result over that sample": (
            m.winrate > 0, f"{m.winrate:+.2f} bb/100"),
        "can absorb your worst historical drawdown": (
            bankroll_eur >= m.max_drawdown * stake.bb_eur,
            f"worst was {m.max_drawdown:.0f}bb = EUR "
            f"{m.max_drawdown * stake.bb_eur:.2f} at {stake.name}"),
    })
