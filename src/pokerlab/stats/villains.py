"""Who you play against: per-villain preflop and postflop tendencies.

Names are stable across the exports and the regulars recur in hundreds of
hands, so their *frequencies* are often well sampled even though their
holdings never are. Every rate here carries its n and is suppressed below
`MIN_N`, like everywhere else. Their result is their own net at your tables,
in bb/100 -- what they win or lose across every hand you shared with them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .core import NL5, Stat

_SQL = """
WITH pre AS (
    SELECT a.hand_id, a.name, a.position, a.verb,
           COALESCE(SUM(CASE WHEN a.verb = 'Raises to' THEN 1 ELSE 0 END) OVER (
               PARTITION BY a.hand_id ORDER BY a.idx
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS raises_before
    FROM actions a JOIN hands h USING (hand_id)
    WHERE a.street = 'PRE-FLOP' AND NOT a.dead AND NOT a.is_hero
      AND a.verb NOT IN ('Posts SB', 'Posts BB', 'Posts Ante')
      AND h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
),
pf AS (
    SELECT hand_id, name,
           max(CASE WHEN verb IN ('Calls','Raises to','Bets') THEN 1 ELSE 0 END) AS vpip,
           max(CASE WHEN verb = 'Raises to' THEN 1 ELSE 0 END) AS pfr,
           max(CASE WHEN verb = 'Raises to' AND raises_before = 1 THEN 1 ELSE 0 END) AS threebet,
           max(CASE WHEN raises_before = 1 THEN 1 ELSE 0 END) AS faced_open,
           max(CASE WHEN verb = 'Folds' AND raises_before >= 2 THEN 1 ELSE 0 END) AS fold_3b,
           max(CASE WHEN raises_before >= 2 THEN 1 ELSE 0 END) AS faced_3b,
           max(CASE WHEN verb = 'Calls' AND raises_before = 0
                     AND position NOT IN ('SB','BB') THEN 1 ELSE 0 END) AS limped
    FROM pre GROUP BY hand_id, name
),
pfa AS (
    SELECT hand_id, name FROM (
        SELECT hand_id, name, row_number() OVER (PARTITION BY hand_id ORDER BY idx DESC) AS rn
        FROM actions WHERE street = 'PRE-FLOP' AND verb = 'Raises to') WHERE rn = 1
),
post AS (
    -- The window runs over EVERY flop action, hero's included: a villain
    -- facing hero's c-bet is the whole point. Hero's own rows drop out after.
    SELECT * FROM (
        SELECT a.hand_id, a.name, a.street, a.verb, a.is_hero,
               (p.name = a.name) AS is_pfa,
               COALESCE(SUM(CASE WHEN a.verb IN ('Bets','Raises to') THEN 1 ELSE 0 END) OVER w, 0)
                   AS bets_before
        FROM actions a JOIN hands h USING (hand_id) LEFT JOIN pfa p USING (hand_id)
        WHERE a.street = 'FLOP' AND h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
        WINDOW w AS (PARTITION BY a.hand_id, a.street ORDER BY a.idx
                     ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
    ) WHERE NOT is_hero
),
flop AS (
    SELECT hand_id, name,
           max(CASE WHEN is_pfa AND bets_before = 0 THEN 1 ELSE 0 END) AS cbet_opp,
           max(CASE WHEN is_pfa AND bets_before = 0 AND verb = 'Bets' THEN 1 ELSE 0 END) AS cbet,
           max(CASE WHEN NOT is_pfa AND bets_before >= 1 THEN 1 ELSE 0 END) AS faced_cbet,
           max(CASE WHEN NOT is_pfa AND bets_before >= 1 AND verb = 'Folds' THEN 1 ELSE 0 END) AS fold_cbet
    FROM post GROUP BY hand_id, name
),
sd AS (
    SELECT s.hand_id, s.name, h.saw_flop, h.showdown, s.net,
           (s.shown IS NOT NULL AND s.shown <> '') AS showed, h.hero_net, h.bb
    FROM seats s JOIN hands h USING (hand_id)
    WHERE NOT s.is_hero AND h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
)
SELECT sd.name, count(*) AS hands,
       sum(pf.vpip) AS vpip, sum(pf.pfr) AS pfr, count(pf.vpip) AS dealt,
       sum(pf.threebet) AS threebet, sum(pf.faced_open) AS faced_open,
       sum(pf.fold_3b) AS fold_3b, sum(pf.faced_3b) AS faced_3b,
       sum(pf.limped) AS limped,
       sum(flop.cbet) AS cbet, sum(flop.cbet_opp) AS cbet_opp,
       sum(flop.fold_cbet) AS fold_cbet, sum(flop.faced_cbet) AS faced_cbet,
       sum(CASE WHEN sd.showed THEN 1 ELSE 0 END) AS showdowns,
       sum(CASE WHEN sd.saw_flop THEN 1 ELSE 0 END) AS flops,
       sum(sd.net * 1.0 / sd.bb) AS net_bb,
       max(h2.played_at) AS last_seen
FROM sd
LEFT JOIN pf USING (hand_id, name)
LEFT JOIN flop USING (hand_id, name)
JOIN hands h2 ON h2.hand_id = sd.hand_id
GROUP BY sd.name
HAVING count(*) >= ?
ORDER BY hands DESC
"""


@dataclass(slots=True)
class Villain:
    name: str
    hands: int
    vpip: Stat
    pfr: Stat
    threebet: Stat
    fold_to_3bet: Stat
    limp: Stat
    cbet: Stat
    fold_to_cbet: Stat
    showdowns: int
    flops: int
    net_bb: float                # the villain's own result over the shared hands
    last_seen: datetime

    @property
    def bb100(self) -> float:
        return 100 * self.net_bb / self.hands

    @property
    def style(self) -> str:
        """A coarse label, only when both rates are sampled."""
        v, p = self.vpip.pct, self.pfr.pct
        if v is None or p is None:
            return ""
        loose = v >= 35
        passive = p < v * 0.5
        return {(False, False): "tight-aggressive", (False, True): "tight-passive",
                (True, False): "loose-aggressive", (True, True): "loose-passive"}[(loose, passive)]


def villains(con, since: datetime, until: datetime, min_hands: int = 50,
             game: str = NL5) -> list[Villain]:
    rows = con.execute(_SQL, [game, since, until, game, since, until,
                              game, since, until, min_hands]).fetchall()
    out = []
    for (name, hands, vpip, pfr, dealt, tb, faced_open, f3b, faced_3b, limped,
         cbet, cbet_opp, fold_cbet, faced_cbet, showdowns, flops, net, last) in rows:
        out.append(Villain(
            name=name, hands=hands,
            vpip=Stat(vpip or 0, dealt or 0), pfr=Stat(pfr or 0, dealt or 0),
            threebet=Stat(tb or 0, faced_open or 0), fold_to_3bet=Stat(f3b or 0, faced_3b or 0),
            limp=Stat(limped or 0, dealt or 0),
            cbet=Stat(cbet or 0, cbet_opp or 0), fold_to_cbet=Stat(fold_cbet or 0, faced_cbet or 0),
            showdowns=showdowns or 0, flops=flops or 0,
            net_bb=round(net or 0.0, 1), last_seen=last))
    return out
