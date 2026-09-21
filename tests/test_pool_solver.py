"""The solver panel, its presets, and the pool profile they are built from.

The pure parts (rule matching, node classification, digest stability) run
anywhere. The profile and page tests need the database; the lock tests need
the solver binary.
"""

from __future__ import annotations

import json

import pytest

from pokerlab.db.load import DEFAULT_DB
from pokerlab.solver import presets as P
from pokerlab.solver.bridge import Spot, available

needs_db = pytest.mark.skipif(not DEFAULT_DB.exists(), reason="database not built")
needs_solver = pytest.mark.skipif(not available(), reason="solver binary not built")


# --------------------------------------------------------------------------
# bridge: the new fields must not move a legacy digest
# --------------------------------------------------------------------------

def test_legacy_digest_is_unchanged_by_new_fields():
    """Every cached solve is keyed on the old payload; a spot that uses none of
    the new fields must hash exactly as before."""
    spot = Spot(oop_range="AA,KK", ip_range="QQ", board=("Ah", "Kd", "7c"),
                starting_pot=60, effective_stack=480)
    assert set(spot.payload()) == {
        "oop_range", "ip_range", "board", "starting_pot", "effective_stack", "bet_sizes",
        "raise_sizes", "max_iterations", "target_exploitability", "compress_memory",
        "rake_rate", "rake_cap", "max_memory_bytes", "action_path"}
    assert spot.digest == "70877861c76076c1"


def test_locks_and_per_player_sizes_enter_the_digest():
    base = Spot(oop_range="AA,KK", ip_range="QQ", board=("Ah", "Kd", "7c"),
                starting_pot=60, effective_stack=480)
    from dataclasses import replace
    a = replace(base, locks=({"path": [], "mode": "uniform", "actions": {"check": 1}},))
    b = replace(base, ip_bet_sizes={"flop": ["50%"], "turn": [], "river": []})
    c = replace(base, dry_run=True)
    assert len({base.digest, a.digest, b.digest}) == 3
    assert c.digest == base.digest, "a dry run asks the same question"


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------

def test_preset_roundtrip_and_rule_specificity():
    p = P.Preset(id="x", name="x", policy=[
        P.Rule("any", "vs_bet", mix={"fold": 0.5, "call": 0.5}),
        P.Rule("flop", "vs_bet", size="big", mix={"fold": 0.7, "call": 0.3}),
        P.Rule("flop", "vs_bet", pot_type="3bet", size="big", mix={"fold": 0.4, "call": 0.6}, lock=False),
    ])
    again = P.Preset.from_dict(json.loads(json.dumps(p.to_dict())))
    assert again == p
    assert p.rule_for("flop", "vs_bet", "srp", "ip", "big").mix["fold"] == 0.7
    assert p.rule_for("turn", "vs_bet", "srp", "ip", "big").mix["fold"] == 0.5
    # the disabled rule never wins, even though it is the most specific
    assert p.rule_for("flop", "vs_bet", "3bet", "ip", "big").mix["fold"] == 0.7
    assert p.rule_for("flop", "lead_unopened", "srp", "ip", "any") is None
    assert p.locks_anything


def _tree():
    """A hand-written flop listing: OOP villain acts first, hero IP has the lead."""
    return [
        {"path": [], "street": "flop", "player": "oop", "pot": 60, "actions": ["check", "bet 20"]},
        {"path": ["check"], "street": "flop", "player": "ip", "pot": 60, "actions": ["check", "bet 20", "bet 45"]},
        {"path": ["check", "bet 20"], "street": "flop", "player": "oop", "pot": 80, "actions": ["fold", "call", "raise 60"]},
        {"path": ["check", "bet 45"], "street": "flop", "player": "oop", "pot": 105, "actions": ["fold", "call", "raise 135"]},
        {"path": ["check", "bet 45", "raise 135"], "street": "flop", "player": "ip", "pot": 240, "actions": ["fold", "call", "allin 480"]},
        {"path": ["bet 20"], "street": "flop", "player": "ip", "pot": 80, "actions": ["fold", "call", "raise 60"]},
        {"path": ["bet 20", "raise 60"], "street": "flop", "player": "oop", "pot": 140, "actions": ["fold", "call", "allin 480"]},
        {"path": ["check", "check", "*"], "street": "turn", "player": "oop", "pot": 60, "actions": ["check", "bet 40"]},
        {"path": ["check", "bet 20", "call", "*"], "street": "turn", "player": "oop", "pot": 100, "actions": ["check", "bet 66"]},
        {"path": ["check", "bet 20", "call", "*", "check"], "street": "turn", "player": "ip", "pot": 100, "actions": ["check", "bet 66"]},
    ]


