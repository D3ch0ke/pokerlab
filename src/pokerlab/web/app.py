"""Local preflop trainer. Server-rendered, no build step, no JS framework.

The grid and the answer buttons are both driven off the chart's declared
`actions`, not off a hardcoded raise/fold pair, because a chart that prescribes
calls and is drilled as raise-or-fold teaches the opposite of what it says.
"""

from __future__ import annotations

import html
from pathlib import Path

import duckdb
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

from ..db.load import DEFAULT_DB
from ..ranges.chart import available, load
from ..ranges.notation import all_hands, deal_combos, grid_index
from ..stats.ranges import observations
from ..trainer.progress import DEFAULT_PATH as PROGRESS_PATH, Progress
from ..trainer.quiz import Filter, NoSpots, Quiz, Spot
from .layout import page

app = FastAPI(title="pokerlab")
_state: dict = {}

#: Chart `spot` values, in the order they happen in a hand.
SCENARIOS = {"RFI": "RFI", "vs_open": "vs open", "vs_3bet": "vs 3-bet", "vs_limp": "vs limp"}
ACTION_LABEL = {"raise": "Raise", "call": "Call", "fold": "Fold"}
ACTION_CLASS = {"raise": "r", "call": "c", "fold": "f"}
SUIT_PIP = {"c": "&clubs;", "d": "&diams;", "h": "&hearts;", "s": "&spades;"}
SUIT_INK = {"c": "black", "s": "black", "d": "red", "h": "red"}


def _quiz() -> Quiz:
    """Built on the shared observations, so a re-import rebuilds it too."""
    from . import state
    obs = state.observations()
    if _state.get("quiz_obs") is not obs:
        _state["quiz"] = Quiz(list(available()), obs, progress=Progress.load(PROGRESS_PATH))
        _state["quiz_obs"] = obs
    return _state["quiz"]


TRAINER_CSS = """
.wrap{max-width:760px}
.btns button{flex:1;padding:.8rem;font-size:.95rem}
"""


def _face(card: str) -> str:
    """One rendered playing card. `card` is like 'Ah'."""
    rank, suit = card[0], card[1]
    shown = "10" if rank == "T" else rank
    pip = SUIT_PIP[suit]
    return (f'<div class="pc {SUIT_INK[suit]}">'
            f'<span class="rk">{shown}{pip}</span>'
            f'<span class="pip">{pip}</span>'
            f'<span class="lo">{shown}{pip}</span></div>')


def _hand_cards(hand: str) -> str:
    """Two cards whose suits agree with the notation, chosen deterministically.

    Deterministic because the question and the verdict render the same hand
    twice; drawing at random would deal a different pair each time and read as
    a flicker. deal_combos already guarantees suited/offsuit/pair consistency,
    so there is no suit logic here to get wrong.
    """
    dealt = deal_combos(hand)
    pick = dealt[sum(ord(ch) for ch in hand) % len(dealt)]
    return f'<div class="cards">{_face(pick[0])}{_face(pick[1])}</div>'


def _grid(chart_id: str, position: str, highlight: str | None = None) -> str:
    """The 13x13 grid, painted with the chart's full action mix.

    Each cell is a horizontal band: raise, then call, then the fold remainder.
    Binarising at 0.5 (what this used to do) hid every mixed cell and every
    call range entirely.
    """
    chart = load(chart_id)
    cells: dict[tuple[int, int], str] = {}
    for hand in all_hands():
        d = chart.prescribed(position, hand)
        r, c = d.get("raise", 0.0), d.get("call", 0.0)
        a, b = round(100 * r), round(100 * (r + c))
        style = (f"background:linear-gradient(90deg,var(--raise) 0 {a}%,"
                 f"var(--call) {a}% {b}%,var(--foldbg) {b}% 100%)")
        cls = "lit" if r + c >= 0.5 else ""
        if hand == highlight:
            cls = (cls + " here").strip()
        cells[grid_index(hand)] = (
            f'<td class="{cls}" style="{style}" title="{html.escape(hand)}: '
            f'{html.escape(_dist_text(d))}">{html.escape(hand)}</td>')
    rows = "".join(
        "<tr>" + "".join(cells[(r, c)] for c in range(13)) + "</tr>" for r in range(13)
    )
    legend = ('<div class="legend">'
              '<span><i class="sw" style="background:var(--raise)"></i>raise</span>'
              '<span><i class="sw" style="background:var(--call)"></i>call</span>'
              '<span><i class="sw" style="background:var(--foldbg)"></i>fold</span>'
              '<span>a split cell is a mixed frequency</span></div>')
    return f'<table class="grid">{rows}</table>{legend}'


