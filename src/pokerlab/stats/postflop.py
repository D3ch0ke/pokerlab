"""Postflop statistics, split by board texture.

Until now the tool measured postflop play with three outcome numbers
(WWSF/WTSD/W$SD) that say *that* something is wrong and never *what*. These
are the process stats: who bet, who folded, on what kind of board.

Pooling across textures hides most of the signal — c-betting 70% is fine on
A-K-4 rainbow and reckless on 9-8-7 two-tone — so every stat here can be
grouped by texture.
"""

from __future__ import annotations

from dataclasses import dataclass

from .core import MIN_N, NL5, Stat

# Per hand and street: what hero could have done, and what hero did.
# `bets_before` is scoped to the street, so 0 means the action is unopened.
_CTE = """
WITH pfa AS (
    SELECT hand_id, name FROM (
        SELECT hand_id, name,
               row_number() OVER (PARTITION BY hand_id ORDER BY idx DESC) AS rn
        FROM actions WHERE street = 'PRE-FLOP' AND verb = 'Raises to') WHERE rn = 1
),
ctx AS (
    SELECT a.hand_id, a.street, a.idx, a.is_hero, a.verb, a.effective, a.pot_before,
           h.flop_paired, h.flop_suits, h.flop_high, h.flop_connect, h.flop_wet,
           (p.name = a.name) AS is_aggressor,
           (p.name IS NOT NULL) AS has_pfa,
           COALESCE(SUM(CASE WHEN a.verb IN ('Bets', 'Raises to') THEN 1 ELSE 0 END) OVER w, 0)
               AS bets_before,
           -- Has the preflop aggressor already acted this street? Betting first
           -- *before* they act is a donk; betting after they check is a stab.
           -- Conflating the two produces a nonsense donk rate.
           COALESCE(MAX(CASE WHEN (p.name = a.name) THEN 1 ELSE 0 END) OVER w, 0)
               AS aggressor_acted
    FROM actions a JOIN hands h USING (hand_id) LEFT JOIN pfa p USING (hand_id)
    WHERE h.game_name = ? AND h.played_at >= ? AND h.played_at < ? AND a.street <> 'PRE-FLOP'
    WINDOW w AS (PARTITION BY a.hand_id, a.street ORDER BY a.idx
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
hs AS (
    SELECT hand_id, street,
           max(CASE WHEN has_pfa THEN 1 ELSE 0 END) AS has_pfa,
           any_value(flop_paired) AS paired, any_value(flop_suits) AS suits,
           any_value(flop_high) AS high, any_value(flop_connect) AS connect,
           any_value(flop_wet) AS wet,
           -- was hero the preflop aggressor on this street?
           max(CASE WHEN is_hero AND is_aggressor THEN 1 ELSE 0 END) AS hero_pfa,
           -- unopened: hero got to act with no bet in front
           max(CASE WHEN is_hero AND bets_before = 0 THEN 1 ELSE 0 END) AS could_bet,
           max(CASE WHEN is_hero AND bets_before = 0 AND verb = 'Bets' THEN 1 ELSE 0 END) AS did_bet,
           -- facing a bet
           max(CASE WHEN is_hero AND bets_before >= 1 THEN 1 ELSE 0 END) AS faced_bet,
           max(CASE WHEN is_hero AND bets_before >= 1 AND verb = 'Folds' THEN 1 ELSE 0 END) AS folded,
           max(CASE WHEN is_hero AND bets_before >= 1 AND verb = 'Calls' THEN 1 ELSE 0 END) AS called,
           max(CASE WHEN is_hero AND bets_before >= 1 AND verb = 'Raises to' THEN 1 ELSE 0 END) AS raised,
           -- did the aggressor bet, so we can score hero's response to a c-bet
           max(CASE WHEN NOT is_hero AND is_aggressor AND bets_before = 0 AND verb = 'Bets'
                    THEN 1 ELSE 0 END) AS villain_cbet,
           -- donk: hero bets first, aggressor has not acted yet
           max(CASE WHEN is_hero AND bets_before = 0 AND verb = 'Bets'
                     AND aggressor_acted = 0 THEN 1 ELSE 0 END) AS donked,
           max(CASE WHEN is_hero AND bets_before = 0 AND aggressor_acted = 0
                    THEN 1 ELSE 0 END) AS donk_chance,
           -- stab: aggressor checked, hero bets
           max(CASE WHEN is_hero AND bets_before = 0 AND verb = 'Bets'
                     AND aggressor_acted = 1 THEN 1 ELSE 0 END) AS stabbed,
           max(CASE WHEN is_hero AND bets_before = 0 AND aggressor_acted = 1
                    THEN 1 ELSE 0 END) AS stab_chance,
           sum(CASE WHEN is_hero AND verb IN ('Bets', 'Raises to') THEN 1 ELSE 0 END) AS aggro,
           sum(CASE WHEN is_hero AND verb = 'Calls' THEN 1 ELSE 0 END) AS passive
    FROM ctx GROUP BY hand_id, street
)
"""

