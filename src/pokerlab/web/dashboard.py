"""The dashboard: overview, hands browser, postflop, preflop, sessions, grading.

Every page is a view over the same stats modules the CLI prints, plus the
verdict store. Nothing is computed here that is not computed there; the page
only decides what sits next to what.
"""

from __future__ import annotations

import html
import statistics
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..bankroll import CONFIDENCE_Z
from ..ranges.chart import available as charts_available, load as load_chart
from ..replay.grader import Filter as GradeFilter, STREETS, candidates
from ..stats import grades
from ..stats.core import EPOCH, FOREVER, MIN_N, NL5, by_position, monthly, summary, window
from ..stats.postflop import by_street, by_texture
from ..stats.sessions import detectable_shift, sessions, tilt_tests
from ..stats.villains import villains
from ..trainer.leakweight import bucket_leaks, curve_is_flat, defence_curve, leak_trend
from . import state
from .layout import bar, mini_cards, money, page, pct, signed, sparkline

router = APIRouter()

WINDOWS = ((1, "24 hours"), (7, "7 days"), (30, "30 days"), (90, "90 days"), (180, "180 days"), (365, "1 year"), (0, "all time"))
NODES = (("", "any node"), ("srp_pfa", "SRP, you raised"), ("srp_caller", "SRP, you called"),
         ("3bet_pfa", "3-bet pot, you 3-bet"), ("3bet_caller", "3-bet pot, you called"),
         ("limped", "limped pot"), ("4bet_pfa", "4-bet pot, you 4-bet"),
         ("4bet_caller", "4-bet pot, you called"))
NODE_LABEL = dict(NODES)
TEXTURES = (("", "any texture"), ("dry", "dry"), ("wet", "wet"), ("paired", "paired"))


def _window(days: int) -> tuple[datetime, datetime]:
    return (EPOCH, FOREVER) if not days else window(days)


def _scope(days: int) -> str:
    return "all time" if not days else f"last {days} days"


def _window_select(days: int, path: str, extra: str = "") -> str:
    opts = "".join(f'<option value="{d}"{" selected" if d == days else ""}>{label}</option>'
                   for d, label in WINDOWS)
    return (f'<form method="get" action="{path}" class="filter">{extra}'
            f'<label>window <select name="days" onchange="this.form.submit()">{opts}</select></label>'
            f'</form>')


def _select(name: str, options, selected: str) -> str:
    return f'<select name="{name}">' + "".join(
        f'<option value="{html.escape(v)}"{" selected" if v == selected else ""}>'
        f'{html.escape(label)}</option>' for v, label in options) + "</select>"


def _tile(k: str, v: str, n: str = "") -> str:
    return f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div><div class="n">{n}</div></div>'


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------

def _ci(con, since, until) -> tuple[float, int] | None:
    rows = con.execute(
        "SELECT hero_net * 1.0 / bb FROM hands WHERE game_name = ? AND played_at >= ? "
        "AND played_at < ?", [NL5, since, until]).fetchall()
    if len(rows) < 2:
        return None
    nets = [r[0] for r in rows]
    se100 = 100 * statistics.stdev(nets) / len(nets) ** 0.5
    return CONFIDENCE_Z * se100, len(nets)


