"""One villain's page: the note, the numbers behind it, what they showed
down, and every hand you played against them.

Server-rendered like the rest. The profile is computed on request (tens of
milliseconds for the biggest regular) and cached per (name, window) until
the next import.
"""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..stats.core import MIN_N
from ..stats import grades
from ..stats.villain_profile import POSITIONS, STREETS, Profile, profile
from . import state
from .dashboard import WINDOWS, _scope, _select, _tile, _window
from .layout import bar, mini_cards, page, signed, sparkline

router = APIRouter()

CSS = """
.vnote{font-size:1rem;line-height:1.5;white-space:pre-line}
.vnote.empty{color:var(--dim);font-style:italic}
.vmeta{font-size:.72rem;color:var(--dim);margin-top:.4rem}
textarea.note-edit{width:100%;min-height:64px;font:inherit;font-size:.9rem;padding:.5rem .6rem;
border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);resize:vertical}
.rate{white-space:nowrap}
.rate .n{color:var(--dim);font-size:.7rem;margin-left:.25rem}
.two{display:grid;grid-template-columns:1fr 1fr;gap:1rem;align-items:start}
@media(max-width:900px){.two{grid-template-columns:1fr}}
"""


def _rate(stat, width: int = 40) -> str:
    """A rate with its bar and its n; '--' with the n when under MIN_N."""
    n = f'<span class="n">n={stat.opportunities}</span>'
    if stat.pct is None:
        return f'<span class="rate">--{n}</span>'
    return f'<span class="rate">{bar(stat.pct / 100, width)}{stat.pct:.0f}%{n}</span>'


def _plain(stat) -> str:
    return "--" if stat.pct is None else f"{stat.pct:.0f}%"


def _profile(name: str, days: int) -> Profile:
    key = ("villain", name, days)
    with state._lock:
        got = state._state.get(key)
    if got is None:
        since, until = _window(days)
        got = profile(state.con(), name, since, until)
        with state._lock:
            state._state[key] = got
    return got


def _graded_against(name: str, p: Profile) -> tuple[int, int, float, int]:
    """(graded decisions vs them, mistakes, EV given up in bb, unstable)."""
    # The store keys villains by seat, not name: match through the seat table.
    seat_of = dict(state.con().execute(
        "SELECT hand_id, position FROM seats WHERE name = ? AND NOT is_hero", [name]).fetchall())
    n = mistakes = unstable = 0
    lost = 0.0
    for rec in state.verdicts():
        if seat_of.get(rec.hand_id) != rec.villain_pos or rec.villain_pos is None:
            continue
        n += 1
        if rec.stable is False:
            unstable += 1
        if grades._ok(rec):
            loss = rec.loss_bb() or 0.0
            lost += loss
            if rec.base and rec.base.approved is False:
                mistakes += 1
    return n, mistakes, round(lost, 1), unstable


def _hand_rows(rows, via: str, hero_first: bool = True) -> str:
    trs = []
    for r in rows:
        cls = "win" if r.hero_net_bb > 0 else "lose" if r.hero_net_bb < 0 else ""
        trs.append(
            f'<tr><td><a href="/replay/{r.hand_id}?via={via}">{r.played_at:%Y-%m-%d %H:%M}</a></td>'
            f'<td>{html.escape(r.hero_pos or "?")} v {html.escape(r.position or "?")}</td>'
            f'<td>{mini_cards(r.hero_cards)}</td><td>{mini_cards(r.board)}</td>'
            f'<td>{r.pot_bb:.1f}bb</td><td class="{cls}">{r.hero_net_bb:+.1f}bb</td>'
            f'<td><span class="note">{"showdown" if r.showdown else "contested" if r.contested else ""}</span></td></tr>')
    return (f'<table class="hands"><tr><th>played</th><th>seats</th><th>you</th><th>board</th>'
            f'<th>pot</th><th>you</th><th></th></tr>{"".join(trs)}</table>')