STREETS = ("FLOP", "TURN", "RIVER")


@dataclass(slots=True)
class StreetStats:
    street: str
    hands: int
    cbet: Stat
    fold_to_cbet: Stat
    raise_vs_cbet: Stat
    donk: Stat
    stab: Stat
    aggression: float | None

    def row(self) -> tuple:
        return (self.street, self.hands, str(self.cbet), str(self.fold_to_cbet),
                str(self.raise_vs_cbet), str(self.donk), str(self.stab),
                f"{self.aggression:.2f}" if self.aggression is not None else "--")


def _stats(rows) -> list[StreetStats]:
    out = []
    for (street, n, cb, cb_opp, f2c, faced_cb, rz, faced, donk, donk_opp,
         stab, stab_opp, aggro, passive) in rows:
        out.append(StreetStats(
            street=street, hands=n,
            cbet=Stat(cb or 0, cb_opp or 0),
            fold_to_cbet=Stat(f2c or 0, faced_cb or 0),
            raise_vs_cbet=Stat(rz or 0, faced or 0),
            donk=Stat(donk or 0, donk_opp or 0),
            stab=Stat(stab or 0, stab_opp or 0),
            aggression=round((aggro or 0) / passive, 2) if passive else None,
        ))
    return out


_SELECT = """
SELECT street, count(*) AS n,
       sum(CASE WHEN hero_pfa = 1 THEN did_bet END) AS cbet,
       sum(CASE WHEN hero_pfa = 1 THEN could_bet END) AS cbet_opp,
       sum(CASE WHEN villain_cbet = 1 THEN folded END) AS fold_to_cbet,
       sum(CASE WHEN villain_cbet = 1 THEN faced_bet END) AS faced_cbet,
       sum(CASE WHEN villain_cbet = 1 THEN raised END) AS raised,
       sum(CASE WHEN villain_cbet = 1 THEN faced_bet END) AS faced,
       sum(CASE WHEN hero_pfa = 0 AND has_pfa = 1 THEN donked END) AS donk,
       sum(CASE WHEN hero_pfa = 0 AND has_pfa = 1 THEN donk_chance END) AS donk_opp,
       sum(CASE WHEN hero_pfa = 0 AND has_pfa = 1 THEN stabbed END) AS stab,
       sum(CASE WHEN hero_pfa = 0 AND has_pfa = 1 THEN stab_chance END) AS stab_opp,
       sum(aggro) AS aggro, sum(passive) AS passive
FROM hs
"""


def by_street(con, since, until, game: str = NL5) -> list[StreetStats]:
    rows = con.execute(
        _CTE + _SELECT + " GROUP BY street ORDER BY array_position(['FLOP','TURN','RIVER'], street)",
        [game, since, until]).fetchall()
    return _stats(rows)


def by_texture(con, since, until, dimension: str = "wet", street: str = "FLOP",
               game: str = NL5) -> list[tuple]:
    """Group flop play by one texture dimension.

    Only the flop is offered: turn and river textures depend on the runout,
    and the flop columns would silently mislabel them.
    """
    if dimension not in {"wet", "paired", "suits", "high", "connect"}:
        raise ValueError(f"unknown texture dimension {dimension!r}")
    rows = con.execute(
        _CTE + _SELECT.replace("SELECT street,", f"SELECT CAST({dimension} AS VARCHAR),")
        + f" WHERE street = '{street}' GROUP BY 1 ORDER BY 2 DESC",
        [game, since, until]).fetchall()
    return [(r[0], s) for r, s in zip(rows, _stats([("", *r[1:]) for r in rows]))]