@router.get("/", response_class=HTMLResponse)
def overview(days: int = 90, worst: str = ""):
    con = state.con()
    since, until = _window(days)
    s = summary(con, since, until)
    if not s["hands"]:
        return page(_window_select(days, "/") +
                    '<div class="card"><div class="verdict no">No hands in this window</div></div>',
                    "Overview", "/", "nothing to show")

    ci = _ci(con, since, until)
    ci_txt = f"± {ci[0]:.0f} at 95%" if ci else ""
    gross = s["bb100"] + s["rake_bb100"]
    tiles = [
        _tile("hands", f"{s['hands']:,}", f"{s['since']} → {s['until']}"),
        _tile("net", signed(s["net_eur"], " €", 2), f"{s['bb100']:+.1f} bb/100 {ci_txt}"),
        _tile("rake paid", f"{s['rake_bb100']:.1f} bb/100",
              f"€{s['rake_eur']:.2f} · {100 * s['rake_bb100'] / gross:.0f}% of gross" if gross > 0
              else f"€{s['rake_eur']:.2f}"),
        _tile("VPIP / PFR", f"{s["VPIP"]} / {s["PFR"]}",
              f"3-bet {s['3bet']} · limp {s['limp']}"),
        _tile("WWSF", str(s["WWSF"]), "won when saw flop"),
        _tile("WTSD / W$SD", f"{s["WTSD"]} / {s["W$SD"]}",
              "went to showdown / won at it"),
    ]
    recs = grades.in_window(state.verdicts(), since, until)
    tot = grades.totals(recs)
    if tot.graded:
        per = tot.loss_per_100_hands
        tiles.append(_tile("EV given up", f"{per:.1f} bb/100" if per is not None else "--",
                           f"over {tot.hands:,} graded hands · {tot.usable} usable decisions"))

    pos_rows = "".join(
        f"<tr><td>{p}</td><td>{n:,}</td><td>{vpip}%</td><td>{pfr}%</td>"
        f"<td>{'--' if tb is None else f'{tb}%'}</td><td>{limp}%</td>"
        f"<td>{signed(net, ' €', 2)}</td></tr>"
        for p, n, vpip, pfr, tb, limp, net in by_position(con, since, until))

    months = monthly(con)
    trend_rows = "".join(
        f'<tr class="{"dimrow" if n < 100 else ""}"><td>{m}</td><td>{n:,}</td>'
        f"<td>{vpip}%</td><td>{pfr}%</td><td>{signed(bb100)}</td></tr>"
        for m, n, vpip, pfr, bb100 in months[-12:])
    spark_v = sparkline([r[2] for r in months])
    spark_b = sparkline([r[4] for r in months])

    from .review import session_card, verdicts_in
    from ..stats.sessions import sessions as _sessions
    sess = _sessions(con)
    last = sess[-1] if sess else None
    if last is not None and worst == "session":
        worst_recs = grades.biggest(verdicts_in(last), 6)
    else:
        worst_recs = grades.biggest(recs, 6)
    worst_toggle = (
        f'<span class="note">'
        f'{"<b>this window</b>" if worst != "session" else f"<a href=\"/?days={days}\">this window</a>"}'
        f' · '
        f'{"<b>last session</b>" if worst == "session" else f"<a href=\"/?days={days}&worst=session\">last session</a>"}'
        f'</span>')
    node_rows = "".join(
        f"<tr><td>{html.escape(NODE_LABEL.get(b.key, b.key))}</td><td>{b.usable}</td>"
        f"<td>{'--' if b.mistake_rate is None else pct(b.mistake_rate)}</td>"
        f"<td>{'--' if b.loss_per_decision is None else f'{b.loss_per_decision:.2f}'}</td>"
        f"<td>{signed(-b.loss_bb, ' bb')}</td></tr>"
        for b in grades.by(recs, lambda r: r.node) if b.graded)
    node_card = (f'<div class="card"><h2>Where the EV goes, by node</h2>'
                 f'<table class="t"><tr><th>node</th><th>usable</th><th>mistakes</th>'
                 f'<th>bb / decision</th><th>total</th></tr>{node_rows}</table>'
                 f'<div class="note" style="margin-top:.5rem">bb per decision is blank under '
                 f'{grades.MIN_GRADED} usable decisions. This is the priority list: train the '
                 f'node with the largest per-decision loss first, not the one with the most '
                 f'hands.</div></div>' if node_rows else "")
    worst = worst_recs
    worst_rows = "".join(
        f'<tr><td><a href="/replay/{r.hand_id}?step={r.idx}">{r.played_at[:10]}</a></td>'
        f"<td>{mini_cards(r.hero_combo and [r.hero_combo[:2], r.hero_combo[2:]] or '')}</td>"
        f"<td>{mini_cards(r.board)}</td><td>{r.street.lower()}</td>"
        f"<td>{html.escape(NODE_LABEL.get(r.node, r.node))}</td>"
        f"<td>{html.escape(r.hero_step)}</td><td>{signed(-(r.loss_bb() or 0), ' bb')}</td></tr>"
        for r in worst)

    sess = sessions(con)[-6:][::-1]
    sess_rows = "".join(
        f"<tr><td>{x.start:%Y-%m-%d %H:%M}</td><td>{x.hands}</td>"
        f"<td>{signed(x.final, ' bb')}</td><td>{x.peak:+.0f} bb</td></tr>" for x in sess)

    body = f"""
    {_window_select(days, "/")}
    <div class="tiles">{"".join(tiles)}</div>
    {session_card(last, index=0, link=True) if last is not None else ""}
    {node_card}
    <div class="cols">
      <div class="card"><h2>By position</h2>
        <table class="t"><tr><th>seat</th><th>hands</th><th>VPIP</th><th>PFR</th>
        <th>3-bet</th><th>limp</th><th>net</th></tr>{pos_rows}</table>
        <div class="note" style="margin-top:.5rem">3-bet shown only from {MIN_N} raises faced.</div>
      </div>
      <div class="card"><h2>Monthly trend</h2>
        <div class="note" style="margin-bottom:.5rem">VPIP {spark_v} &nbsp; bb/100 {spark_b}
          — the shape is the point, not the mean. Months under 100 hands are dimmed.</div>
        <table class="t"><tr><th>month</th><th>hands</th><th>VPIP</th><th>PFR</th>
        <th>bb/100</th></tr>{trend_rows}</table>
      </div>
    </div>
    <div class="cols">
      <div class="card"><h2>Biggest graded mistakes</h2>
        <div style="margin-bottom:.4rem">{worst_toggle}</div>
        {'<table class="t"><tr><th>hand</th><th>you</th><th>board</th><th>street</th><th>node</th><th>you did</th><th>EV</th></tr>' + worst_rows + '</table>' if worst_rows else
         '<div class="note">Nothing graded in this window yet. <a href="/grades">Run the grader</a> to solve your decisions.</div>'}
        {'<div class="note" style="margin-top:.5rem">Trustworthy solves only; a decision the range band could not settle is left out.</div>' if worst_rows else ''}
      </div>
      <div class="card"><h2>Recent sessions</h2>
        <table class="t"><tr><th>start</th><th>hands</th><th>result</th><th>peak</th></tr>{sess_rows}</table>
        <div class="note" style="margin-top:.5rem"><a href="/sessions">Tilt tests against a null model →</a></div>
      </div>
    </div>"""
    return page(body, "Overview", "/", f"NL5 · {_scope(days)} · {s['since']} → {s['until']}")


# --------------------------------------------------------------------------
# hands
# --------------------------------------------------------------------------

_HANDS = """
WITH pre AS (
    SELECT hand_id,
           count(*) FILTER (WHERE verb = 'Raises to') AS raises,
           max(CASE WHEN verb = 'Raises to' THEN idx END) AS last_raise,
           max(CASE WHEN is_hero AND verb = 'Raises to' THEN idx END) AS hero_last_raise
    FROM actions WHERE street = 'PRE-FLOP' GROUP BY hand_id),
fl AS (SELECT hand_id, count(DISTINCT name) AS players FROM actions WHERE street = 'FLOP' GROUP BY hand_id)
SELECT h.hand_id, h.played_at, h.hero_pos, h.hero_cards, h.board, h.hero_net, h.bb,
       h.showdown, h.saw_flop, h.n_players, h.total_pot,
       CASE WHEN pre.raises = 0 THEN 'limped'
            WHEN pre.raises = 1 THEN 'srp' WHEN pre.raises = 2 THEN '3bet' ELSE '4bet' END
         || CASE WHEN pre.raises = 0 THEN ''
                 WHEN pre.hero_last_raise = pre.last_raise THEN '_pfa' ELSE '_caller' END AS node,
       fl.players, h.flop_wet, h.flop_paired
FROM hands h LEFT JOIN pre USING (hand_id) LEFT JOIN fl USING (hand_id)
WHERE h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
  AND (? = '' OR h.hero_pos = ?)
  AND (NOT ? OR h.saw_flop)
  AND (NOT ? OR h.showdown)
  AND abs(h.hero_net) >= ?
  AND (? = '' OR EXISTS (SELECT 1 FROM seats s
        WHERE s.hand_id = h.hand_id AND NOT s.is_hero AND s.name = ?))
ORDER BY h.played_at DESC
"""