def _dist_text(d: dict[str, float]) -> str:
    return " · ".join(f"{a} {f:.0%}" for a, f in d.items() if f > 0)


def _provenance(chart) -> str:
    return (f'<div class="src"><span class="conf">confidence: '
            f'{html.escape(chart.confidence)}</span> &middot; '
            f'<b>{html.escape(chart.id)}</b> &middot; '
            f'assumption: {html.escape(chart.assumption)} v{chart.version}'
            f'<br>{html.escape(chart.source)}</div>')


def _tabs() -> str:
    from .drill import _tabs as tabs
    return tabs("preflop")


def _page(body: str, title: str = "Preflop drill", active: str = "/train",
          sub: str = "spots weighted toward where your own hands deviate") -> HTMLResponse:
    return page(body, title, active, sub, extra_css=TRAINER_CSS)


def _options(values: list[str], selected: str | None, all_label: str) -> str:
    out = [f'<option value=""{" selected" if not selected else ""}>{all_label}</option>']
    for v in values:
        sel = " selected" if v == selected else ""
        out.append(f'<option value="{html.escape(v)}"{sel}>{html.escape(v)}</option>')
    return "".join(out)


def _filter_form(quiz: Quiz, filt: Filter) -> str:
    scen = quiz.scenarios()
    # Positions are restricted to the chosen scenario; "all" shows every one.
    positions = sorted(scen.get(filt.spot, [])) if filt.spot else sorted(
        {p for ps in scen.values() for p in ps})
    labels = "".join(
        f'<option value="{html.escape(s)}"{" selected" if s == filt.spot else ""}>'
        f'{html.escape(SCENARIOS.get(s, s))}</option>' for s in scen)
    p = quiz.progress.stats
    modes = "".join(
        f'<option value="{v}"{" selected" if v == filt.mode else ""}>{l}</option>'
        for v, l in (("", "leak-weighted"), ("due", f"due for review ({p['due_now']})"),
                     ("struggling", f"spots you keep missing ({p['struggling']})")))
    return f"""
    <form method="get" action="/train" class="filter">
      <label>scenario</label>
      <select name="spot" onchange="this.form.submit()">
        <option value=""{" selected" if not filt.spot else ""}>all</option>{labels}
      </select>
      <label>position</label>
      <select name="pos">{_options(positions, filt.position, "all")}</select>
      <label>draw</label>
      <select name="mode">{modes}</select>
      <button type="submit">Apply</button>
    </form>
    """


def _hidden_filter(filt: Filter) -> str:
    return (f'<input type="hidden" name="spot" value="{html.escape(filt.spot or "")}">'
            f'<input type="hidden" name="pos" value="{html.escape(filt.position or "")}">'
            f'<input type="hidden" name="mode" value="{html.escape(filt.mode)}">')


