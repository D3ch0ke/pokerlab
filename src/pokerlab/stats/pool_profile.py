"""What the NL5 pool does at every postflop node, who is in it, and how strongly
an action is tied to strength -- the measurements a pool-specific solve rests on.

Three layers, kept apart because they are known to different degrees:

* **Node tendencies** are measured from every villain action on file, heads-up
  pots only (the solver is a two-player engine), split by pot type, position
  and the size faced. Thousands of observations each.
* **Composition** comes from villains who showed down, so it over-represents
  hands that kept paying. It is read as *how strength-ordered* an action is --
  a river bet is 5% air, a flop check-call is a third air -- never as a range.
* **Archetypes** are a k-means cut over the regulars (100+ hands). The
  silhouette is weak (about 0.2): the pool is a continuum with one clear split
  (loose-passive vs the rest), and the four labels are a reading aid, not a
  discovery. The transient players -- a third of villain-hands -- cannot be
  clustered at all and are profiled as a tier instead; they are far looser
  than the regulars, which a regulars-only model would miss.

Everything here is scoped to NL5 over the whole file. Quarterly figures are
reported so a reader can see the pool drifted little (fold-to-c-bet 50% to
43% over the year, the rest flat) before trusting an all-time number.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..ranges.strength import _card, evaluate
from .core import MIN_N, NL5

CACHE = Path("data/pool_profile.json")

#: Made-hand categories, coarse: a flush draw is "air" here, which is why the
#: solver ranks by equity on the board and not by this label.
CATEGORY = ("no pair", "one pair", "two pair", "trips", "straight", "flush",
            "full house", "quads", "straight flush")

SIZE_BUCKETS = (("<30", 0.0, 0.30), ("30-44", 0.30, 0.45), ("45-59", 0.45, 0.60),
                ("60-79", 0.60, 0.80), ("80-109", 0.80, 1.10), ("110+", 1.10, 99.0))
COARSE = (("small", 0.0, 0.45), ("mid", 0.45, 0.80), ("big", 0.80, 99.0))

REG_HANDS = 100
K_ARCHETYPES = 4
FEATURES = ("vpip", "pfr", "threebet", "limp", "afq", "fold_cbet", "wtsd", "cbet")

# --------------------------------------------------------------------------
# the shared context table: one row per postflop action with its node
# --------------------------------------------------------------------------

CONTEXT_SQL = """
CREATE OR REPLACE TEMP TABLE ctx AS
WITH pre AS (
    SELECT a.hand_id,
           COUNT(*) FILTER (WHERE a.verb = 'Raises to') AS n_raises,
           arg_max(CASE WHEN a.verb = 'Raises to' THEN a.name END,
                   CASE WHEN a.verb = 'Raises to' THEN a.idx END) AS pfa
    FROM actions a WHERE a.street = 'PRE-FLOP' GROUP BY a.hand_id
),
post AS (
    SELECT a.hand_id, a.idx, a.street, a.name, a.verb, a.is_hero, a.effective, a.pot_before,
           a.all_in, h.bb, h.board, h.flop_paired, h.flop_high, h.flop_wet, h.played_at,
           p.n_raises, p.pfa,
           COUNT(DISTINCT a.name) OVER (PARTITION BY a.hand_id, a.street) AS n_street,
           COALESCE(SUM(CASE WHEN a.verb IN ('Bets','Raises to') THEN 1 ELSE 0 END) OVER ws, 0)
               AS bets_before,
           last_value(CASE WHEN a.verb IN ('Bets','Raises to') THEN a.effective END IGNORE NULLS)
               OVER ws AS last_wager,
           last_value(CASE WHEN a.verb IN ('Bets','Raises to') THEN a.name END IGNORE NULLS)
               OVER ws AS last_aggressor,
           COALESCE(SUM(a.contributed) OVER wn, 0) AS own_committed,
           first_value(a.name) OVER (PARTITION BY a.hand_id, a.street ORDER BY a.idx
               ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS first_actor,
           row_number() OVER (PARTITION BY a.hand_id, a.street ORDER BY a.idx) AS street_ord
    FROM actions a JOIN hands h USING (hand_id) JOIN pre p USING (hand_id)
    WHERE h.game_name = ? AND a.street <> 'PRE-FLOP'
    WINDOW ws AS (PARTITION BY a.hand_id, a.street ORDER BY a.idx
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
           wn AS (PARTITION BY a.hand_id, a.street, a.name ORDER BY a.idx
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
agg AS (
    SELECT hand_id, street,
           arg_max(CASE WHEN verb IN ('Bets','Raises to') THEN name END,
                   CASE WHEN verb IN ('Bets','Raises to') THEN idx END) AS street_aggressor
    FROM actions WHERE street <> 'PRE-FLOP' GROUP BY hand_id, street
)
SELECT p.*,
       CASE p.street WHEN 'FLOP' THEN p.pfa WHEN 'TURN' THEN f.street_aggressor
                     ELSE t.street_aggressor END AS lead,
       CASE WHEN p.n_raises = 0 THEN 'limped' WHEN p.n_raises = 1 THEN 'srp'
            WHEN p.n_raises = 2 THEN '3bet' ELSE '4bet+' END AS pot_type,
       CASE WHEN p.bets_before = 0 THEN NULL
            ELSE (p.last_wager - p.own_committed)::DOUBLE
                 / NULLIF(p.pot_before - (p.last_wager - p.own_committed), 0) END AS faced_pct,
       CASE WHEN p.verb = 'Bets' THEN p.effective::DOUBLE / NULLIF(p.pot_before, 0) END AS bet_pct,
       CASE WHEN p.verb = 'Raises to' AND p.last_wager > 0
            THEN p.effective::DOUBLE / p.last_wager END AS raise_x,
       (p.name = p.first_actor) AS is_oop
FROM post p
LEFT JOIN agg f ON f.hand_id = p.hand_id AND f.street = 'FLOP'
LEFT JOIN agg t ON t.hand_id = p.hand_id AND t.street = 'TURN'
"""

#: Villain, heads-up on the street, pot type the solver models.
_HU = "NOT is_hero AND n_street = 2 AND pot_type IN ('srp', '3bet')"

_NODES = {
    # node: (where, numerator verb for the "rate" column)
    "cbet": (f"{_HU} AND street = 'FLOP' AND bets_before = 0 AND name = lead", "Bets"),
    "vs_cbet": (f"{_HU} AND street = 'FLOP' AND bets_before = 1 AND last_aggressor = lead "
                f"AND name <> lead", None),
    "barrel": (f"{_HU} AND street = 'TURN' AND bets_before = 0 AND name = lead", "Bets"),
    "vs_barrel": (f"{_HU} AND street = 'TURN' AND bets_before = 1 AND last_aggressor = lead "
                  f"AND name <> lead", None),
    "river_bet": (f"{_HU} AND street = 'RIVER' AND bets_before = 0 AND name = lead", "Bets"),
    "vs_river_bet": (f"{_HU} AND street = 'RIVER' AND bets_before = 1 AND last_aggressor = lead "
                     f"AND name <> lead", None),
    "donk": (f"{_HU} AND bets_before = 0 AND is_oop AND lead IS NOT NULL AND name <> lead "
             f"AND street_ord = 1", "Bets"),
    "stab": (f"{_HU} AND bets_before = 0 AND lead IS NOT NULL AND name <> lead "
             f"AND street_ord = 2", "Bets"),
    "vs_stab": (f"{_HU} AND bets_before = 1 AND name = lead AND last_aggressor <> lead", None),
    # the villain bet and was raised: bets_before counts their own bet
    "vs_raise": (f"{_HU} AND bets_before = 2 AND last_aggressor <> name", None),
    # a street nobody leads: the previous one was checked through
    "nolead_bet": (f"{_HU} AND bets_before = 0 AND lead IS NULL AND street <> 'FLOP'", "Bets"),
    "vs_nolead_bet": (f"{_HU} AND bets_before = 1 AND lead IS NULL AND street <> 'FLOP'", None),
}

_STAT_SQL = """
SELECT {group} AS grp, COUNT(*) AS n,
       SUM((verb = 'Bets')::INT) AS bet, SUM((verb = 'Folds')::INT) AS fold,
       SUM((verb = 'Calls')::INT) AS calls, SUM((verb = 'Raises to')::INT) AS raise,
       SUM((verb = 'Checks')::INT) AS checks,
       quantile_cont(bet_pct, 0.25) FILTER (WHERE verb = 'Bets') AS q25,
       median(bet_pct) FILTER (WHERE verb = 'Bets') AS med,
       quantile_cont(bet_pct, 0.75) FILTER (WHERE verb = 'Bets') AS q75,
       median(raise_x) FILTER (WHERE verb = 'Raises to') AS raise_med
FROM ctx WHERE {where} GROUP BY 1 ORDER BY 1
"""


@dataclass(slots=True)
class Node:
    """The pool's mix at one node, off `n` villain decisions."""

    node: str
    street: str
    pot_type: str
    position: str            # "ip" | "oop" | "any"
    n: int
    mix: dict[str, float]     # bet/check or fold/call/raise, shares of n
    size: tuple[float, float, float] | None = None   # bet size quartiles, % of pot
    raise_x: float | None = None

    @property
    def ok(self) -> bool:
        return self.n >= MIN_N


@dataclass(slots=True)
class SizeResponse:
    street: str
    bucket: str
    n: int
    fold: float
    call: float
    raise_: float
    mdf_fold: float
    """The most a player can fold before a bet with no equity profits: 1 - MDF
    at the bucket's midpoint. `fold - mdf_fold` is the over-fold."""


@dataclass(slots=True)
class Composition:
    street: str
    action: str
    n: int
    air: float
    pair: float
    strong: float           # two pair or better


@dataclass(slots=True)
class Tier:
    tier: str
    players: int
    villain_hands: int
    share: float
    vpip: float
    pfr: float
    limp: float
    cbet: float | None
    fold_cbet: float | None
    fold_turn: float | None
    fold_river: float | None
    afq: float
    hero_bb100: float
    hero_hands: int


@dataclass(slots=True)
class Archetype:
    id: int
    name: str
    players: int
    hands: int
    share: float
    centroid: dict[str, float]
    flop_fold_by_size: dict[str, tuple[float, int]]
    river_fold_by_size: dict[str, tuple[float, int]]
    turn_fold: tuple[float, int]
    cbet: tuple[float, int]
    cbet_size_med: float | None
    members: list[str]


@dataclass(slots=True)
class Profile:
    built_at: str
    hands: int
    villains: int
    nodes: list[Node]
    cbet_sizes: dict[str, float]
    size_response: list[SizeResponse]
    composition: list[Composition]
    tiers: list[Tier]
    archetypes: list[Archetype]
    players: dict[str, dict]
    quarters: list[dict]
    silhouette: float | None
    pool_means: dict[str, float]

    def node(self, node: str, pot_type: str = "srp", position: str = "any") -> Node | None:
        return next((n for n in self.nodes if n.node == node and n.pot_type == pot_type
                     and n.position == position), None)

    def archetype_of(self, name: str) -> Archetype | None:
        return next((a for a in self.archetypes if name in a.members), None)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Profile":
        d = json.loads(text)
        return cls(
            built_at=d["built_at"], hands=d["hands"], villains=d["villains"],
            nodes=[Node(**{**n, "size": tuple(n["size"]) if n["size"] else None})
                   for n in d["nodes"]],
            cbet_sizes=d["cbet_sizes"],
            size_response=[SizeResponse(**s) for s in d["size_response"]],
            composition=[Composition(**c) for c in d["composition"]],
            tiers=[Tier(**t) for t in d["tiers"]],
            archetypes=[Archetype(**{**a, "turn_fold": tuple(a["turn_fold"]),
                                     "cbet": tuple(a["cbet"]),
                                     "flop_fold_by_size": {k: tuple(v) for k, v in a["flop_fold_by_size"].items()},
                                     "river_fold_by_size": {k: tuple(v) for k, v in a["river_fold_by_size"].items()}})
                        for a in d["archetypes"]],
            players=d["players"], quarters=d["quarters"], silhouette=d["silhouette"],
            pool_means=d["pool_means"],
        )


# --------------------------------------------------------------------------

def _bucket(x: float | None, buckets=SIZE_BUCKETS) -> str | None:
    if x is None or x != x:
        return None
    for name, lo, hi in buckets:
        if lo <= x < hi:
            return name
    return None


def _mdf_fold(bucket: str, buckets=SIZE_BUCKETS) -> float:
    for name, lo, hi in buckets:
        if name == bucket:
            mid = (lo + min(hi, 1.5)) / 2
            return mid / (1 + mid)
    return 0.0


def _nodes(con) -> list[Node]:
    out: list[Node] = []
    for node, (where, _) in _NODES.items():
        for pos_col in ("'any'", "CASE WHEN is_oop THEN 'oop' ELSE 'ip' END"):
            group = f"pot_type || '|' || {pos_col}"
            for grp, n, bet, fold, calls, raise_, checks, q25, med, q75, raise_med in \
                    con.execute(_STAT_SQL.format(group=group, where=where)).fetchall():
                pot_type, position = grp.split("|")
                if node.startswith("vs_"):
                    mix = {"fold": fold / n, "call": calls / n, "raise": raise_ / n}
                else:
                    mix = {"bet": bet / n, "check": checks / n}
                out.append(Node(node, _street_of(node), pot_type, position, n, mix,
                                (q25, med, q75) if med is not None else None, raise_med))
    # raise sizes and check-raise share per street, any node
    return out


def _street_of(node: str) -> str:
    if node in ("cbet", "vs_cbet"):
        return "FLOP"
    if node in ("barrel", "vs_barrel"):
        return "TURN"
    if node in ("river_bet", "vs_river_bet"):
        return "RIVER"
    return "ANY"


def _cbet_sizes(con) -> dict[str, float]:
    rows = con.execute(f"""
        SELECT bet_pct FROM ctx WHERE {_NODES['cbet'][0]} AND verb = 'Bets' AND pot_type = 'srp'
    """).fetchall()
    c = Counter(_bucket(r[0]) for r in rows)
    total = sum(v for k, v in c.items() if k)
    return {name: c[name] / total for name, _, _ in SIZE_BUCKETS} if total else {}


def _size_response(con) -> list[SizeResponse]:
    out = []
    # Facing the LEAD's bet on every street: the river fold rate to a third
    # barrel (34%) is nothing like the fold rate to a probe after checks (62%),
    # and a lock that mixed them would fold a barrel-caller far too often.
    for street in ("FLOP", "TURN", "RIVER"):
        extra = " AND last_aggressor = lead AND name <> lead"
        rows = con.execute(f"""
            SELECT faced_pct, verb FROM ctx
            WHERE {_HU} AND street = '{street}' AND bets_before = 1 {extra}
        """).fetchall()
        by: dict[str, Counter] = defaultdict(Counter)
        for pct, verb in rows:
            b = _bucket(pct)
            if b:
                by[b][verb] += 1
        for name, _, _ in SIZE_BUCKETS:
            c = by.get(name)
            if not c:
                continue
            n = sum(c.values())
            out.append(SizeResponse(street, name, n, c["Folds"] / n, c["Calls"] / n,
                                    c["Raises to"] / n, _mdf_fold(name)))
    return out


def _composition(con) -> list[Composition]:
    rows = con.execute("""
        SELECT c.street, c.verb, c.faced_pct, c.bet_pct, c.board, s.shown
        FROM ctx c JOIN seats s ON s.hand_id = c.hand_id AND s.name = c.name
        WHERE NOT c.is_hero AND c.n_street = 2 AND s.shown IS NOT NULL
          AND c.pot_type IN ('srp', '3bet', 'limped')
          AND c.verb IN ('Bets', 'Calls', 'Raises to', 'Checks')
    """).fetchall()
    n_board = {"FLOP": 3, "TURN": 4, "RIVER": 5}
    tab: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for street, verb, faced, betp, board, shown in rows:
        b = board.split()[:n_board[street]]
        cards = tuple(_card(c) for c in shown.split()) + tuple(_card(c) for c in b)
        if len(cards) < 5:
            continue
        cat = CATEGORY[evaluate(cards)[0]]
        coarse = "air" if cat == "no pair" else "pair" if cat == "one pair" else "strong"
        label = {"Bets": "bet", "Checks": "check", "Calls": "call vs bet",
                 "Raises to": "raise vs bet"}[verb]
        tab[(street, label)][coarse] += 1
        size = betp if verb == "Bets" else faced
        sb = _bucket(size, COARSE)
        if sb and verb in ("Bets", "Calls"):
            tab[(street, f"{label} {sb}")][coarse] += 1
    out = []
    for (street, label), c in sorted(tab.items()):
        n = sum(c.values())
        if n >= MIN_N:
            out.append(Composition(street, label, n, c["air"] / n, c["pair"] / n, c["strong"] / n))
    return out


_FEATURES_SQL = """
CREATE OR REPLACE TEMP TABLE feat AS
WITH pf AS (
    SELECT a.hand_id, a.name, a.verb, a.idx, a.position,
           COALESCE(SUM(CASE WHEN a.verb='Raises to' THEN 1 ELSE 0 END) OVER w, 0) AS rb,
           lag(a.verb) OVER (PARTITION BY a.hand_id, a.name ORDER BY a.idx) AS prior_verb
    FROM actions a JOIN hands h USING (hand_id)
    WHERE a.street='PRE-FLOP' AND NOT a.dead AND a.verb NOT IN ('Posts SB','Posts BB','Posts Ante')
      AND h.game_name = ? AND NOT a.is_hero
    WINDOW w AS (PARTITION BY a.hand_id ORDER BY a.idx ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
pre AS (
    SELECT name,
      COUNT(DISTINCT hand_id) AS pf_hands,
      COUNT(DISTINCT CASE WHEN verb IN ('Calls','Raises to') THEN hand_id END) AS vpip_n,
      COUNT(DISTINCT CASE WHEN verb='Raises to' THEN hand_id END) AS pfr_n,
      COUNT(*) FILTER (WHERE rb=1 AND prior_verb IS NULL) AS threebet_opp,
      COUNT(*) FILTER (WHERE rb=1 AND prior_verb IS NULL AND verb='Raises to') AS threebet_n,
      COUNT(*) FILTER (WHERE rb=0 AND position <> 'BB') AS rfi_opp,
      COUNT(*) FILTER (WHERE rb=0 AND position <> 'BB' AND verb='Calls') AS limp_n,
      COUNT(*) FILTER (WHERE rb=2 AND prior_verb='Raises to') AS f3b_opp,
      COUNT(*) FILTER (WHERE rb=2 AND prior_verb='Raises to' AND verb='Folds') AS f3b_n
    FROM pf GROUP BY name
),
post AS (
    SELECT name,
      COUNT(*) FILTER (WHERE verb IN ('Bets','Raises to')) AS aggr,
      COUNT(*) AS acts,
      COUNT(*) FILTER (WHERE street='FLOP' AND bets_before=0 AND name=lead) AS cbet_opp,
      COUNT(*) FILTER (WHERE street='FLOP' AND bets_before=0 AND name=lead AND verb='Bets') AS cbet_n,
      COUNT(*) FILTER (WHERE street='FLOP' AND bets_before=1 AND last_aggressor=lead AND name<>lead) AS fcb_opp,
      COUNT(*) FILTER (WHERE street='FLOP' AND bets_before=1 AND last_aggressor=lead AND name<>lead AND verb='Folds') AS fcb_n,
      COUNT(*) FILTER (WHERE street='FLOP' AND bets_before=1 AND last_aggressor=lead AND name<>lead AND verb='Raises to') AS rcb_n,
      COUNT(*) FILTER (WHERE street='TURN' AND bets_before=1) AS ftb_opp,
      COUNT(*) FILTER (WHERE street='TURN' AND bets_before=1 AND verb='Folds') AS ftb_n,
      COUNT(*) FILTER (WHERE street='RIVER' AND bets_before=1) AS frb_opp,
      COUNT(*) FILTER (WHERE street='RIVER' AND bets_before=1 AND verb='Folds') AS frb_n,
      COUNT(*) FILTER (WHERE street='RIVER' AND bets_before=0) AS rb_opp,
      COUNT(*) FILTER (WHERE street='RIVER' AND bets_before=0 AND verb='Bets') AS rb_n,
      COUNT(*) FILTER (WHERE bets_before=0 AND lead IS NOT NULL AND name<>lead AND is_oop AND street_ord=1) AS donk_opp,
      COUNT(*) FILTER (WHERE bets_before=0 AND lead IS NOT NULL AND name<>lead AND is_oop AND street_ord=1 AND verb='Bets') AS donk_n,
      median(bet_pct) FILTER (WHERE verb='Bets') AS bet_size_med
    FROM ctx WHERE NOT is_hero GROUP BY name
),
sd AS (
    SELECT c.name,
      COUNT(DISTINCT c.hand_id) AS flop_hands,
      COUNT(DISTINCT CASE WHEN NOT fo.folded AND (rv.has_river OR c.all_in) THEN c.hand_id END) AS sd_hands,
      COUNT(DISTINCT CASE WHEN NOT fo.folded AND (rv.has_river OR c.all_in) AND s.net > 0 THEN c.hand_id END) AS sd_won
    FROM ctx c
    JOIN (SELECT hand_id, name, bool_or(verb='Folds') AS folded FROM actions GROUP BY 1,2) fo USING (hand_id, name)
    JOIN (SELECT hand_id, bool_or(street='RIVER') AS has_river FROM actions GROUP BY 1) rv USING (hand_id)
    JOIN seats s ON s.hand_id=c.hand_id AND s.name=c.name
    WHERE NOT c.is_hero GROUP BY c.name
),
res AS (
    SELECT s.name, COUNT(*) AS hands, SUM(s.net)::DOUBLE / h.bb / COUNT(*) * 100 AS bb100
    FROM seats s JOIN hands h USING (hand_id) WHERE NOT s.is_hero AND h.game_name = ? GROUP BY s.name, h.bb
)
SELECT r.name, r.hands, r.bb100,
  vpip_n::DOUBLE/pf_hands AS vpip, pfr_n::DOUBLE/pf_hands AS pfr,
  threebet_opp, threebet_n::DOUBLE/NULLIF(threebet_opp,0) AS threebet,
  rfi_opp, limp_n::DOUBLE/NULLIF(rfi_opp,0) AS limp,
  f3b_opp, f3b_n::DOUBLE/NULLIF(f3b_opp,0) AS fold3b,
  aggr::DOUBLE/NULLIF(acts,0) AS afq,
  cbet_opp, cbet_n::DOUBLE/NULLIF(cbet_opp,0) AS cbet,
  fcb_opp, fcb_n::DOUBLE/NULLIF(fcb_opp,0) AS fold_cbet, rcb_n::DOUBLE/NULLIF(fcb_opp,0) AS raise_cbet,
  ftb_opp, ftb_n::DOUBLE/NULLIF(ftb_opp,0) AS fold_turn,
  frb_opp, frb_n::DOUBLE/NULLIF(frb_opp,0) AS fold_river,
  rb_opp, rb_n::DOUBLE/NULLIF(rb_opp,0) AS river_bet,
  donk_opp, donk_n::DOUBLE/NULLIF(donk_opp,0) AS donk, bet_size_med,
  flop_hands, sd_hands::DOUBLE/NULLIF(flop_hands,0) AS wtsd, sd_won::DOUBLE/NULLIF(sd_hands,0) AS wsd
FROM res r LEFT JOIN pre USING (name) LEFT JOIN post USING (name) LEFT JOIN sd USING (name)
"""


def _players(con) -> dict[str, dict]:
    con.execute(_FEATURES_SQL, [NL5, NL5])
    cur = con.execute("SELECT * FROM feat ORDER BY hands DESC")
    cols = [d[0] for d in cur.description]
    out = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        out[d["name"]] = {k: (None if v is None or (isinstance(v, float) and v != v) else v)
                          for k, v in d.items() if k != "name"}
    return out


def _weighted(players: dict[str, dict], names, key: str) -> float | None:
    num = den = 0.0
    for n in names:
        v = players[n].get(key)
        if v is None:
            continue
        num += v * players[n]["hands"]
        den += players[n]["hands"]
    return num / den if den else None


def _tiers(con, players: dict[str, dict]) -> list[Tier]:
    con.execute("""
        CREATE OR REPLACE TEMP TABLE tier AS
        SELECT name, hands, CASE WHEN hands >= 100 THEN 'regular (100+)' WHEN hands >= 50 THEN 'semi (50-99)'
                                 WHEN hands >= 20 THEN 'short (20-49)' ELSE 'transient (<20)' END AS tier
        FROM feat
    """)
    total = con.execute("SELECT SUM(hands) FROM tier").fetchone()[0]
    rows = con.execute("""
        SELECT t.tier, COUNT(*) AS players, SUM(t.hands) AS vh,
               SUM(f.vpip * f.hands) / SUM(f.hands), SUM(f.pfr * f.hands) / SUM(f.hands),
               SUM(COALESCE(f.limp, 0) * f.rfi_opp) / NULLIF(SUM(f.rfi_opp), 0),
               SUM(COALESCE(f.cbet, 0) * f.cbet_opp) / NULLIF(SUM(f.cbet_opp), 0),
               SUM(COALESCE(f.fold_cbet, 0) * f.fcb_opp) / NULLIF(SUM(f.fcb_opp), 0),
               SUM(COALESCE(f.fold_turn, 0) * f.ftb_opp) / NULLIF(SUM(f.ftb_opp), 0),
               SUM(COALESCE(f.fold_river, 0) * f.frb_opp) / NULLIF(SUM(f.frb_opp), 0),
               SUM(f.afq * f.hands) / SUM(f.hands)
        FROM tier t JOIN feat f USING (name) GROUP BY 1 ORDER BY 3 DESC
    """).fetchall()
    hero = {r[0]: (r[1], r[2]) for r in con.execute("""
        SELECT t.tier, COUNT(DISTINCT h.hand_id),
               SUM(h.hero_net)::DOUBLE / h.bb / COUNT(DISTINCT h.hand_id) * 100
        FROM hands h JOIN seats s USING (hand_id) JOIN tier t ON t.name = s.name
        WHERE h.game_name = ? AND NOT s.is_hero GROUP BY 1, h.bb
    """, [NL5]).fetchall()}
    out = []
    for tier, players_n, vh, vpip, pfr, limp, cbet, fcb, ft, fr, afq in rows:
        hh, hb = hero.get(tier, (0, 0.0))
        out.append(Tier(tier, players_n, int(vh), vh / total, vpip, pfr, limp or 0.0,
                        cbet, fcb, ft, fr, afq, hb, hh))
    return out


def _archetypes(con, players: dict[str, dict]) -> tuple[list[Archetype], float | None]:
    regs = [n for n, p in players.items() if p["hands"] >= REG_HANDS]
    if len(regs) < 3 * K_ARCHETYPES:
        return [], None
    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        return [], None
    means = {f: _weighted(players, regs, f) for f in FEATURES}
    X = np.array([[players[n][f] if players[n][f] is not None else means[f] for f in FEATURES]
                  for n in regs])
    Z = StandardScaler().fit_transform(X)
    km = KMeans(K_ARCHETYPES, n_init=50, random_state=0).fit(Z)
    sil = float(silhouette_score(Z, km.labels_))
    groups: dict[int, list[str]] = defaultdict(list)
    for n, lab in zip(regs, km.labels_):
        groups[int(lab)].append(n)
    total = sum(players[n]["hands"] for n in regs)

    con.execute("CREATE OR REPLACE TEMP TABLE arch (name VARCHAR, c INTEGER)")
    con.executemany("INSERT INTO arch VALUES (?, ?)", [(n, int(l)) for n, l in zip(regs, km.labels_)])

    def fold_by_size(street: str, extra: str) -> dict[int, dict[str, tuple[float, int]]]:
        rows = con.execute(f"""
            SELECT a.c, CASE WHEN faced_pct < 0.45 THEN 'small' WHEN faced_pct < 0.8 THEN 'mid' ELSE 'big' END,
                   COUNT(*), AVG((verb = 'Folds')::INT)
            FROM ctx JOIN arch a USING (name)
            WHERE {_HU} AND street = '{street}' AND bets_before = 1 {extra} AND faced_pct IS NOT NULL
            GROUP BY 1, 2""").fetchall()
        out: dict[int, dict[str, tuple[float, int]]] = defaultdict(dict)
        for c, b, n, f in rows:
            out[c][b] = (f, n)
        return out

    flop = fold_by_size("FLOP", "AND last_aggressor = lead AND name <> lead")
    river = fold_by_size("RIVER", "")
    turn = {c: (f, n) for c, n, f in con.execute(f"""
        SELECT a.c, COUNT(*), AVG((verb = 'Folds')::INT) FROM ctx JOIN arch a USING (name)
        WHERE {_HU} AND street = 'TURN' AND bets_before = 1 AND last_aggressor = lead AND name <> lead
        GROUP BY 1""").fetchall()}
    cbet = {c: (r, n, m) for c, n, r, m in con.execute(f"""
        SELECT a.c, COUNT(*), AVG((verb = 'Bets')::INT), median(bet_pct) FILTER (WHERE verb = 'Bets')
        FROM ctx JOIN arch a USING (name)
        WHERE {_HU} AND street = 'FLOP' AND bets_before = 0 AND name = lead GROUP BY 1""").fetchall()}

    out = []
    keys = ("vpip", "pfr", "threebet", "limp", "fold3b", "afq", "cbet", "fold_cbet", "raise_cbet",
            "fold_turn", "fold_river", "river_bet", "donk", "wtsd", "wsd", "bb100")
    for c, members in sorted(groups.items()):
        cen = {k: _weighted(players, members, k) for k in keys}
        hands = sum(players[n]["hands"] for n in members)
        out.append(Archetype(c, _name(cen), len(members), hands, hands / total, cen,
                             flop.get(c, {}), river.get(c, {}), turn.get(c, (0.0, 0)),
                             (cbet.get(c, (0.0, 0, None))[0], cbet.get(c, (0.0, 0, None))[1]),
                             cbet.get(c, (0.0, 0, None))[2], sorted(members)))
    out.sort(key=lambda a: -a.share)
    return out, sil


def _name(c: dict[str, float | None]) -> str:
    """A label from the centroid. Rules, not a lookup, so a re-cluster relabels itself."""
    vpip, pfr, afq, fcb = c["vpip"] or 0, c["pfr"] or 0, c["afq"] or 0, c["fold_cbet"] or 1
    if vpip >= 0.6:
        return "maniac"
    if vpip >= 0.33 and pfr / max(vpip, 1e-9) < 0.4:
        return "loose-passive"
    if fcb < 0.45:
        return "sticky"
    if afq >= 0.23:
        return "tight-aggressive"
    return "tight-passive"


def _quarters(con) -> list[dict]:
    rows = con.execute(f"""
        SELECT date_trunc('quarter', played_at)::DATE AS q,
               COUNT(*) FILTER (WHERE street='FLOP' AND bets_before=1 AND last_aggressor=lead AND name<>lead),
               AVG((verb='Folds')::INT) FILTER (WHERE street='FLOP' AND bets_before=1 AND last_aggressor=lead AND name<>lead),
               AVG((verb='Bets')::INT) FILTER (WHERE street='FLOP' AND bets_before=0 AND name=lead),
               AVG((verb='Folds')::INT) FILTER (WHERE street='TURN' AND bets_before=1),
               AVG((verb='Folds')::INT) FILTER (WHERE street='RIVER' AND bets_before=1),
               AVG((verb='Raises to')::INT) FILTER (WHERE street='FLOP' AND bets_before=1)
        FROM ctx WHERE {_HU} GROUP BY 1 ORDER BY 1""").fetchall()
    return [{"quarter": str(q), "n": n, "fold_cbet": a, "cbet": b, "fold_turn": t,
             "fold_river": r, "raise_flop": x} for q, n, a, b, t, r, x in rows]


def build(con) -> Profile:
    """Measure everything. A few seconds over the whole file."""
    con.execute(CONTEXT_SQL, [NL5])
    hands, villains = con.execute(
        "SELECT COUNT(DISTINCT hand_id), COUNT(DISTINCT name) FROM ctx WHERE NOT is_hero").fetchone()
    players = _players(con)
    archetypes, sil = _archetypes(con, players)
    regs = [n for n, p in players.items() if p["hands"] >= REG_HANDS]
    means = {k: _weighted(players, regs, k) for k in
             ("vpip", "pfr", "threebet", "limp", "fold3b", "afq", "cbet", "fold_cbet",
              "raise_cbet", "fold_turn", "fold_river", "river_bet", "donk", "wtsd", "wsd")}
    return Profile(
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        hands=hands, villains=villains,
        nodes=_nodes(con), cbet_sizes=_cbet_sizes(con), size_response=_size_response(con),
        composition=_composition(con), tiers=_tiers(con, players),
        archetypes=archetypes, players={n: p for n, p in players.items() if p["hands"] >= 50},
        quarters=_quarters(con), silhouette=sil, pool_means=means,
    )


def load(con, refresh: bool = False) -> Profile:
    """The cached profile if it was built on this many hands, else a fresh one."""
    n = con.execute("SELECT COUNT(*) FROM hands WHERE game_name = ?", [NL5]).fetchone()[0]
    if CACHE.exists() and not refresh:
        try:
            raw = json.loads(CACHE.read_text())
            if raw.get("_hands_on_file") == n:
                return Profile.from_json(CACHE.read_text())
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    prof = build(con)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    d = json.loads(prof.to_json())
    d["_hands_on_file"] = n
    CACHE.write_text(json.dumps(d))
    return prof