def _texture(wet, paired) -> str:
    if paired:
        return "paired"
    return "wet" if wet else "dry"


HANDS_FILTER = ("days", "pos", "node", "tex", "villain", "minnet", "flop", "sd", "graded", "sort")


def hands_query(days: int = 90, pos: str = "", node: str = "", tex: str = "", villain: str = "",
                minnet: float = 0.0, flop: str = "", sd: str = "", graded: str = "",
                sort: str = "date") -> str:
    """The query string that names one filtered, sorted view of /hands."""
    return (f"days={days}&pos={pos}&node={node}&tex={tex}&villain={quote(villain)}"
            f"&minnet={minnet:g}&flop={flop}&sd={sd}&graded={graded}&sort={sort}")


def hands_from_query(q: str) -> list[dict]:
    """The same list /hands shows for a query string; the replayer walks it."""
    got = {k: v[-1] for k, v in parse_qs(q).items() if k in HANDS_FILTER}
    try:
        kw = dict(got)
        if "days" in kw:
            kw["days"] = int(kw["days"])
        if "minnet" in kw:
            kw["minnet"] = float(kw["minnet"])
    except ValueError:
        return []
    return hand_items(**kw)


def hand_items(days: int = 90, pos: str = "", node: str = "", tex: str = "", villain: str = "",
               minnet: float = 0.0, flop: str = "", sd: str = "", graded: str = "",
               sort: str = "date") -> list[dict]:
    """Every hand a /hands filter admits, in the order the page shows them."""
    con = state.con()
    since, until = _window(days)
    rows = con.execute(_HANDS, [NL5, since, until, pos, pos, bool(flop), bool(sd),
                                int(round(minnet * 100)), villain, villain]).fetchall()
    verdicts = state.by_hand()

    items = []
    for (hid, at, hpos, cards, board, net, bb, showdown, saw_flop, n, pot,
         hnode, players, wet, paired) in rows:
        if node and hnode != node:
            continue
        if tex and (not saw_flop or _texture(wet, paired) != tex):
            continue
        recs = verdicts.get(hid, [])
        losses = [r.loss_bb() or 0.0 for r in recs if grades._ok(r)]
        mistakes = [r for r in recs if grades._ok(r) and r.base and r.base.approved is False]
        unstable = any(r.stable is False for r in recs)
        if graded == "graded" and not recs:
            continue
        if graded == "mistakes" and not mistakes:
            continue
        if graded == "unstable" and not unstable:
            continue
        items.append(dict(hid=hid, at=at, pos=hpos, cards=cards, board=board, net=net, bb=bb,
                          showdown=showdown, saw_flop=saw_flop, n=n, pot=pot, node=hnode,
                          players=players, tex=_texture(wet, paired) if saw_flop else "",
                          graded=len(recs), loss=sum(losses), mistakes=len(mistakes),
                          unstable=unstable))
    if sort == "net":
        items.sort(key=lambda x: x["net"])
    elif sort == "win":
        items.sort(key=lambda x: -x["net"])
    elif sort == "loss":
        items.sort(key=lambda x: -x["loss"])
    return items


