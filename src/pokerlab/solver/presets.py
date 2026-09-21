"""Solver presets: every assumption a solve rests on, in one editable bundle.

A solve is only as good as its inputs, and the inputs are many: two size
menus, the rake, the convergence target, and -- the pool-specific part -- how
the opponent is assumed to play at every node. A preset holds all of them so
a spot can be solved "against equilibrium", "against the NL5 pool as measured"
or "against a sticky regular" by changing one selector, and so the numbers
behind each are on the page and editable, never buried in code.

The pool part is the `policy`: a list of rules, each the pool's measured mix
at one kind of node (street, pot type, position, situation, size faced). When
a preset is applied to a tree, every villain decision node is classified and
locked to the matching rule with the CLI's "ranked" mode -- the mix is dealt
strongest-first by equity on the board, softened towards uniform by a
per-street `blend` calibrated on what villains actually showed down (river
bets are 5% air, flop bets 28%). A rule with no mix leaves its nodes free,
which is what the equilibrium preset does everywhere.

Presets live in `data/solver_presets.json`. The built-in ones are regenerated
from the pool profile when the file is missing; user edits are kept.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..stats.pool_profile import Profile

STORE = Path("data/solver_presets.json")

STREETS = ("flop", "turn", "river")
SITUATIONS = (
    ("lead_unopened", "has the lead, first to act or checked to: bet or check"),
    ("vs_bet", "faces a bet from the lead: fold, call or raise"),
    ("nonlead_first", "out of position without the lead, first to act: donk or check"),
    ("nonlead_after_check", "the lead checked to them: stab or check"),
    ("lead_vs_stab", "has the lead, checked, now faces a bet"),
    ("vs_raise", "bet and was raised"),
    ("nolead_unopened", "nobody leads (previous street checked through), unopened: bet or check"),
    ("vs_bet_nolead", "faces a bet on a street nobody leads"),
)
SIZES = ("small", "mid", "big")      # < 45%, 45-79%, 80%+ of pot


@dataclass(slots=True)
class Rule:
    street: str                 # flop | turn | river | any
    situation: str
    pot_type: str = "any"       # srp | 3bet | any
    position: str = "any"       # ip | oop | any
    size: str = "any"           # small | mid | big | any  (size faced)
    n: int = 0
    mix: dict[str, float] = field(default_factory=dict)   # bet/check or fold/call/raise
    lock: bool = True
    basis: str = ""

    def specificity(self) -> int:
        return sum(v != "any" for v in (self.street, self.pot_type, self.position, self.size))


@dataclass(slots=True)
class Preset:
    id: str
    name: str
    description: str = ""
    basis: str = ""
    builtin: bool = False
    # Defaults are the tree that fits 6 GB at 100bb: two flop sizes, one on the
    # turn and river, and raises on the flop only. Measured 2026-09-12: turn and
    # river raises rarely move the flop answer, the flop raise does (up to 50
    # points), and every extra branch carries a whole turn and river under it.
    hero_bets: dict[str, list[str]] = field(default_factory=lambda: {"flop": ["33%", "75%"], "turn": ["66%"], "river": ["75%"]})
    hero_raises: dict[str, list[str]] = field(default_factory=lambda: {"flop": ["2.5x"], "turn": [], "river": []})
    villain_bets: dict[str, list[str]] = field(default_factory=lambda: {"flop": ["33%", "66%"], "turn": ["50%"], "river": ["66%"]})
    villain_raises: dict[str, list[str]] = field(default_factory=lambda: {"flop": ["3x"], "turn": [], "river": []})
    donk_sizes: dict[str, list[str]] = field(default_factory=lambda: {"turn": [], "river": []})
    policy: list[Rule] = field(default_factory=list)
    blend: dict[str, float] = field(default_factory=lambda: {"flop": 0.5, "turn": 0.3, "river": 0.1})
    width_scale: dict[str, float] = field(default_factory=lambda: {"call": 1.0, "raise": 1.0})
    """Multiplies the villain's measured preflop width: an archetype that calls
    1.4x as often as the pool is put on a range 1.4x as wide."""
    max_iterations: int = 300
    target_exploitability: float = 0.5
    rake_rate: float = 0.055
    rake_cap: int = 100
    memory_budget_gb: float = 6.0
    compress_memory: bool = False
    add_allin_threshold: float = 1.5
    force_allin_threshold: float = 0.15
    merging_threshold: float = 0.1

    @property
    def locks_anything(self) -> bool:
        return any(r.lock and r.mix for r in self.policy)

    def rule_for(self, street: str, situation: str, pot_type: str, position: str,
                 size: str) -> Rule | None:
        """The most specific enabled rule matching a node, or None."""
        best = None
        for r in self.policy:
            if not r.lock or not r.mix or r.situation != situation:
                continue
            if r.street not in ("any", street) or r.pot_type not in ("any", pot_type):
                continue
            if r.position not in ("any", position) or r.size not in ("any", size):
                continue
            if best is None or r.specificity() > best.specificity():
                best = r
        return best

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Preset":
        d = dict(d)
        d["policy"] = [Rule(**r) for r in d.get("policy", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "preset"


def load_all(profile: Profile | None = None) -> list[Preset]:
    if STORE.exists():
        try:
            return [Preset.from_dict(d) for d in json.loads(STORE.read_text())]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
    presets = builtin(profile) if profile else [equilibrium()]
    save_all(presets)
    return presets


def save_all(presets: list[Preset]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps([p.to_dict() for p in presets], indent=1))
    tmp.replace(STORE)


def get(presets: list[Preset], pid: str) -> Preset | None:
    return next((p for p in presets if p.id == pid), None)


def upsert(presets: list[Preset], preset: Preset) -> list[Preset]:
    out = [p for p in presets if p.id != preset.id]
    out.append(preset)
    return out


def regenerate_builtins(presets: list[Preset], profile: Profile) -> list[Preset]:
    """Rebuild the built-in presets from a fresh profile; user presets untouched."""
    keep = [p for p in presets if not p.builtin]
    return builtin(profile) + keep


# --------------------------------------------------------------------------
# built-in presets from the measured profile
# --------------------------------------------------------------------------

def equilibrium() -> Preset:
    return Preset(
        id="equilibrium", name="Equilibrium (no pool)", builtin=True,
        description="Both players free at every node: the game-theory answer for the tree.",
        basis="No pool assumption. Sizes are a common menu, rake as measured on Betclic NL5.",
    )


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _sizes_from(profile: Profile) -> dict[str, list[str]]:
    """The sizes the pool actually uses: its 25th and 75th percentile flop
    bet, and its median turn and river bet, rounded to 5% of pot. One size on
    the later streets keeps a 100bb tree inside the budget."""
    out = {}
    for street, node in (("flop", "cbet"), ("turn", "barrel"), ("river", "river_bet")):
        n = profile.node(node, "srp", "any")
        if n and n.size:
            q25, med, q75 = n.size
            lo, mid, hi = (max(20, min(5 * round(20 * x), 200)) for x in (q25, med, q75))
            if street == "flop":
                out[street] = [f"{lo}%"] if hi - lo < 15 else [f"{lo}%", f"{hi}%"]
            else:
                out[street] = [f"{mid}%"]
        else:
            out[street] = ["50%"]
    return out


def _raise_from(profile: Profile) -> dict[str, list[str]]:
    """The pool's median flop raise; no turn or river raises (see Preset)."""
    n = profile.node("vs_cbet", "srp", "any")
    x = n.raise_x if n and n.raise_x else 3.0
    return {"flop": [f"{max(2.0, min(round(x * 2) / 2, 5.0)):g}x"], "turn": [], "river": []}


