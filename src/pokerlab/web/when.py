"""When to sit down: looseness of the pool by hour and weekday, and the hours
the known fish are actually at your tables.

Everything here is conditioned on the hours you played. An hour with fewer
than `MIN_CELL` hands is blank, not zero, and the looseness figure carries
its standard error because the whole point is whether hours differ by more
than noise. Your own bb/100 by hour is shown dimmed: at these sample sizes
its standard error is 40–100 bb/100, which is to say it is not a result.
"""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..stats.villains import villains
from ..stats.when import BLOCKS, DAYS, MIN_CELL, looseness
from . import state
from .dashboard import WINDOWS, _scope, _select, _tile, _window
from .layout import page

router = APIRouter()

FISH_VPIP = 35.0      # loose, the same threshold as the style label
FISH_HANDS = 100

CSS = """
table.hm{border-collapse:collapse;font-size:.7rem;font-variant-numeric:tabular-nums}
table.hm th{font-weight:600;color:var(--dim);font-size:.62rem;padding:.15rem .2rem;text-align:center}
table.hm td{width:34px;height:26px;text-align:center;border:1px solid var(--bg);color:var(--fg)}
table.hm td.blank{color:var(--line)}
table.hm td span{display:block;font-size:.55rem;color:var(--dim)}
.hourbar{display:inline-block;height:9px;border-radius:2px;vertical-align:middle;margin-right:.35rem}
.dimnum{color:var(--dim)}
"""


def _shade(mean: float | None, overall: float) -> str:
    """Background from the deviation against the overall: green looser, red tighter."""
    if mean is None:
        return ""
    d = mean - overall
    alpha = min(abs(d) / 0.15, 1.0) * 0.75
    colour = "47,125,79" if d > 0 else "163,58,58"
    return f'style="background:rgba({colour},{alpha:.2f})"'


def _hour_rows(cells, labels, overall: float) -> str:
    rows = []
    for key in sorted(cells):
        c = cells[key]
        m, se = c.mean, c.se
        if m is None:
            rows.append(f'<tr class="dimrow"><td class="l">{labels(key)}</td><td>{c.n}</td>'
                        f'<td colspan="3">under {MIN_CELL} hands</td></tr>')
            continue
        width = int(120 * m)
        cls = "g" if m > overall else "r"
        rows.append(
            f'<tr><td class="l">{labels(key)}</td><td>{c.n:,}</td>'
            f'<td class="l"><i class="hourbar bar {cls}" style="width:{width}px"></i>{100 * m:.0f}%'
            f' <span class="note">±{100 * se:.0f}</span></td>'
            f'<td>{100 * (m - overall):+.0f}</td>'
            f'<td class="dimnum">{c.bb100:+.0f}</td></tr>')
    return "".join(rows)


