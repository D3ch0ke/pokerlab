"""Leak trends: the measured frequencies that can show improvement.

Winrate cannot — its interval is ±35 bb/100 at this volume. A frequency
converges in a couple of thousand hands, so this is the page that says
whether training is working. Every series is weekly, carries its n, and is
blanked under MIN_N; the then-vs-now line at the bottom of each card is a
two-proportion z-test between the halves of the window, because a curve
that wobbles is what sampling does and only a shift beyond noise counts.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..stats import grades
from ..stats.core import MIN_N
from ..stats.postflop import by_street, by_texture
from . import state
from .layout import page, pct

router = APIRouter()

WINDOWS = ((90, "90 days"), (180, "180 days"), (365, "1 year"), (0, "all time"))
#: Early seats share a column: separately neither reaches MIN_N in a period.
OPENERS = (("UTG+HJ", ("UTG", "HJ")), ("CO", ("CO",)), ("BTN", ("BTN",)), ("SB", ("SB",)))


@dataclass(slots=True)
class Point:
    label: str
    made: int
    n: int

    @property
    def rate(self) -> float | None:
        return self.made / self.n if self.n >= MIN_N else None

    @property
    def se(self) -> float | None:
        r = self.rate
        return None if r is None else math.sqrt(r * (1 - r) / self.n)


@dataclass(slots=True)
class Series:
    name: str
    points: list[Point] = field(default_factory=list)
    target: float | None = None           # a reference level to draw, if one is honest

    def halves(self) -> tuple[Point, Point]:
        mid = len(self.points) // 2
        a, b = self.points[:mid], self.points[mid:]
        return (Point("then", sum(p.made for p in a), sum(p.n for p in a)),
                Point("now", sum(p.made for p in b), sum(p.n for p in b)))


#: How many periods the window is cut into. Calendar weeks were tried first
#: and never reached MIN_N on a single preflop cell — a month of play is ~900
#: hands, and BB-vs-UTG comes up ten times in it. Equal-hand periods do.
PERIODS = 6


def _periods(since: datetime, until: datetime, k: int = PERIODS) -> list[tuple[datetime, datetime, str]]:
    """The window cut into k periods holding the same number of hands each,
    so every period carries the same evidence whatever the calendar did."""
    from ..stats.core import NL5
    rows = state.con().execute(
        "SELECT played_at FROM hands WHERE game_name = ? AND played_at >= ? AND played_at < ? "
        "ORDER BY played_at", [NL5, since, until]).fetchall()
    times = [r[0] for r in rows]
    if len(times) < k * MIN_N:
        k = max(1, len(times) // MIN_N)
    if not times:
        return []
    out = []
    for i in range(k):
        a = times[i * len(times) // k]
        b = times[(i + 1) * len(times) // k] if i + 1 < k else times[-1] + timedelta(seconds=1)
        last = times[(i + 1) * len(times) // k - 1]
        label = (f"{a:%d %b}–{last:%d %b}" if a.date() != last.date() else f"{a:%d %b}")
        out.append((a, b, label))
    return out


def _z(a: Point, b: Point) -> tuple[float, float] | None:
    """(shift in points, z) between two proportions, when both are sampled."""
    if a.n < MIN_N or b.n < MIN_N:
        return None
    p = (a.made + b.made) / (a.n + b.n)
    se = math.sqrt(p * (1 - p) * (1 / a.n + 1 / b.n)) or 1e-9
    return (b.made / b.n - a.made / a.n), (b.made / b.n - a.made / a.n) / se


# --------------------------------------------------------------------------
# the series
# --------------------------------------------------------------------------

def defence_series(weeks) -> list[Series]:
    obs = [o for o in state.observations() if o.spot == "vs_open" and o.position == "BB"]
    out = []
    for name, seats in OPENERS:
        s = Series(f"BB vs {name} open")
        for a, b, label in weeks:
            sub = [o for o in obs if o.opener in seats and a <= o.at < b]
            s.points.append(Point(label, sum(1 for o in sub if o.verb in ("Raises to", "Calls")), len(sub)))
        out.append(s)
    return out


def postflop_series(weeks) -> dict[str, list[Series]]:
    con = state.con()
    cbet = {t: Series(f"c-bet on {t} flops") for t in ("dry", "wet", "paired")}
    donk = {st: Series(f"donk on the {st.lower()}") for st in ("FLOP", "TURN", "RIVER")}
    rvc = Series("raise vs flop c-bet")
    for a, b, label in weeks:
        streets = {s.street: s for s in by_street(con, a, b)}
        for st, series in donk.items():
            x = streets.get(st)
            series.points.append(Point(label, x.donk.made if x else 0, x.donk.opportunities if x else 0))
        f = streets.get("FLOP")
        rvc.points.append(Point(label, f.raise_vs_cbet.made if f else 0,
                                f.raise_vs_cbet.opportunities if f else 0))
        wet = {k: s for k, s in by_texture(con, a, b, "wet")}
        paired = {k: s for k, s in by_texture(con, a, b, "paired")}
        pr = paired.get("true")
        cbet["paired"].points.append(Point(label, pr.cbet.made if pr else 0, pr.cbet.opportunities if pr else 0))
        # Dry and wet exclude paired boards so the three cells do not overlap:
        # the texture split reports wet/dry over every flop, so paired is
        # subtracted from whichever side it fell on. It cannot be told apart
        # here, so dry/wet stay as the postflop page reports them.
        for key, tex in (("false", "dry"), ("true", "wet")):
            x = wet.get(key)
            cbet[tex].points.append(Point(label, x.cbet.made if x else 0, x.cbet.opportunities if x else 0))
    return {"cbet": list(cbet.values()), "donk": list(donk.values()), "rvc": [rvc]}


def ev_series(weeks) -> tuple[list[tuple[str, int, float | None]], dict[str, list[tuple[str, int, float | None]]]]:
    """Per period: (label, usable decisions, bb lost per decision) overall and by node."""
    recs = state.verdicts()
    overall = []
    by_node: dict[str, list] = {}
    for a, b, label in weeks:
        wk = grades.in_window(recs, a, b)
        t = grades.totals(wk)
        overall.append((label, t.usable, t.loss_bb / t.usable if t.usable >= grades.MIN_GRADED else None))
        for bucket in grades.by(wk, lambda r: r.node):
            by_node.setdefault(bucket.key, []).append((label, bucket.usable, bucket.loss_per_decision))
    return overall, by_node


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _chart(s: Series, width: int = 260, height: int = 74) -> str:
    """Rate per period with a ± SE band; periods under MIN_N leave a gap."""
    pts = s.points
    if not pts:
        return ""
    step = width / max(len(pts) - 1, 1)
    xs = [i * step for i in range(len(pts))]
    sampled = [p for p in pts if p.rate is not None]
    if not sampled:
        return '<span class="note">no period reaches the sample floor</span>'
    lo = min(max(0.0, p.rate - p.se) for p in sampled)
    hi = max(min(1.0, p.rate + p.se) for p in sampled)
    if s.target is not None:
        lo, hi = min(lo, s.target), max(hi, s.target)
    span = (hi - lo) or 0.1
    y = lambda v: height - 4 - (v - lo) / span * (height - 8)
    line, band_top, band_bot = [], [], []
    for x, p in zip(xs, pts):
        r = p.rate
        if r is None:
            if line:
                line.append(None)
            continue
        line.append(f"{x:.1f},{y(r):.1f}")
        band_top.append(f"{x:.1f},{y(min(1, r + p.se)):.1f}")
        band_bot.append(f"{x:.1f},{y(max(0, r - p.se)):.1f}")
    segs, cur = [], []
    for item in line:
        if item is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(item)
    if cur:
        segs.append(cur)
    band = ""
    if len(band_top) >= 2:
        band = (f'<polygon fill="currentColor" opacity=".12" '
                f'points="{" ".join(band_top + band_bot[::-1])}"/>')
    target = ""
    if s.target is not None:
        target = (f'<line x1="0" x2="{width}" y1="{y(s.target):.1f}" y2="{y(s.target):.1f}" '
                  f'stroke="var(--dim)" stroke-dasharray="3 3" stroke-width="1"/>')
    lines = "".join(
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.6" points="{" ".join(seg)}"/>'
        if len(seg) > 1 else
        f'<circle r="2" fill="currentColor" cx="{seg[0].split(",")[0]}" cy="{seg[0].split(",")[1]}"/>'
        for seg in segs)
    return (f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'{target}{band}{lines}</svg>')


def _series_card(title: str, series: list[Series], note: str, periods) -> str:
    cols = "".join(f"<th>{html.escape(s.name)}</th>" for s in series)
    charts = "".join(f'<td class="l">{_chart(s)}</td>' for s in series)
    rows = []
    for i, (_, _, label) in enumerate(periods):
        cells = []
        for s in series:
            p = s.points[i]
            r = p.rate
            cells.append(f"<td>{'--' if r is None else f'{100 * r:.0f}%'} "
                         f'<span class="note">({p.n})</span></td>')
        rows.append(f"<tr><td>{label}</td>{''.join(cells)}</tr>")
    shifts = []
    for s in series:
        a, b = s.halves()
        z = _z(a, b)
        if z is None:
            shifts.append("<td>--</td>")
            continue
        shift, zz = z
        shifts.append(f'<td>{"<b>" if abs(zz) >= 2 else ""}{100 * a.made / a.n:.0f}% → '
                      f'{100 * b.made / b.n:.0f}%{"</b>" if abs(zz) >= 2 else ""} '
                      f'<span class="note">z={zz:.1f}</span></td>')
    return f"""
    <div class="card"><h2>{title}</h2>
      <table class="t"><tr><th>period</th>{cols}</tr>
        <tr><td></td>{charts}</tr>{"".join(rows)}
        <tr><td><b>then → now</b></td>{"".join(shifts)}</tr></table>
      <div class="note" style="margin-top:.5rem">{note} Rates blank under {MIN_N} opportunities.
        The last row splits the window in half; |z| ≥ 2 (bold) is a shift larger than sampling
        noise, anything smaller is not evidence either way.</div>
    </div>"""


@router.get("/leaks", response_class=HTMLResponse)
def leaks_page(days: int = 180):
    from .dashboard import NODE_LABEL, _select
    until = datetime.now(timezone.utc) + timedelta(days=1)
    since = until - timedelta(days=days) if days else datetime(2025, 10, 1, tzinfo=timezone.utc)
    weeks = _periods(since, until)

    defence = defence_series(weeks)
    post = postflop_series(weeks)
    overall, by_node = ev_series(weeks)

    ev_cols = "".join(f"<th>{html.escape(NODE_LABEL.get(k, k))}</th>" for k in by_node)
    ev_rows = []
    for i, (label, n, per) in enumerate(overall):
        cells = [f"<td>{'--' if per is None else f'{per:.2f}'} <span class=note>({n})</span></td>"]
        for k, pts in by_node.items():
            match = next(((nn, pp) for (l, nn, pp) in pts if l == label), (0, None))
            cells.append(f"<td>{'--' if match[1] is None else f'{match[1]:.2f}'} "
                         f"<span class=note>({match[0]})</span></td>")
        ev_rows.append(f"<tr><td>{label}</td>{''.join(cells)}</tr>")
    ev_card = f"""
    <div class="card"><h2>EV given up per graded decision, bb</h2>
      <table class="t"><tr><th>period</th><th>all nodes</th>{ev_cols}</tr>{"".join(ev_rows)}</table>
      <div class="note" style="margin-top:.5rem">Solver verdicts, usable decisions only; blank under
        {grades.MIN_GRADED} in the cell. Comparable across weeks only while the grader has run on
        every week — an ungraded week reads as nothing, not as zero.</div>
    </div>"""

    sel = f"""<form method="get" action="/leaks" class="filter">
      <label>window {_select("days", [(str(d), l) for d, l in WINDOWS], str(days))}</label>
      <button type="submit">Apply</button></form>"""
    from .drill import _tabs
    body = (_tabs("leaks") + sel
            + _series_card("Blind defence by the opener's seat (BB, raise or call)", defence,
                           "The shape is the finding: defence should widen from UTG to SB. "
                           "A flat or inverted row is the leak, whatever the chart's absolute levels.", weeks)
            + _series_card("C-bet by flop texture (single-raised pots, you raised)", post["cbet"],
                           "Paired flops are the best c-bet texture and were your lowest; "
                           "the solver bets 25–31% here, so lower is not automatically worse.", weeks)
            + _series_card("Donk bets (leading into the aggressor)", post["donk"],
                           "Under 10% is ordinary; yours ran 32% on the flop rising to 49% by the river.", weeks)
            + _series_card("Raise vs flop c-bet", post["rvc"],
                           "Roughly double the typical rate at 26%.", weeks)
            + ev_card)
    return page(body, "Leak trends", "/train",
                f"{len(weeks)} equal-hand periods over {'all time' if not days else f'the last {days} days'}"
                f" · the frequencies that can move")