def pool_policy(profile: Profile, scale_fold: dict[str, float] | None = None) -> list[Rule]:
    """Rules from the profile: sized responses per street, lead/non-lead nodes
    per street × pot type × position. `scale_fold` multiplies fold shares per
    street (an archetype's fold rate over the pool's) and renormalises."""
    rules: list[Rule] = []
    by = {}
    for s in profile.size_response:
        by.setdefault(s.street, {})[s.bucket] = s
    coarse = {"small": ("<30", "30-44"), "mid": ("45-59", "60-79"), "big": ("80-109", "110+")}
    for street in ("FLOP", "TURN", "RIVER"):
        for size, buckets in coarse.items():
            rows = [by.get(street, {}).get(b) for b in buckets]
            rows = [r for r in rows if r]
            n = sum(r.n for r in rows)
            if n < 30:
                continue
            fold = sum(r.fold * r.n for r in rows) / n
            raise_ = sum(r.raise_ * r.n for r in rows) / n
            call = 1 - fold - raise_
            if scale_fold and street.lower() in scale_fold:
                k = scale_fold[street.lower()]
                fold = min(0.95, fold * k)
                rest = max(0.0, 1 - fold)
                tot = call + raise_
                call, raise_ = (rest * call / tot, rest * raise_ / tot) if tot else (rest, 0.0)
            rules.append(Rule(street.lower(), "vs_bet", "any", "any", size, n,
                              {"fold": round(fold, 3), "call": round(call, 3), "raise": round(raise_, 3)},
                              basis=f"pool, HU, {street.lower()} bets of {size} size: n={n}"))
    node_map = {
        ("cbet", "FLOP"): "lead_unopened", ("barrel", "TURN"): "lead_unopened",
        ("river_bet", "RIVER"): "lead_unopened",
        ("donk", "ANY"): "nonlead_first", ("stab", "ANY"): "nonlead_after_check",
        ("vs_stab", "ANY"): "lead_vs_stab", ("vs_raise", "ANY"): "vs_raise",
        ("nolead_bet", "ANY"): "nolead_unopened", ("vs_nolead_bet", "ANY"): "vs_bet_nolead",
    }
    for node in profile.nodes:
        sit = node_map.get((node.node, node.street))
        if not sit or node.n < 30 or node.pot_type not in ("srp", "3bet"):
            continue
        street = node.street.lower() if node.street != "ANY" else "any"
        mix = {k: round(v, 3) for k, v in node.mix.items()}
        rules.append(Rule(street, sit, node.pot_type, node.position, "any", node.n, mix,
                          basis=f"pool, HU {node.pot_type} {node.position}: n={node.n}"))
    return rules