@router.get("/hands", response_class=HTMLResponse)
def hands(days: int = 90, pos: str = "", node: str = "", tex: str = "", villain: str = "",
          minnet: float = 0.0, flop: str = "", sd: str = "", graded: str = "",
          sort: str = "date", page_no: int = 0):
    items = hand_items(days, pos, node, tex, villain, minnet, flop, sd, graded, sort)
    limit = 60
    total = len(items)
    shown = items[page_no * limit:(page_no + 1) * limit]

    q = hands_query(days, pos, node, tex, villain, minnet, flop, sd, graded, sort)
    # Each row carries the filter it came from, so the replayer's next/prev
    # hand walks this list rather than the calendar.
    via = quote(q, safe="")
    form = f"""
    <form method="get" action="/hands" class="filter">
      <label>window {_select("days", [(str(d), l) for d, l in WINDOWS], str(days))}</label>
      <label>seat {_select("pos", [("", "any")] + [(p, p) for p in ("UTG", "HJ", "CO", "BTN", "SB", "BB")], pos)}</label>
      <label>node {_select("node", NODES, node)}</label>
      <label>flop {_select("tex", TEXTURES, tex)}</label>
      <label>villain <input type="text" name="villain" value="{html.escape(villain)}" placeholder="any" style="width:110px"></label>
      <label>min €swing <input type="number" name="minnet" value="{minnet:g}" step="0.5" style="width:60px"></label>
      <label><input type="checkbox" name="flop" value="1" {"checked" if flop else ""}> saw flop</label>
      <label><input type="checkbox" name="sd" value="1" {"checked" if sd else ""}> showdown</label>
      <label>grading {_select("graded", [("", "any"), ("graded", "graded"), ("mistakes", "with a mistake"), ("unstable", "band disagreed")], graded)}</label>
      <label>sort {_select("sort", [("date", "newest"), ("net", "biggest loss"), ("win", "biggest win"), ("loss", "most EV given up")], sort)}</label>
      <button type="submit">Filter</button>
    </form>
    <div class="filter" style="margin-top:-.5rem"><span>quick:</span>
      <a href="/hands?days=1&sort=loss&graded=graded">last 24 h, most EV given up</a> ·
      <a href="/hands?days=7&graded=mistakes&sort=loss">this week's mistakes</a> ·
      <a href="/hands?days=30&node=3bet_caller&graded=graded&sort=loss">3-bet pots as caller</a> ·
      <a href="/review">review the last session</a></div>"""

    if not shown:
        return page(form + '<div class="card"><div class="verdict no">No hands match</div>'
                    '<div class="note">Nothing is being sampled behind your back — widen the filter.</div></div>',
                    "Hands", "/hands", f"0 hands")

    trs = []
    for x in shown:
        cls = "win" if x["net"] > 0 else "lose" if x["net"] < 0 else ""
        if x["graded"]:
            if x["mistakes"]:
                g = (f'<span class="badge b-no">{x["mistakes"]} mistake{"s" if x["mistakes"] > 1 else ""}</span>'
                     f' <span class="neg">{-x["loss"]:+.1f}bb</span>')
            elif x["unstable"]:
                g = '<span class="badge b-mid">band disagreed</span>'
            else:
                g = '<span class="badge b-ok">holds up</span>'
        else:
            g = ""
        outcome = "showdown" if x["showdown"] else "no showdown" if x["saw_flop"] else "folded pre"
        trs.append(
            f'<tr><td><a href="/replay/{x["hid"]}?via={via}">{x["at"]:%Y-%m-%d %H:%M}</a></td>'
            f'<td>{html.escape(x["pos"] or "?")}</td><td>{mini_cards(x["cards"] or "")}</td>'
            f'<td>{mini_cards(x["board"] or "")}</td>'
            f'<td><span class="note">{html.escape(NODE_LABEL.get(x["node"], x["node"] or ""))}'
            f'{" · " + x["tex"] if x["tex"] else ""}</span></td>'
            f'<td>{money(x["pot"])}</td><td class="{cls}">{x["net"] / x["bb"]:+.1f}bb</td>'
            f'<td><span class="note">{outcome}</span></td><td>{g}</td></tr>')

    nav = []
    if page_no:
        nav.append(f'<a href="/hands?{q}&page_no={page_no - 1}">&larr; previous</a>')
    if (page_no + 1) * limit < total:
        nav.append(f'<a href="/hands?{q}&page_no={page_no + 1}">next &rarr;</a>')

    body = (form +
            f'<div class="card"><table class="hands"><tr><th>played</th><th>seat</th><th>you</th>'
            f'<th>board</th><th>node</th><th>pot</th><th>result</th><th></th><th>grading</th></tr>'
            f'{"".join(trs)}</table>'
            f'<div class="filter" style="margin-top:1rem;gap:1rem">{" ".join(nav)}</div></div>')
    return page(body, "Hands", "/hands",
                f"{total:,} hands · {_scope(days)} · showing {page_no * limit + 1}–{page_no * limit + len(shown)}")


# --------------------------------------------------------------------------
# villains
# --------------------------------------------------------------------------

#: Column key -> (header label, value, default direction). Every column sorts;
#: a rate that is suppressed (too few opportunities) always sorts last, so a
#: click on "fold to 3-bet" never puts the unmeasured players on top.
_VCOLS = (
    ("name", "villain", lambda v: v.name.lower(), "asc"),
    ("hands", "hands", lambda v: v.hands, "desc"),
    ("recent", "last seen", lambda v: v.last_seen, "desc"),
    ("vpip", "VPIP", lambda v: v.vpip.pct, "desc"),
    ("pfr", "PFR", lambda v: v.pfr.pct, "desc"),
    ("threebet", "3-bet", lambda v: v.threebet.pct, "desc"),
    ("fold3b", "fold to 3-bet", lambda v: v.fold_to_3bet.pct, "desc"),
    ("limp", "limp", lambda v: v.limp.pct, "desc"),
    ("cbet", "c-bet", lambda v: v.cbet.pct, "desc"),
    ("foldcb", "fold to c-bet", lambda v: v.fold_to_cbet.pct, "desc"),
    ("shown", "shown", lambda v: v.showdowns, "desc"),
    ("bb100", "their bb/100", lambda v: v.bb100, "desc"),
    ("style", "style", lambda v: v.style or None, "asc"),
)
_VSORT = {k: (val, default) for k, _, val, default in _VCOLS}

VILLAINS_CSS = """
table.t tr.noterow td{text-align:left;white-space:normal;font-size:.78rem;line-height:1.4;
color:var(--dim);padding:.1rem .5rem .5rem 1.4rem;border-bottom:1px solid var(--line)}
table.t tr.noterow td div{color:var(--fg);opacity:.85;max-width:100ch}
table.t tr.noterow td .when{font-size:.66rem;text-transform:uppercase;letter-spacing:.05em}
table.t tr:has(+ tr.noterow) td{border-bottom:0}
"""


def _sort_villains(rows, sort: str, direction: str):
    val, _ = _VSORT.get(sort, _VSORT["hands"])
    keyed = [(val(v), v) for v in rows]
    measured = [(k, v) for k, v in keyed if k is not None]
    missing = [v for k, v in keyed if k is None]
    measured.sort(key=lambda kv: kv[0], reverse=(direction == "desc"))
    return [v for _, v in measured] + missing