@router.get("/villains/{name}", response_class=HTMLResponse)
def villain_page(name: str, days: int = 0):
    p = _profile(name, days)
    via = quote(f"days={days}&villain={name}", safe="")
    if not p.hands:
        return page(f'<div class="card"><div class="verdict no">No hands with {html.escape(name)}'
                    f' in {_scope(days)}</div><div class="note"><a href="/villains">back to villains</a></div></div>',
                    html.escape(name), "/villains", "", extra_css=CSS)

    note = state.notes().get(name)
    note_html = (f'<div class="vnote">{html.escape(note.text)}</div>'
                 f'<div class="vmeta">written {note.written} on {note.hands:,} hands by {note.by}'
                 f'{" · " + str(p.hands - note.hands) + " hands since" if p.hands > note.hands else ""}'
                 f' · judgement from the stats below, not a measurement</div>'
                 if note else '<div class="vnote empty">No note yet. Two lines: what they do, what to do about it.</div>')
    form = f"""
    <details style="margin-top:.6rem"><summary class="note" style="cursor:pointer">edit note</summary>
      <form method="post" action="/villains/{quote(name, safe='')}/note" style="margin-top:.5rem">
        <input type="hidden" name="days" value="{days}">
        <textarea name="text" class="note-edit">{html.escape(note.text) if note else ""}</textarea>
        <div class="filter" style="margin-top:.4rem"><button type="submit">Save</button>
          <span>empty text removes the note</span></div>
      </form></details>"""

    n_graded, n_mistakes, ev_lost, n_unstable = _graded_against(name, p)
    hero_cont = (f"{p.hero_contested_bb:+.0f}bb over {p.contested} contested"
                 if p.contested else "no contested pots")
    tiles = "".join([
        _tile("hands shared", f"{p.hands:,}", f"{p.first_seen:%b %Y} – {p.last_seen:%b %Y}"),
        _tile("their result", signed(p.bb100, " bb/100", 0), f"{p.net_bb:+.0f}bb at your tables"),
        _tile("yours at their table", signed(p.hero_bb100, " bb/100", 0), hero_cont),
        _tile("VPIP / PFR", f"{_plain(p.vpip.stat)} / {_plain(p.pfr.stat)}", p.style or "style unmeasured"),
        _tile("3-bet", _plain(p.threebet.stat), f"n={p.threebet.stat.opportunities} facing an open"),
        _tile("fold to 3-bet", _plain(p.fold_to_3bet.stat),
              f"n={p.fold_to_3bet.stat.opportunities} · 4-bets {_plain(p.fourbet.stat)}"),
        _tile("limp", _plain(p.limp.stat), f"cold-calls opens {_plain(p.cold_call.stat)}"),
        _tile("open size", f"{p.open_size}bb" if p.open_size else "--",
              f"3-bets to {p.threebet_size}× the open" if p.threebet_size else "3-bet size unmeasured"),
        _tile("flop c-bet", _plain(p.streets["FLOP"].cbet.stat),
              f"n={p.streets['FLOP'].cbet.stat.opportunities} · folds to a flop bet {_plain(p.streets['FLOP'].fold_to_bet.stat)}"),
        _tile("goes to showdown", _plain(p.wtsd.stat),
              f"n={p.wtsd.stat.opportunities} flops · wins {_plain(p.wsd.stat)} there"),
        _tile("graded vs them", f"{n_graded}",
              f"{n_mistakes} mistake{'s' if n_mistakes != 1 else ''} · {-ev_lost:+.1f}bb" if n_graded else "nothing graded"),
    ])

    seat_rows = "".join(
        f'<tr><td class="l">{pos}</td><td>{s.dealt}</td><td>{_rate(s.rfi.stat)}</td>'
        f'<td>{_rate(s.vs_open_fold.stat)}</td><td>{_rate(s.vs_open_call.stat)}</td>'
        f'<td>{_rate(s.vs_open_3bet.stat)}</td></tr>'
        for pos, s in p.seats.items() if s.dealt)
    seats = f"""
    <div class="card"><h2>By position</h2>
      <table class="t"><tr><th class="l">seat</th><th>dealt</th><th>raises first in</th>
        <th>folds to an open</th><th>calls</th><th>3-bets</th></tr>{seat_rows}</table>
      <div class="note" style="margin-top:.5rem">"Raises first in" is their raise rate when
        nobody had entered; the three "vs open" columns are their first action facing exactly
        one raise. Rates under n={MIN_N} show as --.</div></div>"""

    street_rows = "".join(
        f'<tr><td class="l">{st.lower()}</td><td>{_rate(s.cbet.stat)}</td><td>{_rate(s.lead.stat)}</td>'
        f'<td>{_rate(s.fold_to_bet.stat)}</td><td>{_rate(s.raise_bet.stat)}</td>'
        f'<td>{_rate(s.check_raise.stat)}</td><td>{_rate(s.aggression)}</td></tr>'
        for st, s in p.streets.items())
    streets = f"""
    <div class="card"><h2>By street</h2>
      <table class="t"><tr><th class="l">street</th><th>c-bets</th><th>leads</th>
        <th>folds to a bet</th><th>raises a bet</th><th>check-raises</th><th>aggression</th></tr>{street_rows}</table>
      <div class="note" style="margin-top:.5rem">"C-bets" is bet-when-first-to-bet as the
        preflop aggressor, "leads" the same when they were not. "Aggression" is bets and raises
        over every action they took on the street. Multiway pots are included.</div></div>"""

    cats = " · ".join(f"{c} {n}" for c, n in p.shown_by_category)
    rb = p.river_bets_shown
    river_line = (f"Of {rb.opp} river bets or raises they showed, {rb.made} had no pair"
                  if rb.opp >= 10 else
                  f"Only {rb.opp} river bet{'s' if rb.opp != 1 else ''} shown — nothing to say about their bluffing")
    shown_rows = "".join(
        f'<tr><td><a href="/replay/{s.hand_id}?via={via}">{s.played_at:%Y-%m-%d}</a></td>'
        f'<td>{html.escape(s.position or "?")}</td><td>{mini_cards(s.cards)}</td>'
        f'<td>{mini_cards(s.board)}</td><td>{html.escape(s.category)}</td>'
        f'<td><span class="note">{html.escape(s.preflop)}</span></td>'
        f'<td><span class="note">{html.escape(s.river_verb.lower() or "—")}</span></td>'
        f'<td>{signed(s.net_bb, "bb")}</td><td>{mini_cards(s.hero_cards)}</td></tr>'
        for s in p.shown[:40])
    showdowns = f"""
    <div class="card"><h2>What they showed down — {len(p.shown)} hands</h2>
      <div class="dist">{html.escape(cats) or "nothing shown yet"}</div>
      <div class="note" style="margin-bottom:.6rem">{html.escape(river_line)}. Shown hands are a
        biased sample — the ones that reached showdown and were turned over.</div>
      {'<table class="hands"><tr><th>date</th><th>seat</th><th>held</th><th>board</th><th>made</th><th>preflop</th><th>river</th><th>their net</th><th>you had</th></tr>' + shown_rows + '</table>' if shown_rows else ''}
      {f'<div class="note" style="margin-top:.5rem">Showing the most recent 40 of {len(p.shown)}.</div>' if len(p.shown) > 40 else ''}
    </div>"""

    spark = sparkline([n for _, n in p.monthly], 220, 30)
    months = " · ".join(f"{m} {n}" for m, n in p.monthly)
    body = f"""
    <form method="get" action="/villains/{quote(name, safe='')}" class="filter">
      <label>window {_select("days", [(str(d), l) for d, l in WINDOWS], str(days))}</label>
      <button type="submit">Show</button>
      <a href="/hands?days={days}&villain={quote(name)}">all hands</a> ·
      <a href="/train/postflop?villain={quote(name)}">drill your graded spots vs them</a> ·
      <a href="/villains">back to villains</a>
    </form>
    <div class="card"><h2>Note</h2>{note_html}{form}</div>
    <div class="tiles">{tiles}</div>
    <div class="two">{seats}{streets}</div>
    {showdowns}
    <div class="two">
      <div class="card"><h2>Biggest pots between you</h2>{_hand_rows(p.biggest, via) if p.biggest else '<div class="note">none contested</div>'}</div>
      <div class="card"><h2>Most recent hands</h2>{_hand_rows(p.recent, via)}</div>
    </div>
    <div class="card"><h2>When you shared a table</h2>{spark}
      <div class="note">{html.escape(months)}</div></div>"""
    sub = (f"{p.hands:,} hands · {_scope(days)} · {p.style or 'style unmeasured'} · "
           f"last seen {p.last_seen:%Y-%m-%d}")
    return page(body, name, "/villains", sub, extra_css=CSS, wide=True)


@router.post("/villains/{name}/note")
def save_note(name: str, text: str = Form(""), days: int = Form(0)):
    p = _profile(name, 0)
    state.notes().set(name, text, p.hands, by="you")
    return RedirectResponse(f"/villains/{quote(name, safe='')}?days={days}", status_code=303)