def pool_preset(profile: Profile) -> Preset:
    return Preset(
        id="nl5-pool", name="NL5 pool (measured)", builtin=True,
        description=("Every villain node locked to the pool's measured mix, dealt strongest-first "
                     "by equity and softened per street by how much air villains showed with that "
                     "action. Hero is solved as a best response."),
        basis=(f"Villain actions in heads-up NL5 pots, {profile.hands:,} hands through "
               f"{profile.built_at[:10]}; showdown composition for the blend. See /pool."),
        villain_bets=_sizes_from(profile), villain_raises=_raise_from(profile),
        policy=pool_policy(profile),
    )


def archetype_presets(profile: Profile) -> list[Preset]:
    out = []
    pm = profile.pool_means
    for a in profile.archetypes:
        c = a.centroid
        scale = {}
        for street, key in (("flop", "fold_cbet"), ("turn", "fold_turn"), ("river", "fold_river")):
            if c.get(key) and pm.get(key):
                scale[street] = c[key] / pm[key]
        call_scale = (c["vpip"] / pm["vpip"]) if c.get("vpip") and pm.get("vpip") else 1.0
        raise_scale = (c["pfr"] / pm["pfr"]) if c.get("pfr") and pm.get("pfr") else 1.0
        cbet = a.cbet[0] if a.cbet[1] >= 30 else None
        policy = pool_policy(profile, scale)
        if cbet is not None:
            for r in policy:
                if r.situation == "lead_unopened" and r.street == "flop":
                    r.mix = {"bet": round(cbet, 3), "check": round(1 - cbet, 3)}
                    r.basis += f"; c-bet rate from the archetype (n={a.cbet[1]})"
        out.append(Preset(
            id=f"vs-{slug(a.name)}", name=f"vs {a.name} regular", builtin=True,
            description=(f"{a.players} regulars, {a.share:.0%} of regular-hands: VPIP {c['vpip']:.0%}, "
                         f"PFR {c['pfr']:.0%}, folds to c-bet {c['fold_cbet']:.0%}, turn {c['fold_turn']:.0%}, "
                         f"river {c['fold_river']:.0%}. Pool policy with fold shares scaled by "
                         + ", ".join(f"{k} ×{v:.2f}" for k, v in scale.items()) + "."),
            basis=f"k-means over regulars with 100+ hands (silhouette {profile.silhouette:.2f}: a "
                  f"continuum, not distinct types). Members listed on /pool.",
            villain_bets=_sizes_from(profile), villain_raises=_raise_from(profile),
            policy=policy, width_scale={"call": round(call_scale, 2), "raise": round(raise_scale, 2)},
        ))
    return out