@router.get("/villains", response_class=HTMLResponse)
def villains_page(days: int = 0, min_hands: int = 50, sort: str = "hands", q: str = "",
                  dir: str = ""):
    con = state.con()
    since, until = _window(days)
    # A searched name is wanted even when it is thin: drop the floor to 1 so a
    # villain from last night with 12 hands still comes up, with n on show.
    rows = villains(con, since, until, min_hands=1 if q else max(1, min_hands))
    if q:
        needle = q.strip().lower()
        rows = [v for v in rows if needle in v.name.lower()]
    if sort not in _VSORT:
        sort = "hands"
    direction = dir if dir in ("asc", "desc") else _VSORT[sort][1]
    rows = _sort_villains(rows, sort, direction)

    def th(key: str, label: str, default: str) -> str:
        # Clicking the active column flips it; any other column starts on its natural side.
        nxt = ("asc" if direction == "desc" else "desc") if key == sort else default
        arrow = (" &#9660;" if direction == "desc" else " &#9650;") if key == sort else ""
        cls = ' class="l"' if key in ("name", "style") else ""
        return (f'<th{cls}><a href="/villains?q={html.escape(q)}&days={days}&min_hands={min_hands}'
                f'&sort={key}&dir={nxt}" style="color:inherit">{label}{arrow}</a></th>')

    heads = "".join(th(k, label, default) for k, label, _, default in _VCOLS)

    def cell(stat) -> str:
        return f"{bar(stat.pct / 100 if stat.pct is not None else None, 40)}{stat}"

    notes = state.notes()

    def note_row(name: str) -> str:
        n = notes.get(name)
        if n is None:
            return ""
        lines = "".join(f"<div>{html.escape(l)}</div>" for l in n.text.strip().split("\n"))
        return (f'<tr class="noterow"><td colspan="{len(_VCOLS)}">{lines}'
                f'<span class="when">note · {n.written} · {n.hands:,} hands · {n.by}</span></td></tr>')

    trs = "".join(
        f'<tr><td><a href="/villains/{quote(v.name, safe="")}?days={days}"><b>{html.escape(v.name)}</b></a> '
        f'<a href="/hands?days={days}&villain={quote(v.name)}" class="note">hands</a> '
        f'<a href="/train/postflop?villain={quote(v.name)}" class="note" title="drill your graded spots against this villain">drill</a></td>'
        f"<td>{v.hands:,}</td><td>{v.last_seen:%Y-%m-%d}</td>"
        f"<td>{cell(v.vpip)}</td><td>{cell(v.pfr)}</td><td>{v.threebet}</td><td>{v.fold_to_3bet}</td>"
        f"<td>{v.limp}</td><td>{v.cbet}</td><td>{v.fold_to_cbet}</td><td>{v.showdowns}</td>"
        f"<td>{signed(v.bb100)}</td><td class=\"l\"><span class=\"note\">{v.style}</span></td></tr>"
        f"{note_row(v.name)}"
        for v in rows)
    body = f"""
    <form method="get" action="/villains" class="filter">
      <label>name <input type="text" name="q" value="{html.escape(q)}" placeholder="search" style="width:150px" autofocus></label>
      <label>window {_select("days", [(str(d), l) for d, l in WINDOWS], str(days))}</label>
      <label>at least <input type="number" name="min_hands" value="{min_hands}" style="width:60px"> hands</label>
      <input type="hidden" name="sort" value="{html.escape(sort)}">
      <input type="hidden" name="dir" value="{html.escape(direction)}">
      <button type="submit">Show</button>
    </form>
    <div class="card">
      <table class="t"><tr>{heads}</tr>{trs}</table>
      <div class="note" style="margin-top:.6rem">Frequencies are theirs over every hand you shared, suppressed
        under {MIN_N} opportunities. "Their bb/100" is their own result at your tables — mostly variance at
        these sample sizes, so read the frequencies, not the money. Style needs both VPIP and PFR sampled:
        loose is VPIP ≥ 35%, passive is PFR under half of VPIP. Click a column to sort by it, again to flip;
        unsampled rates always sort last. Click a name for the full profile: positions, streets,
        showdowns, your history against them, and the note.</div>
    </div>"""
    if q and not rows:
        body = body.replace('<div class="card">', '<div class="card"><div class="verdict no">No villain matches '
                            f'"{html.escape(q)}"</div>', 1)
    sub = (f'{len(rows)} players matching "{html.escape(q)}" · {_scope(days)}' if q
           else f"{len(rows)} players with {min_hands}+ hands · {_scope(days)}")
    return page(body, "Villains", "/villains", sub, extra_css=VILLAINS_CSS)


# --------------------------------------------------------------------------
# postflop
# --------------------------------------------------------------------------