def test_classify_reads_lead_situation_and_size_from_the_path():
    by = {tuple(n["path"]): n for n in _tree()}
    c = lambda path: P.classify(path, by, 60, hero_is_oop=False, hero_is_lead=True)  # noqa: E731
    assert c([])["situation"] == "nonlead_first"                       # villain OOP, hero leads
    assert c(["check"])["situation"] == "lead_unopened"                 # hero, checked to
    small = c(["check", "bet 20"])
    assert (small["situation"], small["size"]) == ("vs_bet", "small")  # 20 into 60 = 33%
    big = c(["check", "bet 45"])
    assert (big["situation"], big["size"]) == ("vs_bet", "mid")        # 45 into 60 = 75%
    assert c(["bet 20", "raise 60"])["situation"] == "vs_raise"        # villain donked, got raised
    assert c(["check", "check", "*"])["situation"] == "nolead_unopened"  # flop checked through
    assert c(["check", "bet 20", "call", "*"])["situation"] == "nonlead_first"
    assert c(["check", "bet 20", "call", "*", "check"])["situation"] == "lead_unopened"


def test_locks_for_only_locks_villain_nodes_with_offered_actions():
    preset = P.Preset(id="t", name="t", policy=[
        P.Rule("any", "nonlead_first", mix={"bet": 0.2, "check": 0.8}),
        P.Rule("any", "vs_bet", mix={"fold": 0.5, "call": 0.4, "raise": 0.1}),
        P.Rule("any", "vs_raise", mix={"fold": 0.3, "call": 0.6, "raise": 0.1}),
    ])
    locks, applied = P.locks_for(preset, _tree(), 60, hero_is_oop=False, hero_is_lead=True, pot_type="srp")
    paths = {tuple(l["path"]) for l in locks}
    assert () in paths and ("check", "bet 20") in paths and ("bet 20", "raise 60") in paths
    assert ("check",) not in paths, "hero's node is never locked"
    assert all(l["mode"] == "ranked" for l in locks)
    # a node with no rule is reported, not locked
    free = [a for a in applied if a["rule"] is None]
    assert {a["situation"] for a in free} == {"nolead_unopened"}
    # 'raise' is dropped where the node offers only an all-in? no: all-in counts as a raise
    lock = next(l for l in locks if l["path"] == ["bet 20", "raise 60"])
    assert "raise" in lock["actions"]


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------

def test_bucket_mdf_and_naming_rules():
    from pokerlab.stats import pool_profile as pp
    assert pp._bucket(0.33) == "30-44" and pp._bucket(1.5) == "110+" and pp._bucket(None) is None
    assert 0.26 < pp._mdf_fold("30-44") < 0.28        # midpoint 37.5% → 27% fold allowed
    assert pp._name({"vpip": 0.8, "pfr": 0.3, "afq": 0.3, "fold_cbet": 0.5}) == "maniac"
    assert pp._name({"vpip": 0.42, "pfr": 0.04, "afq": 0.18, "fold_cbet": 0.56}) == "loose-passive"
    assert pp._name({"vpip": 0.30, "pfr": 0.20, "afq": 0.26, "fold_cbet": 0.35}) == "sticky"
    assert pp._name({"vpip": 0.22, "pfr": 0.14, "afq": 0.27, "fold_cbet": 0.63}) == "tight-aggressive"
    assert pp._name({"vpip": 0.21, "pfr": 0.12, "afq": 0.16, "fold_cbet": 0.62}) == "tight-passive"