def transient_preset(profile: Profile) -> Preset | None:
    t = next((t for t in profile.tiers if t.tier.startswith("transient")), None)
    regs = next((t for t in profile.tiers if t.tier.startswith("regular")), None)
    if not t or not regs:
        return None
    scale = {}
    for street, key in (("flop", "fold_cbet"), ("turn", "fold_turn"), ("river", "fold_river")):
        a, b = getattr(t, key), getattr(regs, key)
        if a and b:
            scale[street] = a / b
    policy = pool_policy(profile, scale)
    if t.cbet:
        for r in policy:
            if r.situation == "lead_unopened" and r.street == "flop":
                r.mix = {"bet": round(t.cbet, 3), "check": round(1 - t.cbet, 3)}
    return Preset(
        id="vs-unknown", name="vs unknown / transient player", builtin=True,
        description=(f"Players seen under 20 hands ({t.share:.0%} of villain-hands): VPIP {t.vpip:.0%}, "
                     f"limp {t.limp:.0%}, folds to c-bet {t.fold_cbet:.0%}, river {t.fold_river:.0%}. "
                     f"Far looser than the regulars; pool policy with folds scaled by "
                     + ", ".join(f"{k} ×{v:.2f}" for k, v in scale.items()) + "."),
        basis="Tier aggregate; no per-player reads are possible at this sample.",
        villain_bets=_sizes_from(profile), villain_raises=_raise_from(profile),
        policy=policy, width_scale={"call": round(t.vpip / regs.vpip, 2), "raise": round(t.pfr / regs.pfr, 2)},
    )


def builtin(profile: Profile) -> list[Preset]:
    out = [equilibrium(), pool_preset(profile)]
    out += archetype_presets(profile)
    tp = transient_preset(profile)
    if tp:
        out.append(tp)
    return out


# --------------------------------------------------------------------------
# applying a preset to a tree
# --------------------------------------------------------------------------

def _size_of(pct: float) -> str:
    return "small" if pct < 0.45 else "mid" if pct < 0.8 else "big"