@router.get("/postflop", response_class=HTMLResponse)
def postflop(days: int = 90, dim: str = "wet"):
    con = state.con()
    since, until = _window(days)
    street_rows = "".join(
        f"<tr><td>{s.street.lower()}</td><td>{s.hands:,}</td><td>{s.cbet}</td>"
        f"<td>{s.fold_to_cbet}</td><td>{s.raise_vs_cbet}</td><td>{s.donk}</td><td>{s.stab}</td>"
        f"<td>{'--' if s.aggression is None else f'{s.aggression:.2f}'}</td></tr>"
        for s in by_street(con, since, until))
    dims = (("wet", "wet / dry"), ("paired", "paired"), ("high", "high card"),
            ("suits", "suits"), ("connect", "connectedness"))
    def _tex_label(label) -> str:
        if dim == "wet":
            return "wet" if label in (True, "true") else "dry"
        if dim == "paired":
            return "paired" if label in (True, "true") else "unpaired"
        return str(label)

    tex_rows = "".join(
        f"<tr><td>{html.escape(_tex_label(label))}</td><td>{s.hands:,}</td><td>{s.cbet}</td>"
        f"<td>{s.fold_to_cbet}</td><td>{s.stab}</td></tr>"
        for label, s in by_texture(con, since, until, dim))

    recs = grades.in_window(state.verdicts(), since, until)
    cells = grades.cbet_baseline(recs)
    base_rows = "".join(
        f"<tr><td>{c.texture}</td><td>{c.n}</td>"
        f"<td>{bar(c.solver, 80)}{pct(c.solver)}</td><td>{bar(c.hero, 80, 'g')}{pct(c.hero)}</td>"
        f"<td>{'' if c.gap is None else signed(100 * c.gap, ' pts', 0)}</td></tr>"
        for c in cells)
    baseline = (f"""
      <table class="t"><tr><th>flop</th><th>spots</th><th>solver c-bets</th><th>you c-bet</th><th>gap</th></tr>{base_rows}</table>
      <div class="note" style="margin-top:.5rem">Both columns come from the same graded spots: the
        solver's bet frequency over the range you actually arrive with, beside what you did on those
        flops. Under {MIN_N} spots a texture shows its count only. Single-raised pots where you
        raised preflop, heads-up, your first decision on the flop with no bet in front.</div>"""
                if cells else
                '<div class="note">No graded c-bet spots in this window. On the <a href="/grades">grading page</a>, '
                'grade the flop at the "SRP, you raised" node, first decision only — 947 spots, roughly one night.</div>')

    extra = f'<label>texture {_select("dim", dims, dim)}</label>'
    body = f"""
    {_window_select(days, "/postflop", extra)}
    <div class="card"><h2>By street</h2>
      <table class="t"><tr><th>street</th><th>hands</th><th>c-bet</th><th>fold to c-bet</th>
      <th>raise vs c-bet</th><th>donk</th><th>stab</th><th>AF</th></tr>{street_rows}</table>
      <div class="note" style="margin-top:.5rem">donk = you bet before the preflop aggressor acts ·
        stab = they checked, you bet · AF = (bets + raises) / calls. Both donk and stab are undefined in limped pots.</div>
    </div>
    <div class="cols">
      <div class="card"><h2>Flop by texture</h2>
        <table class="t"><tr><th>{html.escape(dict(dims)[dim])}</th><th>hands</th><th>c-bet</th>
        <th>fold to c-bet</th><th>stab</th></tr>{tex_rows}</table>
        <div class="note" style="margin-top:.5rem">Texture is the flop only — the runout must not relabel it.</div>
      </div>
      <div class="card"><h2>Solver c-bet baseline</h2>{baseline}</div>
    </div>"""
    return page(body, "Postflop", "/postflop", f"NL5 · {_scope(days)}")


# --------------------------------------------------------------------------
# preflop
# --------------------------------------------------------------------------

@router.get("/preflop", response_class=HTMLResponse)
def preflop(chart: str = "rfi_6max_100bb", days: int = 90):
    obs = state.observations()
    charts = charts_available()
    try:
        ch = load_chart(chart)
    except FileNotFoundError:
        ch = charts[0]
    opts = [(c.id, f"{c.label} ({c.confidence})") for c in charts]

    leaks = bucket_leaks(obs, ch, half_life=1e9)[:12]
    leak_rows = "".join(
        f"<tr><td>{l.position}</td><td>{html.escape(l.bucket)}</td><td>{pct(l.observed)}</td>"
        f"<td>{pct(l.prescribed)}</td>"
        f'<td><span class="badge {"b-no" if l.mass > 20 else "b-mid"}">{l.kind}</span></td>'
        f"<td>{l.weight:.0f}</td></tr>" for l in leaks)

    trends = leak_trend(obs, ch, split_days=days)
    trend_rows = "".join(
        f"<tr><td>{html.escape(t.bucket)}</td><td>{pct(t.chart)}</td><td>{pct(t.early)}</td>"
        f"<td>{pct(t.recent)}</td><td>{html.escape(t.kind)}</td><td>{html.escape(t.trend)}</td>"
        f"<td>{t.early_n}/{t.recent_n}</td></tr>" for t in trends)

    # The defence curve is a vs-open question whichever chart is being browsed;
    # it is the one preflop finding that survives every chart's confidence.
    defence = next((c for c in charts if c.spot == "vs_open" and c.confidence != "derived"), None)
    curves = []
    for hero_pos in ("BB", "SB"):
        try:
            cells = defence_curve(obs, defence, hero_pos=hero_pos) if defence else []
        except Exception:                                  # noqa: BLE001 - chart without this node
            continue
        if not cells:
            continue
        flat = curve_is_flat(cells)
        rows = "".join(
            f"<tr><td>vs {c.opener}</td><td>{c.n}</td>"
            f"<td>{bar(c.observed, 80, 'g')}{pct(c.observed)}</td>"
            f"<td>{bar(c.prescribed, 80)}{pct(c.prescribed)}</td>"
            f"<td>{signed(100 * c.gap, ' pts', 0)}</td></tr>" for c in cells)
        curves.append(f"""
          <div class="card"><h2>{hero_pos} defence by opener's seat</h2>
            {'<div class="warn">Flat: your defence barely responds to who opened. It should run from tight vs UTG to wide vs SB.</div>' if flat else ''}
            <table class="t"><tr><th>opener</th><th>n</th><th>you defend</th><th>chart</th><th>gap</th></tr>{rows}</table>
            <div class="note" style="margin-top:.5rem">Defend = raise + call, both sides, against
              {html.escape(defence.label)}. Seats with too few openers faced are omitted, not estimated.</div>
          </div>""")

    body = f"""
    <form method="get" action="/preflop" class="filter">
      <label>chart {_select("chart", opts, ch.id)}</label>
      <label>then/now split <input type="number" name="days" value="{days}" style="width:60px"> days</label>
      <button type="submit">Show</button>
    </form>
    <div class="warn">Charts are hand-authored references carrying their own confidence
      (<b>{html.escape(ch.confidence)}</b>, {html.escape(ch.assumption)}), not solver output.
      Findings about the <i>shape</i> of a deviation are stronger than its level.
      <span class="note">{html.escape(ch.source)}</span></div>
    <div class="cols">
      <div class="card"><h2>Largest deviations, all time</h2>
        <table class="t"><tr><th>seat</th><th>bucket</th><th>you</th><th>chart</th><th></th><th>n</th></tr>{leak_rows}</table>
        <div class="note" style="margin-top:.5rem">Blended across 11 months, including when you played very differently. History, not a diagnosis of today.</div>
      </div>
      <div class="card"><h2>Then vs now</h2>
        {'<table class="t"><tr><th>bucket</th><th>chart</th><th>then</th><th>now</th><th></th><th>trend</th><th>n</th></tr>' + trend_rows + '</table>' if trend_rows else '<div class="note">No bucket has enough decisions in both halves to compare.</div>'}
        <div class="note" style="margin-top:.5rem">Only buckets with enough data in both halves. Most cannot be judged on recent play at all and are omitted rather than guessed.</div>
      </div>
    </div>
    {"".join(curves)}"""
    return page(body, "Preflop", "/preflop", f"graded against {html.escape(ch.label)}")


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