@router.get("/when", response_class=HTMLResponse)
def when_page(days: int = 0):
    con = state.con()
    since, until = _window(days)
    fish = [v for v in villains(con, since, until, min_hands=FISH_HANDS)
            if v.vpip.pct is not None and v.vpip.pct >= FISH_VPIP]
    fish.sort(key=lambda v: -v.hands)
    w = looseness(con, since, until, [v.name for v in fish])
    if not w.hands or w.overall is None:
        return page('<div class="card"><div class="verdict no">No hands in this window</div></div>',
                    "When", "/when", _scope(days), extra_css=CSS)
    overall = w.overall

    # ---- the headline: the best and worst hour blocks --------------------
    ranked = sorted(((c.mean, h) for h, c in w.by_hour.items() if c.mean is not None), reverse=True)
    loose_hours = ", ".join(f"{h:02d}h" for _, h in ranked[:4])
    tight_hours = ", ".join(f"{h:02d}h" for _, h in sorted(ranked[-4:], key=lambda x: x[1]))
    days_ranked = sorted(((c.mean, d) for d, c in w.by_day.items() if c.mean is not None), reverse=True)
    day_spread = (100 * (days_ranked[0][0] - days_ranked[-1][0])) if len(days_ranked) > 1 else 0
    tiles = "".join([
        _tile("opponents in the pot", f"{100 * overall:.0f}%", f"per hand, over {w.hands:,} hands"),
        _tile("loosest hours", loose_hours or "--",
              f"{100 * ranked[0][0]:.0f}% at {ranked[0][1]:02d}h" if ranked else ""),
        _tile("tightest hours", tight_hours or "--",
              f"{100 * ranked[-1][0]:.0f}% at {ranked[-1][1]:02d}h" if ranked else ""),
        _tile("weekday spread", f"{day_spread:.0f} pts",
              f"{DAYS[days_ranked[0][1]]} loosest, {DAYS[days_ranked[-1][1]]} tightest" if len(days_ranked) > 1 else ""),
    ])

    # ---- by hour, by weekday --------------------------------------------
    head = ('<tr><th class="l">{}</th><th>hands</th><th class="l">opponents in the pot</th>'
            '<th>vs overall</th><th class="dimnum">your bb/100</th></tr>')
    hours = (f'<div class="card"><h2>By hour</h2><table class="t">{head.format("hour")}'
             f'{_hour_rows(w.by_hour, lambda h: f"{h:02d}:00", overall)}</table>'
             f'<div class="note" style="margin-top:.5rem">Share of opponents who voluntarily put money in '
             f'preflop, averaged over your hands in that hour, with its standard error. Your bb/100 is '
             f'dimmed on purpose: with a few hundred hands per hour its error is 40–100 bb/100 and it '
             f'does not tell you anything. The hour is the hand history\'s clock (Europe/Rome).</div></div>')
    weekdays = (f'<div class="card"><h2>By weekday</h2><table class="t">{head.format("day")}'
                f'{_hour_rows(w.by_day, lambda d: DAYS[d], overall)}</table></div>')

    # ---- weekday x hour heatmap -----------------------------------------
    active_hours = sorted({h for (_d, h), c in w.grid.items() if c.n >= MIN_CELL})
    hm_head = "".join(f"<th>{h:02d}</th>" for h in active_hours)
    hm_rows = []
    for d in range(7):
        cells = []
        for h in active_hours:
            c = w.grid.get((d, h))
            if c is None or c.mean is None:
                cells.append(f'<td class="blank">·<span>{c.n if c else 0}</span></td>')
            else:
                cells.append(f'<td {_shade(c.mean, overall)}>{100 * c.mean:.0f}<span>{c.n}</span></td>')
        hm_rows.append(f'<tr><th>{DAYS[d]}</th>{"".join(cells)}</tr>')
    heat = (f'<div class="card"><h2>Weekday × hour</h2><div style="overflow-x:auto">'
            f'<table class="hm"><tr><th></th>{hm_head}</tr>{"".join(hm_rows)}</table></div>'
            f'<div class="note" style="margin-top:.5rem">Percent of opponents in the pot, the hand count '
            f'beneath. Green is looser than your overall {100 * overall:.0f}%, red tighter; a dot is a cell '
            f'under {MIN_CELL} hands. Hours you never play are not shown at all.</div></div>')

    # ---- the check: does the hour effect survive inside each month? -----
    labels = [b for b, _ in BLOCKS]
    month_rows = []
    for m in sorted(w.by_month):
        cells = []
        for b in labels:
            c = w.by_month[m].get(b)
            if c is None or c.mean is None:
                cells.append(f'<td class="dimnum">-- <span class="note">n={c.n if c else 0}</span></td>')
            else:
                cells.append(f'<td>{100 * c.mean:.0f}% <span class="note">n={c.n}</span></td>')
        month_rows.append(f'<tr><td class="l">{m}</td>{"".join(cells)}</tr>')
    months = (f'<div class="card"><h2>The same by month</h2><table class="t"><tr><th class="l">month</th>'
              f'{"".join(f"<th>{b}</th>" for b in labels)}</tr>{"".join(month_rows)}</table>'
              f'<div class="note" style="margin-top:.5rem">The pool changes over the year and you did not '
              f'play every hour every month, so an hour effect is only real if it shows up inside months too. '
              f'Read across a row.</div></div>')

    # ---- the known fish -------------------------------------------------
    fish_rows = []
    for p in w.fish:
        top = p.top_hours(3)
        hours_txt = " · ".join(
            f"{h:02d}h {100 * share:.0f}%<span class='note'> of {n}</span>" if n >= MIN_CELL
            else f"{h:02d}h <span class='note'>{seen} hands</span>"
            for (h, share, n), seen in zip(top, [s for _, s, _ in sorted(p.by_hour, key=lambda r: -r[1])[:3]]))
        best_day = max(p.by_day, key=lambda r: (r[1] / r[2]) if r[2] >= MIN_CELL else -1)
        day_txt = (f"{DAYS[best_day[0]]} {100 * best_day[1] / best_day[2]:.0f}%"
                   f"<span class='note'> of {best_day[2]}</span>" if best_day[2] >= MIN_CELL else "--")
        v = next(x for x in fish if x.name == p.name)
        fish_rows.append(
            f'<tr><td class="l"><a href="/villains/{quote(p.name, safe="")}?days={days}">{html.escape(p.name)}</a></td>'
            f'<td>{p.hands}</td><td>{v.vpip}</td><td class="l">{p.first:%b %y} – {p.last:%b %y}</td>'
            f'<td class="l">{hours_txt}</td><td class="l">{day_txt}</td></tr>')
    fish_card = (f'<div class="card"><h2>Where the known fish are — {len(w.fish)} players, VPIP ≥ {FISH_VPIP:.0f}%, '
                 f'{FISH_HANDS}+ hands</h2><table class="t"><tr><th class="l">player</th><th>hands</th>'
                 f'<th>VPIP</th><th class="l">seen</th><th class="l">hours they are at your table most</th>'
                 f'<th class="l">best day</th></tr>{"".join(fish_rows)}</table>'
                 f'<div class="note" style="margin-top:.5rem">"05h 61% of 400" means: of the 400 hands you '
                 f'played at 05:00 between the first and last time you saw them, they were seated in 61%. '
                 f'The span matters — a player who quit in April is not absent from every hour since. '
                 f'Hours where you have under {MIN_CELL} hands show their raw count instead.</div></div>')

    body = f"""
    <form method="get" action="/when" class="filter">
      <label>window {_select("days", [(str(d), l) for d, l in WINDOWS], str(days))}</label>
      <button type="submit">Show</button>
    </form>
    <div class="tiles">{tiles}</div>
    <div class="cols">{hours}{weekdays}</div>
    {heat}
    {months}
    {fish_card}"""
    return page(body, "When to play", "/when",
                f"{w.hands:,} hands · {_scope(days)} · everything conditioned on the hours you actually played",
                extra_css=CSS, wide=True)