def classify(path: list[str], nodes_by_path: dict[tuple[str, ...], dict], starting_pot: int,
             hero_is_oop: bool, hero_is_lead: bool) -> dict | None:
    """What kind of node sits at the end of `path`, from the dry-run listing.

    Walks the path keeping, per street, who leads and what each player has
    committed, so the size faced is the amount to call over the pot before it.
    """
    node = nodes_by_path.get(tuple(path))
    if node is None:
        return None
    lead = 0 if (hero_is_oop == hero_is_lead) else 1        # 0 = oop
    street_lead = lead
    committed = [0, 0]
    last_wager = 0
    last_aggressor = None
    bets_before = 0
    lead_checked = False
    actor = 0
    pot_before_wager = starting_pot
    pot = starting_pot
    prev_pot = starting_pot
    street_idx = 0
    prefix: list[str] = []
    for step in path:
        prefix.append(step)
        n = nodes_by_path.get(tuple(prefix))
        if step == "*":
            # new street: whoever was last aggressor leads; nobody if checked through
            street_lead = last_aggressor if last_aggressor is not None else None
            committed = [0, 0]
            last_wager = 0
            last_aggressor = None
            bets_before = 0
            lead_checked = False
            actor = 0
            street_idx += 1
            if n:
                pot = n["pot"]
                pot_before_wager = pot
            continue
        verb = step.split()[0]
        amount = int(step.split()[1]) if len(step.split()) > 1 and step.split()[1].isdigit() else 0
        if verb in ("bet", "raise", "allin"):
            pot_before_wager = pot
            committed[actor] = amount
            last_wager = amount
            last_aggressor = actor
            bets_before += 1
        elif verb == "call":
            committed[actor] = last_wager
        elif verb == "check" and actor == street_lead:
            lead_checked = True
        if n:
            pot = n["pot"]
        actor = 1 - actor
        prev_pot = pot
    player = 0 if node["player"] == "oop" else 1
    street = node["street"]
    if street_lead is None:
        situation = "vs_bet_nolead" if bets_before else "nolead_unopened"
        size = "any"
        if bets_before:
            to_call = last_wager - committed[player]
            size = _size_of(to_call / max(1, node["pot"] - to_call))
        return {"player": player, "street": street, "situation": situation, "size": size,
                "position": "oop" if player == 0 else "ip"}
    if bets_before == 0:
        if player == street_lead:
            situation = "lead_unopened"
        elif lead_checked:
            situation = "nonlead_after_check"
        else:
            situation = "nonlead_first"
        size = "any"
    else:
        to_call = last_wager - committed[player]
        size = _size_of(to_call / max(1, node["pot"] - to_call))
        if bets_before >= 2 and last_aggressor != player and committed[player] > 0:
            situation = "vs_raise"
        elif player == street_lead:
            situation = "lead_vs_stab"
        else:
            situation = "vs_bet"
    return {"player": player, "street": street, "situation": situation, "size": size,
            "position": "oop" if player == 0 else "ip"}


def locks_for(preset: Preset, tree: list[dict], starting_pot: int, hero_is_oop: bool,
              hero_is_lead: bool, pot_type: str) -> tuple[list[dict], list[dict]]:
    """Locks for every villain node the preset has a rule for.

    Returns (locks, applied) where `applied` lists each node with the rule it
    got, for the page to show what was assumed where.
    """
    villain = 1 if hero_is_oop else 0
    by_path = {tuple(n["path"]): n for n in tree}
    locks, applied = [], []
    for node in tree:
        if (0 if node["player"] == "oop" else 1) != villain:
            continue
        info = classify(node["path"], by_path, starting_pot, hero_is_oop, hero_is_lead)
        if info is None:
            continue
        rule = preset.rule_for(info["street"], info["situation"], pot_type, info["position"], info["size"])
        if rule is None:
            applied.append({**info, "path": node["path"], "rule": None})
            continue
        actions = {}
        names = node["actions"]
        for key, f in rule.mix.items():
            if f <= 0:
                continue
            if key == "raise" and not any(a.startswith(("raise", "allin")) for a in names):
                continue
            if key == "bet" and not any(a.startswith(("bet", "allin")) for a in names):
                continue
            if key in ("fold", "call", "check") and key not in names:
                continue
            actions[key] = f
        if not actions:
            applied.append({**info, "path": node["path"], "rule": None})
            continue
        locks.append({"path": node["path"], "mode": "ranked", "actions": actions,
                      "blend": preset.blend.get(info["street"], 0.3)})
        applied.append({**info, "path": node["path"], "rule": asdict(rule), "actions": actions})
    return locks, applied