@router.get("/sessions", response_class=HTMLResponse)
def sessions_page(peak: float = 50):
    con = state.con()
    sess = sessions(con)
    # 2,000 reshuffles of every hand: seconds, and the answer only changes on import.
    key = ("tilt", peak, len(sess))
    if key not in state._state:
        state._state[key] = tilt_tests(sess, min_peak=peak)
    tests = state._state[key]
    lengths = sorted(s.hands for s in sess)
    test_rows = "".join(
        f"<tr><td>{html.escape(t.name)}</td><td>{t.observed:.0f}</td><td>{t.null_median:.0f}</td>"
        f"<td>{t.null_lo:.0f} .. {t.null_hi:.0f}</td>"
        f'<td class="{"neg" if t.significant else "pos"}">{t.percentile:.0f}%</td></tr>' for t in tests)
    verdict = ("At least one statistic falls outside chance — worth investigating."
               if any(t.significant for t in tests) else
               "Every statistic sits inside what chance produces. Giving back part of a peak is not "
               "evidence of tilt: a peak is by definition a maximum, so play after it regresses even "
               "with no change in how you play.")
    n = sum(s.hands for s in sess)
    thr = tests[0].threshold if tests else 0
    recent = sess[::-1][:40]
    sess_rows = "".join(
        f"<tr><td>{x.start:%Y-%m-%d %H:%M}</td><td>{x.hands}</td><td>{signed(x.final, ' bb')}</td>"
        f"<td>{x.peak:+.0f}</td><td>{x.given_back:.0f}</td><td>{100 * x.peak_position:.0f}%</td></tr>"
        for x in recent)
    body = f"""
    <div class="tiles">
      {_tile("sessions", str(len(sess)), "30-minute gap")}
      {_tile("median length", f"{lengths[len(lengths) // 2] if lengths else 0} hands", f"{sum(1 for s in sess if s.hands >= 50)} sessions of 50+")}
      {_tile("detectable shift", f"{detectable_shift(n):.0f} bb/100", "smallest change your volume could show")}
    </div>
    <div class="card"><h2>"I climb then give it back" — tested against your own hands reshuffled</h2>
      {'<table class="t"><tr><th>statistic</th><th>you</th><th>null median</th><th>null 90% range</th><th>percentile</th></tr>' + test_rows + '</table>' if tests else f'<div class="note">Not enough sessions peaking at +{peak:.0f}bb to test.</div>'}
      <div class="note" style="margin-top:.6rem">{verdict}</div>
      <div class="note" style="margin-top:.4rem">Null model: {n:,} of your own hands resampled, session lengths kept, order destroyed.
        Flagged only outside {thr:.1f}%–{100 - thr:.1f}%, Bonferroni-corrected for {len(tests)} statistics.</div>
    </div>
    <div class="card"><h2>Recent sessions</h2>
      <table class="t"><tr><th>start</th><th>hands</th><th>result</th><th>peak</th><th>given back</th><th>peak at</th></tr>{sess_rows}</table>
    </div>"""
    return page(body, "Sessions", "/sessions", f"{len(sess)} sessions reconstructed")


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------

def _lock_note(g) -> str:
    """A lock file left by a process that died says nothing is running; say so
    rather than leaving a reader to wonder why the last batch has no status."""
    import os
    path = g.store.root / ".grading.lock"
    if g.running or not path.exists():
        return ""
    try:
        pid = int(path.read_text().strip() or 0)
        os.kill(pid, 0)
        alive = True
    except (ValueError, ProcessLookupError, PermissionError):
        alive = False
    if alive:
        return (f'<div class="warn">Another process (pid {pid}) holds the grading lock — a CLI '
                f'batch is running; starting one here would be refused.</div>')
    return (f'<div class="note">A stale lock from pid {pid} sits in the verdict store; that '
            f'process is gone, so it will be ignored by the next batch.</div>')


def _progress_card(g) -> str:
    p = g.progress
    if p.status == "idle":
        return _lock_note(g)
    frac = (p.done + p.failed + p.skipped) / p.total if p.total else 0
    eta = p.eta_seconds
    refresh = '<meta http-equiv="refresh" content="5">' if g.running else ""
    stop = ('<form method="post" action="/grades/stop" style="display:inline">'
            '<button class="f">Stop after this solve</button></form>' if g.running else "")
    return f"""
    <div class="card">{refresh}
      <h2>{'Grading…' if g.running else 'Last run: ' + p.status}</h2>
      <div class="note">{html.escape(p.filter)}</div>
      <div class="progress"><i style="width:{100 * frac:.1f}%"></i></div>
      <div class="note">{p.done} graded · {p.failed} failed · {p.remaining} left
        {f' · {p.per_solve:.0f} s per decision' if p.per_solve else ''}
        {f' · about {eta / 60:.0f} min to go' if eta else ''}
        {f' · now {html.escape(p.current)}' if p.current else ''}</div>
      {f'<div class="warn" style="margin-top:.6rem">last error: {html.escape(p.error)}</div>' if p.error else ''}
      <div style="margin-top:.6rem">{stop}</div>
    </div>"""