def _question(spot: Spot, quiz: Quiz, filt: Filter) -> str:
    s = quiz.session
    p = quiz.progress.stats
    chart = load(spot.chart_id)
    buttons = "".join(
        f'<button class="{ACTION_CLASS.get(a, "")}" name="action" value="{html.escape(a)}" '
        f'data-key="{a[0]}">{ACTION_LABEL.get(a, a.title())}</button>' for a in chart.actions)
    return f"""
    <div class="card">
      <div class="spot">
        {_hand_cards(spot.hand)}
        <div>
          <div class="tag">{html.escape(spot.position)}</div>
          <div class="tag">{html.escape(spot.label)}</div>
        </div>
      </div>
      <form method="post" action="/train/answer" class="btns" id="q">
        <input type="hidden" name="chart_id" value="{html.escape(spot.chart_id)}">
        <input type="hidden" name="position" value="{html.escape(spot.position)}">
        <input type="hidden" name="hand" value="{html.escape(spot.hand)}">
        {_hidden_filter(filt)}
        {buttons}
      </form>
    </div>
    <div class="score">{s.right}/{s.asked} this session
      {f"&middot; {s.accuracy:.0%}" if s.asked else ""}
      &middot; {p['answered']:,} all-time ({p['accuracy']:.0%})
      &middot; {p['struggling']} spots you keep missing &middot; keys: r c f</div>
    <script>
    document.addEventListener('keydown',function(e){{
      if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'||e.metaKey||e.ctrlKey)return;
      var b=document.querySelector('#q button[data-key="'+e.key.toLowerCase()+'"]');
      if(b){{e.preventDefault();b.click();}}
    }});
    </script>
    """


def _curve_html(quiz: Quiz, chart, position: str) -> str:
    """Facing an open from the blinds: the whole defence curve, this seat marked.

    The drill's answer is one cell; the leak is the shape across cells, and
    the shape is what a chart's imprecision cannot fake.
    """
    from ..trainer.leakweight import curve_is_flat, defence_curve
    if chart.spot != "vs_open" or "_vs_" not in position:
        return ""
    hero_pos, opener = position.split("_vs_", 1)
    cells = defence_curve(quiz.obs, chart, hero_pos)
    if len(cells) < 3:
        return ""
    rows = "".join(
        f'<tr{" style=font-weight:600" if c.opener == opener else ""}><td>{c.opener}</td>'
        f"<td>{c.n}</td><td>{c.observed:.0%}</td><td>{c.prescribed:.0%}</td>"
        f'<td class="{"neg" if abs(c.gap) >= 0.1 else ""}">{100 * c.gap:+.0f}</td></tr>'
        for c in cells)
    flat = ("Your curve is flat: it should widen from UTG to SB, whatever the chart's levels."
            if curve_is_flat(cells) else "Your curve does slope; the question is by how much.")
    return f"""
    <div class="dist" style="margin-top:.8rem"><b>{html.escape(hero_pos)} defence by the opener's seat</b></div>
    <table class="t" style="max-width:420px"><tr><th>opener</th><th>n</th><th>you</th><th>chart</th><th>gap</th></tr>{rows}</table>
    <div class="note">{flat}</div>"""


def _filter_from(spot: str | None, pos: str | None, mode: str | None = None) -> Filter:
    """Query params win; absent params keep whatever was last applied."""
    if spot is None and pos is None and mode is None:
        return _state.get("filter", Filter())
    # A position left over from another scenario is NOT quietly dropped: the
    # combination is empty, and the empty page says so rather than widening
    # the filter behind the user's back.
    filt = Filter(spot or None, pos or None, mode or "")
    _state["filter"] = filt
    return filt


def _empty(quiz: Quiz, filt: Filter, why: str) -> str:
    return f"""
    {_filter_form(quiz, filt)}
    <div class="card">
      <div class="verdict no">No spots match that filter</div>
      <div class="note">{html.escape(why)}. Widen the scenario or the position;
        nothing is being sampled behind your back.</div>
    </div>
    """


@app.get("/train", response_class=HTMLResponse)
def index(spot: str | None = None, pos: str | None = None, mode: str | None = None):
    quiz = _quiz()
    filt = _filter_from(spot, pos, mode)
    try:
        question = quiz.next_spot(filt)
    except NoSpots as exc:
        return _page(_empty(quiz, filt, str(exc)))
    _state["spot"] = question
    return _page(_tabs() + _filter_form(quiz, filt) + _question(question, quiz, filt))