@needs_db
def test_profile_measures_the_pool_with_samples(tmp_path, monkeypatch):
    import duckdb
    from pokerlab.stats import pool_profile as pp
    monkeypatch.setattr(pp, "CACHE", tmp_path / "profile.json")
    con = duckdb.connect(str(DEFAULT_DB), read_only=True)
    prof = pp.load(con)
    assert prof.hands > 5000
    assert abs(sum(t.share for t in prof.tiers) - 1.0) < 1e-6
    cbet = prof.node("cbet", "srp", "ip")
    assert cbet and cbet.n >= 500 and 0.4 < cbet.mix["bet"] < 0.8
    assert all(s.n >= 30 for s in prof.size_response)
    # the river rule is the LEAD's bet: nothing like the fold to a probe after checks
    river_big = [s for s in prof.size_response if s.street == "RIVER" and s.bucket == "110+"]
    assert river_big and river_big[0].fold > river_big[0].mdf_fold
    # cached on disk keyed by the hand count, and identical on reload
    again = pp.load(con)
    assert again.built_at == prof.built_at
    if prof.archetypes:
        assert abs(sum(a.share for a in prof.archetypes) - 1.0) < 1e-6
        assert {a.name for a in prof.archetypes} <= {"maniac", "loose-passive", "sticky",
                                                       "tight-aggressive", "tight-passive"}


@needs_db
def test_builtin_presets_cover_every_villain_node(tmp_path, monkeypatch):
    import duckdb
    from pokerlab.stats import pool_profile as pp
    monkeypatch.setattr(pp, "CACHE", tmp_path / "profile.json")
    con = duckdb.connect(str(DEFAULT_DB), read_only=True)
    prof = pp.load(con)
    presets = P.builtin(prof)
    ids = [p.id for p in presets]
    assert ids[:2] == ["equilibrium", "nl5-pool"] and "vs-unknown" in ids
    pool = presets[1]
    assert all(s in pool.villain_bets for s in P.STREETS)
    assert pool.villain_raises["turn"] == [] and pool.villain_raises["river"] == []
    locks, applied = P.locks_for(pool, _tree(), 60, hero_is_oop=False, hero_is_lead=True, pot_type="srp")
    assert len(locks) == sum(1 for a in applied if a["rule"] is not None) == len(applied)
    # every rule carries its sample and its origin
    assert all(r.n >= 30 and r.basis for r in pool.policy)


# --------------------------------------------------------------------------
# solver: locks change the answer, and a dry run allocates nothing
# --------------------------------------------------------------------------

@needs_solver
def test_dry_run_lists_nodes_and_per_player_sizes():
    from pokerlab.solver.bridge import dry_run
    spot = Spot(oop_range="AA,KK,QQ,JJ,TT", ip_range="AA,KK,QQ,AKs", board=("Ah", "Kd", "7c"),
                starting_pot=60, effective_stack=200,
                bet_sizes={"flop": ["33%", "75%"], "turn": ["66%"], "river": ["75%"]},
                raise_sizes={"flop": ["2.5x"], "turn": [], "river": []},
                ip_bet_sizes={"flop": ["50%"], "turn": ["50%"], "river": ["50%"]})
    d = dry_run(spot)
    assert d.tree and d.memory_usage_bytes > 0 and d.iterations == 0
    root = d.tree[0]
    assert root["path"] == [] and root["player"] == "oop" and root["actions"] == ["check", "bet 20", "bet 45"]
    ip = next(n for n in d.tree if n["path"] == ["check"])
    assert ip["actions"] == ["check", "bet 30"], "IP got its own 50% menu"
    assert any(n["street"] == "river" for n in d.tree)


