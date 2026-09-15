"""Solver bridge. Skips when the Rust binary has not been built."""

import pytest

from pokerlab.solver.bridge import Solution, Spot, SolverError, available, solve

pytestmark = pytest.mark.skipif(not available(), reason="solver binary not built")

RIVER = dict(oop_range="QQ+, AKs", ip_range="JJ+, AQs+",
             board=("Ah", "Kd", "7c", "2s", "9h"), starting_pot=60, effective_stack=200)


@pytest.fixture(scope="module")
def solved():
    return solve(Spot(**RIVER, bet_sizes={"river": ["50%"]},
                      raise_sizes={"river": ["2.5x"]}), cache=False)


def test_solve_converges(solved):
    assert solved.converged
    assert solved.exploitability_pct_pot < 1.0
    assert solved.trustworthy


def test_evs_sum_to_the_pot(solved):
    """Zero rake: whatever one player does not win, the other does."""
    assert solved.oop_ev + solved.ip_ev == pytest.approx(60, abs=0.01)


def test_strategy_frequencies_sum_to_one(solved):
    for row in solved.root_strategy[:20]:
        assert sum(row["actions"].values()) == pytest.approx(1.0, abs=1e-3)


def test_an_empty_betting_tree_is_not_reported_as_a_perfect_solve():
    """The mirror image of the unconverged trap.

    With no bet sizes the tree has no betting, so exploitability is trivially
    zero at zero iterations -- which looks like a flawless solve. It must not
    pass as trustworthy.
    """
    s = solve(Spot(**RIVER, bet_sizes={"river": []}, raise_sizes={"river": []}), cache=False)
    assert s.exploitability == 0.0 and s.iterations == 0
    assert s.warnings and not s.trustworthy


def test_caching_returns_an_identical_solution(tmp_path, monkeypatch):
    from pokerlab.solver import bridge
    monkeypatch.setattr(bridge, "CACHE_DIR", tmp_path)
    spot = Spot(**RIVER, bet_sizes={"river": ["50%"]}, raise_sizes={"river": ["2.5x"]})
    first = bridge.solve(spot)
    assert (tmp_path / f"{spot.digest}.json").exists()
    assert bridge.solve(spot) == first


def test_spot_digest_is_content_addressed():
    a = Spot(**RIVER, bet_sizes={"river": ["50%"]})
    b = Spot(**RIVER, bet_sizes={"river": ["75%"]})
    assert a.digest != b.digest
    assert a.digest == Spot(**RIVER, bet_sizes={"river": ["50%"]}).digest


def test_memory_budget_is_not_part_of_the_cache_key():
    """A guard, not an input: raising the budget must not orphan every cached solve."""
    a = Spot(**RIVER, max_memory_bytes=2_000_000_000)
    b = Spot(**RIVER, max_memory_bytes=6_000_000_000)
    assert a.digest == b.digest
    assert a.digest != Spot(**RIVER, rake_rate=0.055, rake_cap=100).digest


def test_cache_file_records_the_question(tmp_path, monkeypatch):
    import json
    from pokerlab.solver import bridge
    monkeypatch.setattr(bridge, "CACHE_DIR", tmp_path)
    spot = Spot(**RIVER, bet_sizes={"river": ["50%"]}, raise_sizes={"river": []})
    bridge.solve(spot)
    stored = json.loads((tmp_path / f"{spot.digest}.json").read_text())
    assert stored["input"] == spot.payload()


def test_rake_lowers_both_players_ev():
    free = solve(Spot(**RIVER, bet_sizes={"river": ["50%"]}, raise_sizes={"river": []}), cache=False)
    raked = solve(Spot(**RIVER, bet_sizes={"river": ["50%"]}, raise_sizes={"river": []},
                       rake_rate=0.055, rake_cap=100), cache=False)
    assert raked.oop_ev + raked.ip_ev < free.oop_ev + free.ip_ev


def test_a_bad_range_is_an_error_not_a_fabricated_result():
    with pytest.raises(SolverError):
        solve(Spot(oop_range="not a range", ip_range="AA",
                   board=("Ah", "Kd", "7c", "2s", "9h"),
                   starting_pot=60, effective_stack=200), cache=False)


# --------------------------------------------------------------------------
# node walking: reading a decision that is not the first one of the street
# --------------------------------------------------------------------------

def _spot(**kw):
    from pokerlab.solver.bridge import Spot
    base = dict(
        oop_range="AA,KK,QQ,AKs,76s", ip_range="JJ,TT,AQs,A5s,65s",
        board=("Ah", "Kd", "7c"), starting_pot=60, effective_stack=200,
        bet_sizes={"flop": ["50%"], "turn": ["66%"], "river": ["75%"]},
        raise_sizes={"flop": ["2.5x"], "turn": ["2.5x"], "river": ["2.5x"]},
        max_iterations=60,
    )
    base.update(kw)
    return Spot(**base)


@pytest.mark.skipif(not available(), reason="solver binary not built")
def test_action_path_moves_the_reported_node():
    """Without this, only the first decision of a street can be examined --
    and most real decisions are not the first one."""
    from pokerlab.solver.bridge import solve
    root = solve(_spot(), cache=False)
    facing = solve(_spot(action_path=("bet 30",)), cache=False)

    assert root.root_player == "oop" and facing.root_player == "ip"
    assert facing.node_path == ["bet 30"]
    assert "fold" in facing.root_actions and "call" in facing.root_actions


@pytest.mark.skipif(not available(), reason="solver binary not built")
def test_per_action_ev_is_reported_with_fold_as_the_baseline():
    from pokerlab.solver.bridge import solve
    sol = solve(_spot(action_path=("bet 30",)), cache=False)
    ev = sol.ev_for("Ac5c")
    assert set(ev) == set(sol.root_actions)
    assert ev["fold"] == 0.0                 # folding is the zero by construction
    assert ev["call"] > ev["fold"]           # the nut flush draw is not a fold


@pytest.mark.skipif(not available(), reason="solver binary not built")
def test_nearest_size_substitution_is_reported_not_hidden():
    """A tree cannot hold every size a real hand used. When it substitutes one,
    the caller has to be told the solve answered a slightly different question."""
    from pokerlab.solver.bridge import solve
    sol = solve(_spot(action_path=("bet~41",)), cache=False)
    assert sol.node_path == ["bet 30"]
    assert any("bet~41" in w and "bet 30" in w for w in sol.warnings)
    assert not sol.trustworthy               # a warning must never pass silently


@pytest.mark.skipif(not available(), reason="solver binary not built")
def test_an_impossible_path_fails_loudly():
    from pokerlab.solver.bridge import SolverError, solve
    with pytest.raises(SolverError, match="not available"):
        solve(_spot(action_path=("raise 300",)), cache=False)


@pytest.mark.skipif(not available(), reason="solver binary not built")
def test_memory_budget_refuses_before_allocating():
    """A deep stack in a small pot builds a tree of many gigabytes. The size is
    known before any memory is committed, so it must be refused in milliseconds
    rather than discovered by paging the machine."""
    from pokerlab.solver.bridge import SolverError, solve
    with pytest.raises(SolverError, match="budget"):
        solve(_spot(oop_range="22+,A2s+,K2s+,Q2s+,J2s+,T2s+,92s+,82s+,72s+,62s+,52s+,42s+,32s",
                    ip_range="22+,A2s+,K2s+,Q2s+,J2s+,T2s+,92s+,82s+,72s+,62s+,52s+,42s+,32s",
                    starting_pot=10, effective_stack=2000,
                    max_memory_bytes=50_000_000), cache=False)