def _bucket_table(title: str, buckets, label=lambda k: k) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(label(b.key))}</td><td>{b.graded}</td><td>{b.usable}</td>"
        f"<td>{'--' if b.mistake_rate is None else pct(b.mistake_rate)}</td>"
        f"<td>{'--' if b.loss_per_decision is None else f'{b.loss_per_decision:.2f}'}</td>"
        f"<td>{signed(-b.loss_bb, ' bb')}</td><td>{b.unstable}</td><td>{b.untrusted}</td></tr>"
        for b in buckets)
    return f"""
    <div class="card"><h2>{title}</h2>
      <table class="t"><tr><th></th><th>graded</th><th>usable</th><th>mistakes</th>
      <th>bb / decision</th><th>total EV</th><th>band split</th><th>untrusted</th></tr>{rows}</table>
    </div>"""


@router.get("/grades", response_class=HTMLResponse)
def grades_page(days: int = 0):
    g = state.grader()
    since, until = _window(days)
    recs = grades.in_window(state.verdicts(), since, until)
    tot = grades.totals(recs)

    tiles = ""
    tables = ""
    if tot.graded:
        per = tot.loss_per_100_hands
        tiles = f"""<div class="tiles">
          {_tile("graded decisions", f"{tot.graded:,}", f"{tot.hands:,} hands · {tot.solve_seconds / 3600:.1f} h of solving")}
          {_tile("usable", f"{tot.usable:,}", f"{tot.unstable} band split · {tot.untrusted} untrusted")}
          {_tile("EV given up", f"{tot.loss_bb:.0f} bb", f"{per:.1f} bb per 100 graded hands" if per is not None else "")}
        </div>"""
        worst = grades.biggest(recs, 15)
        worst_rows = "".join(
            f'<tr><td><a href="/replay/{r.hand_id}?step={r.idx}">{r.played_at[:16].replace("T", " ")}</a></td>'
            f"<td>{mini_cards([r.hero_combo[:2], r.hero_combo[2:]] if r.hero_combo else '')}</td>"
            f"<td>{mini_cards(r.board)}</td><td>{r.street.lower()}</td>"
            f"<td>{html.escape(NODE_LABEL.get(r.node, r.node))}</td><td>{html.escape(r.hero_step)}</td>"
            f"<td>{html.escape(grades.preferred(r.base, r.starting_pot)[0])}</td>"
            f"<td>{signed(-(r.loss_bb() or 0), ' bb')}</td></tr>" for r in worst)
        tables = (
            _bucket_table("By street", grades.by(recs, lambda r: r.street.lower()))
            + _bucket_table("By node", grades.by(recs, lambda r: r.node), lambda k: NODE_LABEL.get(k, k))
            + _bucket_table("By flop texture (flop decisions)",
                            grades.by([r for r in recs if r.street == "FLOP"], grades.texture_label))
            + _bucket_table("By your action", grades.by(recs, lambda r: r.hero_verb.lower()))
            + f"""<div class="card"><h2>Biggest mistakes</h2>
              <table class="t"><tr><th>hand</th><th>you</th><th>board</th><th>street</th><th>node</th>
              <th>you did</th><th>solver prefers</th><th>EV</th></tr>{worst_rows}</table></div>""")

    node_opts = "".join(f'<option value="{v}">{l}</option>' for v, l in NODES)
    body = f"""
    {_progress_card(g)}
    <div class="card"><h2>Grade a batch</h2>
      <form method="post" action="/grades/start" class="filter" style="gap:.8rem">
        <label>window {_select("days", [(str(d), l) for d, l in WINDOWS], "0")}</label>
        <label>street <select name="street"><option value="">all</option>
          <option value="FLOP">flop</option><option value="TURN">turn</option><option value="RIVER">river</option></select></label>
        <label>node <select name="node">{node_opts}</select></label>
        <label><input type="checkbox" name="first" value="1"> first decision of the street only</label>
        <label>reads <select name="widths"><option value="base">measured range only</option>
          <option value="tight+base+loose">full band (3×)</option></select></label>
        <label>limit <input type="number" name="limit" value="0" style="width:70px"></label>
        <button type="submit" class="primary" {"disabled" if g.running else ""}>Start</button>
      </form>
      <div class="note">After a session, <a href="/review">Review</a> has a one-click import-and-grade
        for the new hands.</div>
      <div class="note">One solve at a time, on all but four cores and at low priority so the machine stays
        usable; a flop node takes about a minute that way, turn and river seconds. Everything solved is kept under <code>data/verdicts/</code> and a stopped run resumes where it
        left off. For the solver c-bet baseline: flop · "SRP, you raised" · first decision only.</div>
    </div>
    {_window_select(days, "/grades") if tot.graded else ""}
    {tiles}{tables}
    {'' if tot.graded else '<div class="card"><div class="note">Nothing graded yet.</div></div>'}"""
    return page(body, "Grading", "/grades",
                f"solver verdicts on your decisions · {_scope(days)}")


@router.post("/grades/start")
def grades_start(days: str = Form("0"), street: str = Form(""), node: str = Form(""),
                 first: str = Form(""), widths: str = Form("base"), limit: str = Form("0")):
    g = state.grader()
    if not g.running:
        since, until = _window(int(days or 0))
        filt = GradeFilter(since=since, until=until,
                           streets=(street,) if street else STREETS,
                           nodes=(node,) if node else (), first_only=bool(first),
                           widths=tuple(widths.split("+")), limit=int(limit or 0))
        items = candidates(state.con(), filt, state.store())
        try:
            g.start(items, filt)
        except Exception as exc:                            # noqa: BLE001 - AlreadyRunning, shown on the page
            g.progress.status = "failed"
            g.progress.error = str(exc)
            g.progress.filter = filt.describe()
    return RedirectResponse("/grades", status_code=303)


@router.post("/grades/stop")
def grades_stop():
    state.grader().stop()
    return RedirectResponse("/grades", status_code=303)