@app.post("/train/answer", response_class=HTMLResponse)
def answer(chart_id: str = Form(...), position: str = Form(...),
           hand: str = Form(...), action: str = Form(...),
           spot: str = Form(""), pos: str = Form(""), mode: str = Form("")):
    quiz = _quiz()
    chart = load(chart_id)
    filt = _filter_from(spot, pos, mode)
    asked = Spot(chart_id, chart.label, position, hand, chart.spot)
    v = quiz.grade(asked, action)

    if v.mixed and v.correct:
        head = (f'<span class="mix">Mixed — the chart plays '
                f'{html.escape(" or ".join(v.accepted))} here</span>')
    elif v.correct:
        head = '<span class="ok">Correct</span>'
    else:
        head = (f'<span class="no">Not per the chart — it '
                f'{html.escape(v.best)}s this hand</span>')

    quiz.progress.save(PROGRESS_PATH)  # persist every answer; drills get interrupted
    body = f"""
    <div class="card">
      <div class="note" style="margin-bottom:.6rem">previous hand</div>
      <div class="spot">{_hand_cards(hand)}
        <div><div class="tag">{html.escape(position)}</div>
             <div class="tag">{html.escape(chart.label)}</div></div></div>
      <div class="verdict" style="margin-top:1rem">{head}</div>
      <div class="dist">chart here: {html.escape(v.summary)}
        &middot; you answered {html.escape(action)} ({v.chart_freq:.0%})</div>
      <div class="note">{html.escape(v.note)}</div>
      {_curve_html(quiz, chart, position)}
      {_grid(chart_id, position, hand)}
      {_provenance(chart)}
    </div>
    """
    # The next hand goes on top: the previous answer is there to glance back at,
    # not to scroll past before every question.
    try:
        nxt = quiz.next_spot(filt)
    except NoSpots as exc:
        return _page(_tabs() + _empty(quiz, filt, str(exc)) + body)
    _state["spot"] = nxt
    return _page(_tabs() + _filter_form(quiz, filt) + _question(nxt, quiz, filt) + body)


@app.get("/ranges", response_class=HTMLResponse)
def ranges(chart: str | None = None, position: str | None = None):
    """Browse a chart's grid without answering anything."""
    charts = list(available())
    by_id = {c.id: c for c in charts}
    picked = by_id.get(chart or "", charts[0])
    if position not in picked.positions:
        position = picked.positions[0]

    chart_opts = "".join(
        f'<option value="{html.escape(c.id)}"{" selected" if c.id == picked.id else ""}>'
        f'{html.escape(c.label)}</option>' for c in charts)
    pos_opts = "".join(
        f'<option value="{html.escape(p)}"{" selected" if p == position else ""}>'
        f'{html.escape(p)}</option>' for p in picked.positions)
    rng = picked.action_range(position, "raise")
    call = picked.action_range(position, "call")
    body = f"""
    <form method="get" action="/ranges" class="filter">
      <label>chart</label>
      <select name="chart" onchange="this.form.submit()">{chart_opts}</select>
      <label>position</label>
      <select name="position" onchange="this.form.submit()">{pos_opts}</select>
      <button type="submit">Show</button>
    </form>
    <div class="card">
      <div class="spot"><div class="tag">{html.escape(position)}</div>
        <div class="tag">{html.escape(picked.spot)}</div></div>
      <div class="dist">raise {rng.pct}% of combos
        &middot; call {call.pct}% &middot; total {round(rng.pct + call.pct, 1)}%</div>
      {_grid(picked.id, position)}
      {_provenance(picked)}
    </div>
    """
    return _page(body, "Range viewer", "/ranges", "every chart carries its confidence and source")


def build_app(db: Path = DEFAULT_DB):
    from . import state
    from .dashboard import router as dashboard_router
    from .drill import router as drill_router
    from .replay import router as replay_router
    from .review import router as review_router
    from .leaks import router as leaks_router
    state.configure(db)
    _state["db"] = db
    if not _state.get("attached"):
        _state["attached"] = True
        app.include_router(dashboard_router)
        app.include_router(replay_router)
        app.include_router(drill_router)
        app.include_router(review_router)
        app.include_router(leaks_router)
    return app


def serve(db: Path = DEFAULT_DB, port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(build_app(db), host="127.0.0.1", port=port, log_level="warning")