@needs_solver
def test_uniform_and_ranked_locks_change_the_best_response():
    from dataclasses import replace
    from pokerlab.solver.bridge import solve
    base = Spot(oop_range="22+,A2s+,K9s+,QTs+,ATo+,KQo",
                ip_range="22+,A2s+,K5s+,Q9s+,J9s+,T9s,A9o+,KTo+",
                board=("Ah", "Kd", "7c", "2s", "9d"), starting_pot=100, effective_stack=300,
                bet_sizes={"flop": [], "turn": [], "river": ["75%"]},
                raise_sizes={"flop": [], "turn": [], "river": ["2.5x"]},
                max_iterations=300, target_exploitability=0.3, report_paths=(("bet 75",),))
    eq = solve(base, cache=False)
    uniform = solve(replace(base, locks=({"path": ["bet 75"], "mode": "uniform",
                                          "actions": {"fold": 0.5, "call": 0.42, "raise": 0.08}},)), cache=False)
    ranked = solve(replace(base, locks=({"path": ["bet 75"], "mode": "ranked",
                                         "actions": {"fold": 0.5, "call": 0.42, "raise": 0.08}},)), cache=False)
    assert eq.locked_nodes == 0 and uniform.locked_nodes == 1 and ranked.locked_nodes == 1
    assert uniform.reports[0]["locked"] and abs(uniform.reports[0]["aggregate"]["fold"] - 0.5) < 0.01
    # folding half of every hand, sets included, is punished with a bet every time;
    # folding the weakest half is not
    assert uniform.aggregate["bet 75"] > 0.95
    assert 0.2 < ranked.aggregate["bet 75"] < 0.9
    assert uniform.trustworthy, "the lock notice must not read as a warning"
    # the ranked lock put the raises on the strongest hands
    rows = sorted(ranked.reports[0]["strategy"], key=lambda r: -r["equity"])
    top = [r for r in rows if r["weight"] > 0][:3]
    assert all(r["actions"].get("raise 188", 0) + r["actions"].get("allin 300", 0) > 0.99 for r in top)


@needs_solver
def test_lock_refuses_an_action_the_node_does_not_offer():
    from dataclasses import replace
    from pokerlab.solver.bridge import SolverError, solve
    base = Spot(oop_range="AA,KK", ip_range="QQ,JJ", board=("Ah", "Kd", "7c", "2s", "9d"),
                starting_pot=100, effective_stack=300,
                bet_sizes={"flop": [], "turn": [], "river": ["75%"]},
                raise_sizes={"flop": [], "turn": [], "river": []})
    with pytest.raises(SolverError, match="not offered"):
        solve(replace(base, locks=({"path": ["bet 75"], "mode": "uniform",
                                    "actions": {"fold": 0.5, "check": 0.5}},)), cache=False)


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@needs_db
def test_pool_and_solver_pages_render(tmp_path, monkeypatch):
    from pokerlab.stats import pool_profile as pp
    monkeypatch.setattr(pp, "CACHE", tmp_path / "profile.json")
    monkeypatch.setattr(P, "STORE", tmp_path / "presets.json")
    from pokerlab.web import state
    from pokerlab.web.app import build_app
    build_app(DEFAULT_DB)
    state._state.pop("profile", None)
    state._state.pop("presets", None)
    from pokerlab.web.pool import pool_page
    body = pool_page().body.decode()
    assert "Who is at the table" in body and "fold allowed by MDF" in body.lower() or "MDF" in body
    from pokerlab.web import solver as S
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/solver", "query_string": b"", "headers": []}
    page = S.solver_page(Request(scope)).body.decode()
    assert "Estimate tree" in page and "NL5 pool (measured)" in page and "pool · BB_vs_BTN" in page
    assert "(0% of hands" not in page
    assert S.presets_index().status_code == 200
    assert S.preset_page("nl5-pool").status_code == 200
    assert (tmp_path / "presets.json").exists()
