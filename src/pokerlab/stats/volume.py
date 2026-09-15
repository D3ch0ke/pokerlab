"""Volume tracking: hands, hours and table count over time.

Added because the binding constraint on answering "am I a winning player" is
sample size, and sample size is volume. Table count is tracked alongside
winrate because adding tables reliably costs bb/100, and the only way to find
your own limit is to watch it happen.
"""

from __future__ import annotations

from ..stats.core import NL5

_MONTHLY = """
WITH mins AS (
    SELECT DISTINCT strftime(played_at, '%Y-%m') AS mo,
           date_trunc('minute', played_at) AS t
    FROM hands WHERE game_name = ?
),
conc AS (
    SELECT strftime(played_at, '%Y-%m') AS mo, date_trunc('minute', played_at) AS t,
           count(DISTINCT table_id) AS tabs
    FROM hands WHERE game_name = ? GROUP BY 1, 2
),
hrs AS (SELECT mo, count(*) / 60.0 AS hours FROM mins GROUP BY mo),
tab AS (SELECT mo, avg(tabs) AS avg_tables, max(tabs) AS max_tables FROM conc GROUP BY mo),
res AS (
    SELECT strftime(played_at, '%Y-%m') AS mo, count(*) AS hands,
           sum(hero_net) / 100.0 AS eur, avg(bb) AS bb
    FROM hands WHERE game_name = ? GROUP BY 1
)
SELECT res.mo, res.hands, round(hrs.hours, 1) AS hours,
       round(res.hands / nullif(hrs.hours, 0), 0) AS hands_per_hour,
       round(tab.avg_tables, 2) AS avg_tables, tab.max_tables,
       round(res.eur, 2) AS eur,
       round(100.0 * (res.eur * 100 / res.bb) / res.hands, 1) AS bb100
FROM res JOIN hrs USING (mo) JOIN tab USING (mo) ORDER BY res.mo
"""


def monthly(con, game: str = NL5) -> list[tuple]:
    return con.execute(_MONTHLY, [game, game, game]).fetchall()


def project(hands_per_hour_per_table: float, hours_per_day: float,
            tables: int, days_per_week: float) -> dict:
    per_month = hands_per_hour_per_table * tables * hours_per_day * days_per_week * 4.333
    return {"per_month": round(per_month), "per_year": round(per_month * 12)}
