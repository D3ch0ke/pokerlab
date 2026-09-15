"""Hand replayer: browse your hands, step through one, solve the node you are on.

Server-rendered like the trainer, and for the same reason -- no build step and
nothing to keep in sync. The one piece of machinery here is the solve job: a
fresh flop node takes tens of seconds, which cannot be done inside a request,
so a solve runs on a thread and the page refreshes itself until it lands.
"""

from __future__ import annotations

import html
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..db.load import DEFAULT_DB
from ..ranges.chart import chart_for
from ..ranges.notation import canonical
from ..replay.evaluate import TOLERANCE_PCT_POT, Verdict, evaluate
from ..replay.hand import Frame, ReplayHand, load as load_hand
from ..replay.node import Decision, decisions
from ..replay.pool import build as build_pool, postflop_rates
from ..stats.core import NL5
from ..stats.ranges import observations
from . import state
from .app import _face, _grid
from .layout import page

router = APIRouter()
_jobs: dict[str, "Job"] = {}
_jobs_lock = threading.Lock()

SUIT_INK = {"c": "black", "s": "black", "d": "red", "h": "red"}

REPLAY_CSS = """
.felt{position:relative;background:var(--felt);border:1px solid var(--line);
border-radius:150px/110px;margin:.5rem 0 1rem;height:400px}
.middle{position:absolute;left:50%;top:44%;transform:translate(-50%,-50%);
text-align:center;width:60%}
.board{display:flex;gap:.35rem;justify-content:center;min-height:60px}
.board .pc{width:42px;height:60px}
.board .pc .pip{font-size:1.4rem} .board .pc .rk{font-size:.72rem}
.potline{margin-top:.5rem;font-size:.85rem;color:var(--dim);
font-variant-numeric:tabular-nums}
.pot{font-weight:700;color:var(--fg);font-size:1rem}
.seat{position:absolute;width:124px;text-align:center;font-size:.72rem;
transform:translate(-50%,-50%);line-height:1.3}
.seat .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.seat .st{color:var(--dim);font-variant-numeric:tabular-nums}
.seat .pos{font-size:.62rem;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
.seat.folded{opacity:.32}
.seat.acting .nm{color:var(--accent)}
.seat.acting{outline:2px solid var(--accent);outline-offset:6px;border-radius:8px}
.seat.hero .nm{color:var(--raise)}
.seat .cards{justify-content:center;margin-bottom:.2rem}
.seat .pc{width:27px;height:38px}
.seat .pc .pip{font-size:.9rem} .seat .pc .rk{font-size:.52rem} .seat .pc .lo{display:none}
.seat .back{width:27px;height:38px;border-radius:5px;background:var(--cardback);
border:1px solid var(--edge)}
.bet{margin-top:.25rem;font-size:.72rem;color:var(--fg);background:var(--chip);
border-radius:99px;padding:.05rem .45rem;display:inline-block;
font-variant-numeric:tabular-nums}
.log{font-size:.8rem;max-height:230px;overflow-y:auto;border:1px solid var(--line);
border-radius:8px}
.log a{display:flex;gap:.5rem;padding:.28rem .6rem;text-decoration:none;color:var(--fg);
border-bottom:1px solid var(--line)}
.log a:last-child{border-bottom:0}
.log a.now{background:var(--line);font-weight:600}
.log a.hero{color:var(--raise)}
.log .who{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log .amt{font-variant-numeric:tabular-nums;color:var(--dim)}
.log .hdr{background:var(--bg);color:var(--dim);font-size:.68rem;text-transform:uppercase;
letter-spacing:.06em;padding:.2rem .6rem;border-bottom:1px solid var(--line)}
.steps{display:flex;gap:.4rem;margin:.5rem 0 1rem;flex-wrap:wrap}
.steps a{padding:.4rem .75rem;border:1px solid var(--line);border-radius:7px;
text-decoration:none;color:var(--fg);font-size:.85rem;font-weight:600}
.steps a.off{opacity:.35;pointer-events:none}
.grid2{display:grid;grid-template-columns:1fr 300px;gap:1rem;align-items:start}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
table.hands{width:100%;border-collapse:collapse;font-size:.82rem}
table.hands th{text-align:left;font-weight:600;color:var(--dim);font-size:.7rem;
text-transform:uppercase;letter-spacing:.05em;padding:.4rem .5rem;
border-bottom:1px solid var(--line)}
table.hands td{padding:.35rem .5rem;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
table.hands tr:hover td{background:var(--line)}
table.hands a{text-decoration:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem}
.win{color:var(--raise)} .lose{color:var(--fold)}
.band{width:100%;border-collapse:collapse;font-size:.82rem;margin:.6rem 0}
.band th{text-align:right;font-weight:600;color:var(--dim);font-size:.68rem;
text-transform:uppercase;letter-spacing:.05em;padding:.3rem .45rem;
border-bottom:1px solid var(--line)}
.band th:first-child,.band td:first-child{text-align:left}
.band td{padding:.4rem .5rem;border-bottom:1px solid var(--line);text-align:right;
font-variant-numeric:tabular-nums;white-space:nowrap}
.band{max-width:640px}
.band td.v{font-weight:600}
.layers{font-size:.75rem;color:var(--dim);line-height:1.5;margin-top:.5rem}
.layers li{margin-bottom:.15rem}
.layers code{font-size:.72rem;background:var(--line);padding:.05rem .3rem;border-radius:3px}
.badge{display:inline-block;font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;
font-weight:700;padding:.1rem .45rem;border-radius:99px;border:1px solid currentColor}
.b-ok{color:var(--raise)} .b-no{color:var(--fold)} .b-mid{color:var(--call)}
.spin{color:var(--dim);font-size:.85rem}
"""





@dataclass
class Job:
    key: str
    status: str = "running"        # running | done | failed
    verdict: Verdict | None = None
    error: str = ""
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def elapsed(self) -> float:
        return (datetime.now(timezone.utc) - self.started).total_seconds()


def _job_key(hand_id: str, step: int) -> str:
    return f"{hand_id}:{step}"


def _start_solve(hand: ReplayHand, decision: Decision, step: int) -> Job:
    key = _job_key(hand.hand_id, step)
    with _jobs_lock:
        existing = _jobs.get(key)
        if existing and existing.status in ("running", "done"):
            return existing
        job = _jobs[key] = Job(key)

    def run() -> None:
        try:
            job.verdict = evaluate(state.pool(), state.observations(), hand, decision)
            job.status = "done"
        except Exception as exc:                      # noqa: BLE001 - surfaced verbatim
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "failed"
            traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return job


@dataclass
class WhatIf:
    """One step ahead: the villain's response after an action hero could have taken."""
    key: str
    action: str
    status: str = "running"
    mix: dict[str, float] = field(default_factory=dict)      # villain's range-weighted response
    strategy: dict[str, dict[str, float]] = field(default_factory=dict)
    villain_range: object = None
    error: str = ""
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def elapsed(self) -> float:
        return (datetime.now(timezone.utc) - self.started).total_seconds()


_whatifs: dict[str, WhatIf] = {}


def whatif_actions(rec, hero_oop: bool) -> list[str]:
    """Actions whose next node is the villain's, so a one-step look-ahead exists.

    A call closes the street, a fold ends the hand, and a check in position
    closes the street too — after those the next decision needs a card.
    """
    return [a for a in rec.base.ev
            if a.split()[0] in ("bet", "raise", "allin") or (a == "check" and hero_oop)]


def _start_whatif(hand: ReplayHand, decision: Decision, step: int, action: str, rec) -> WhatIf:
    key = f"{_job_key(hand.hand_id, step)}:{action}"
    with _jobs_lock:
        existing = _whatifs.get(key)
        if existing and existing.status in ("running", "done"):
            return existing
        job = _whatifs[key] = WhatIf(key, action)

    def run() -> None:
        from dataclasses import replace
        from ..replay.evaluate import MEMORY_BUDGET, build_spot, reconstruct
        from ..solver.bridge import solve
        from .grids import strategy_by_hand
        try:
            rc = reconstruct(state.pool(), state.observations(), hand, decision, "base",
                             rec.base.degraded, rec.base.digest, rec.base.villain_pct)
            spot = build_spot(rc.hero_range, rc.assignment.range, decision, rc.hero_is_oop,
                              action_path=tuple(decision.action_path) + (action,))
            spot = replace(rc.spot, action_path=spot.action_path, max_memory_bytes=MEMORY_BUDGET)
            sol = solve(spot)
            job.strategy = strategy_by_hand(sol, rc.assignment.range)
            job.villain_range = rc.assignment.range
            mass = 0.0
            for hand_code, mix in job.strategy.items():
                w = rc.assignment.range.freq(hand_code)
                from ..ranges.notation import combos
                w *= combos(hand_code)
                mass += w
                for a, p in mix.items():
                    job.mix[a] = job.mix.get(a, 0.0) + w * p
            job.mix = {a: v / mass for a, v in job.mix.items()} if mass else {}
            job.status = "done"
        except Exception as exc:                      # noqa: BLE001 - surfaced verbatim
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "failed"
            traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return job


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _page(body: str, title: str = "Replayer", sub: str = "") -> HTMLResponse:
    return page(body, title, "/hands", sub, extra_css=REPLAY_CSS, wide=True)


def _money(cents: int) -> str:
    return f"€{cents / 100:.2f}"


def bb_amount(cents: int, bb: int) -> str:
    """Chips in big blinds: 'raised to 0.4bb' reads, 'raised to €0.02' does not.

    The hand history and the solver both count cents; the player thinks in
    blinds. One decimal, trailing zero dropped, so 15 chips at NL5 is '3bb'.
    """
    x = cents / bb
    return f"{x:.1f}".rstrip("0").rstrip(".") + "bb"


def _bb(cents: int, bb: int) -> str:
    return f"{cents / bb:+.1f}bb"


def _action_name(name: str, bb: int) -> str:
    """Render a solver action in big blinds.

    The solver names nodes in raw chips ("allin 408"), which is the same number
    the hand history writes as €4.08. Showing its own units next to a table
    denominated in blinds makes the reader do the conversion.
    """
    verb, _, amount = name.partition(" ")
    if not amount:
        return verb
    money = bb_amount(int(float(amount)), bb)
    label = {"bet": f"bet {money}", "raise": f"raise to {money}",
             "allin": f"all-in {money}"}.get(verb)
    return label or f"{verb} {money}"


def _back() -> str:
    return '<div class="back"></div>'


def _seat_html(hand: ReplayHand, frame: Frame, seat, angle: float, revealed: bool) -> str:
    """One seat around the felt, placed on an ellipse by angle."""
    import math
    # Radii chosen so a seat box sits inside the felt rather than straddling
    # its rim; the vertical one is tighter because the boxes are tall.
    x = 50 + 39 * math.cos(angle)
    y = 50 + 34 * math.sin(angle)

    classes = ["seat"]
    if seat.name in frame.folded:
        classes.append("folded")
    if seat.name == frame.action.name:
        classes.append("acting")
    if seat.is_hero:
        classes.append("hero")

    if seat.is_hero and hand.hero_cards:
        cards = f'<div class="cards">{"".join(_face(c) for c in hand.hero_cards)}</div>'
    elif revealed and seat.shown:
        cards = f'<div class="cards">{"".join(_face(c) for c in seat.shown)}</div>'
    elif seat.name in frame.folded:
        cards = '<div class="cards"></div>'
    else:
        cards = f'<div class="cards">{_back()}{_back()}</div>'

    committed = frame.street_committed.get(seat.name, 0)
    bet = f'<div class="bet">{bb_amount(committed, hand.bb)}</div>' if committed else ""
    return (f'<div class="{" ".join(classes)}" style="left:{x:.1f}%;top:{y:.1f}%">'
            f'{cards}<div class="nm">{html.escape(seat.name)}</div>'
            f'<div class="pos">{html.escape(seat.position or "?")}</div>'
            f'<div class="st">{bb_amount(frame.stacks.get(seat.name, 0), hand.bb)}</div>{bet}</div>')


def _felt(hand: ReplayHand, frame: Frame, revealed: bool) -> str:
    import math
    seats = sorted(hand.seats, key=lambda s: s.seat_no)
    hero_i = next((i for i, s in enumerate(seats) if s.is_hero), 0)
    # Rotate so hero sits at the bottom, the way the table looked when played.
    ordered = seats[hero_i:] + seats[:hero_i]
    n = len(ordered)
    html_seats = "".join(
        _seat_html(hand, frame, s, math.pi / 2 + 2 * math.pi * i / n, revealed)
        for i, s in enumerate(ordered))

    board = "".join(_face(c) for c in frame.board) or '<span class="spin">pre-flop</span>'
    return f"""
    <div class="felt">
      {html_seats}
      <div class="middle">
        <div class="board">{board}</div>
        <div class="potline">pot <span class="pot">{bb_amount(frame.pot, hand.bb)}</span>
          &middot; {html.escape(frame.action.street.lower())}</div>
      </div>
    </div>"""


def _log(hand: ReplayHand, step: int) -> str:
    rows = []
    street = None
    for i, f in enumerate(hand.frames()):
        a = f.action
        if a.street != street:
            street = a.street
            board = " ".join(hand.board_at(street))
            rows.append(f'<div class="hdr">{html.escape(street.lower())}'
                        f'{" &middot; " + html.escape(board) if board else ""}</div>')
        amount = ""
        if a.verb in ("Bets", "Raises to", "Calls", "Posts SB", "Posts BB"):
            amount = bb_amount(a.effective or a.contributed, hand.bb)
        if a.effective != a.announced and a.announced:
            amount += f' <span class="spin">(of {bb_amount(a.announced, hand.bb)})</span>'
        cls = "now" if i == step else ""
        if a.is_hero:
            cls += " hero"
        rows.append(
            f'<a class="{cls.strip()}" href="/replay/{hand.hand_id}?step={i}">'
            f'<span class="who">{html.escape(a.name)}</span>'
            f'<span>{html.escape(a.verb.lower())}</span>'
            f'<span class="amt">{amount}</span></a>')
    return f'<div class="log">{"".join(rows)}</div>'


def _steps(hand: ReplayHand, step: int, total: int) -> str:
    def link(label: str, target: int, on: bool) -> str:
        cls = "" if on else " class=off"
        return f'<a{cls} href="/replay/{hand.hand_id}?step={max(0, min(total - 1, target))}">{label}</a>'
    ds = [d.idx for d in decisions(hand)]
    nxt = next((i for i in ds if i > step), None)
    prv = next((i for i in reversed(ds) if i < step), None)
    return (f'<div class="steps">{link("&#8676; start", 0, step > 0)}'
            f'{link("&#8592; back", step - 1, step > 0)}'
            f'{link("next &#8594;", step + 1, step < total - 1)}'
            f'{link("end &#8677;", total - 1, step < total - 1)}'
            f'{link("&#9679; prev decision", prv if prv is not None else step, prv is not None)}'
            f'{link("&#9679; next decision", nxt if nxt is not None else step, nxt is not None)}'
            f'</div>')


def _preflop_panel(hand: ReplayHand, decision: Decision) -> str:
    """Preflop nodes are graded against a chart, exactly as the trainer does."""
    from ..replay.node import _villain_preflop
    # The charts describe hero's FIRST preflop decision only. When hero acts
    # again on the street — opened and now facing a 3-bet, say — there is no
    # chart for that node, and grading the second verb against the opening
    # chart would report every fold to a re-raise as a deviation.
    first = next((a for a in hand.actions
                  if a.street == "PRE-FLOP" and a.is_hero
                  and not a.is_blind and not a.dead), None)
    if first is not None and decision.action.idx != first.idx:
        return (f'<div class="card"><div class="verdict">Not graded</div>'
                f'<div class="note">No chart covers a decision facing a re-raise. '
                f'Your first action this street ({html.escape(first.verb.lower())}) '
                f'is the one the chart speaks to; nothing is estimated for this one.'
                f'</div></div>')
    spot, verb, opener = _villain_preflop(hand, hand.hero)
    seat = hand.seat_of[hand.hero]
    key = f"{seat.position}_vs_{opener}" if spot == "vs_open" and opener else seat.position
    chart = chart_for(spot, key)
    if not chart:
        return (f'<div class="card"><div class="verdict">Not graded</div>'
                f'<div class="note">No chart covers {html.escape(str(key))} in '
                f'{html.escape(spot)}. Nothing here is estimated in its place.</div></div>')

    hand_code = canonical(list(hand.hero_cards))
    d = chart.prescribed(key, hand_code)
    # Checking the big blind is not folding, and scoring it as one would
    # report every free flop as a chart deviation.
    if decision.action.verb == "Checks":
        return (f'<div class="card"><div class="verdict">Not graded</div>'
                f'<div class="note">You checked your option. A raise-or-fold chart '
                f'does not prescribe anything here, so nothing is scored.</div></div>')
    took = {"Raises to": "raise", "Calls": "call", "Folds": "fold"}.get(
        decision.action.verb, "fold")
    freq = d.get(took, 0.0)
    if freq >= 0.5:
        head, cls = "Matches the chart", "b-ok"
    elif freq > 0:
        head, cls = f"Mixed — the chart takes this {freq:.0%} of the time", "b-mid"
    else:
        head, cls = "Not in the chart's range here", "b-no"
    dist = " · ".join(f"{a} {f:.0%}" for a, f in d.items() if f > 0)
    return f"""
    <div class="card">
      <div class="spot"><span class="badge {cls}">{html.escape(head)}</span></div>
      <div class="dist" style="margin-top:.6rem">{html.escape(hand_code)} from
        {html.escape(key)}: {html.escape(dist)} &middot; you
        {html.escape(decision.action.verb.lower())}</div>
      <div class="note">A postflop solver does not model preflop, so this node is graded
        against a reference chart rather than solved. The chart is judgement, not solver
        output — read a mismatch as worth investigating, not as a verdict.</div>
      {_grid(chart.id, key, hand_code)}
      <div class="src"><span class="conf">confidence:
        {html.escape(chart.confidence)}</span> &middot; <b>{html.escape(chart.id)}</b>
        &middot; assumption: {html.escape(chart.assumption)}</div>
    </div>"""


def _layers_html(read) -> str:
    items = "".join(f"<li>{html.escape(str(layer))}</li>" for layer in read.assignment.layers)
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in read.assignment.notes)
    return (f'<ul class="layers">{items}</ul>'
            f'<ul class="layers" style="margin-top:.4rem">{notes}</ul>')


def _verdict_panel(verdict: Verdict, hand: ReplayHand) -> str:
    d = verdict.decision
    usable = verdict.usable
    if not usable:
        errs = "".join(f"<li>{html.escape(r.error or '')}</li>" for r in verdict.reads)
        return (f'<div class="card"><div class="verdict no">Could not solve this node</div>'
                f'<ul class="layers">{errs}</ul></div>')

    cls = {"holds up": "b-ok", "loses EV": "b-no",
           "depends on the range": "b-mid"}.get(verdict.headline, "b-mid")

    rows = []
    for r in verdict.reads:
        if not r.ev:
            rows.append(f'<tr><td>{r.width}</td><td colspan="5">'
                        f'{html.escape(r.error or "no result")}</td></tr>')
            continue
        loss = verdict.loss_bb(r)
        ok = r.approved(d.starting_pot)
        # "Best" used to mean "within tolerance", which read as agreement when
        # the solver in fact preferred something else. Show the gap either way.
        if abs(loss) < 5e-3:
            mark = '<span class="win">best</span>'
        elif ok:
            mark = f'<span class="win">ok · −{abs(loss):.2f}bb</span>'
        else:
            mark = f'<span class="lose">−{abs(loss):.2f}bb</span>'
        rows.append(
            f'<tr><td class="v">{r.width}</td>'
            f'<td>{r.assignment.width_pct:.1f}%</td>'
            f'<td>{html.escape(_action_name(str(r.hero_action), hand.bb))}</td>'
            f'<td>{(r.hero_freq or 0):.0%}</td>'
            f'<td>{html.escape(_action_name(str(r.best), hand.bb))}</td>'
            f'<td>{mark}</td></tr>')

    base = next((r for r in verdict.reads if r.width == "base" and r.ev), usable[0])
    narrowed = any(l.name == "narrowing" for l in base.assignment.layers)
    stability = (
        ("The answer is the same against every range in the band, so the range "
         "assumption did not decide it."
         + (" The band stresses the postflop narrowing as well as the preflop "
            "width, since here the narrowing is doing most of the work."
            if narrowed else ""))
        if verdict.stable else
        "The answer FLIPS inside the band. This decision was never settled by the "
        "cards — it depends entirely on how wide you put your opponent, which is "
        "the part nobody knows.")

    warn = ""
    if verdict.warnings:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in verdict.warnings)
        warn = (f'<div class="note" style="margin-top:.6rem"><b>Read with care:</b>'
                f'<ul class="layers">{items}</ul></div>')

    ev_rows = "".join(
        f'<tr><td>{html.escape(_action_name(a, hand.bb))}</td>'
        f'<td>{base.freq.get(a, 0):.0%}</td>'
        f'<td>{base.ev[a] / hand.bb:+.2f}bb</td></tr>'
        for a in sorted(base.ev, key=base.ev.get, reverse=True))

    return f"""
    <div class="card">
      <div class="spot"><span class="badge {cls}">{html.escape(verdict.headline)}</span>
        <span class="tag">{html.escape(d.label_in(hand.bb))}</span>
        <span class="tag">vs {html.escape(str(d.villain))}</span></div>

      <table class="band">
        <tr><th>range</th><th>width</th><th>you</th><th>solver plays it</th>
            <th>solver's best</th><th>cost vs best</th></tr>
        {"".join(rows)}
      </table>
      <div class="note">{html.escape(stability)} A gap under
        {int(TOLERANCE_PCT_POT * 100)}% of the pot is marked <b>ok</b> rather than a
        mistake: below that the tree's own bet sizes and the solver's residual
        exploitability are larger than the difference being measured.</div>

      <div class="dist" style="margin-top:1rem"><b>At the measured width</b>, holding
        {html.escape(" ".join(hand.hero_cards))}:</div>
      <table class="band">
        <tr><th>action</th><th>frequency</th><th>EV</th></tr>{ev_rows}
      </table>
      <div class="note">EV is chips over folding, converted to big blinds. A frequency
        says what the solver does; the EV says what it costs not to.</div>

      <div class="dist" style="margin-top:1rem"><b>Your range here</b> —
        {html.escape(verdict.hero_range_note)}</div>
      <div class="dist"><b>{html.escape(base.assignment.label)}</b> —
        {base.assignment.width_pct:.1f}% of hands, confidence
        {html.escape(base.assignment.confidence)}</div>
      {_layers_html(base)}
      <div class="src"><b>What the band does not test:</b> it varies how WIDE the
        opponent is, not which hands fill that width. Two ranges of equal width but
        different composition can still disagree, and a verdict that only just holds
        is more fragile than the band alone makes it look.</div>
      {warn}
    </div>"""


def _stored_panel(rec, hand: ReplayHand, d: Decision, step: int) -> str:
    """A verdict the grader already wrote for this decision."""
    cls = {"holds up": "b-ok", "loses EV": "b-no",
           "depends on the range": "b-mid"}.get(rec.headline, "b-mid")
    rows = []
    for r in rec.reads:
        if not r.ev:
            rows.append(f'<tr><td>{r.width}</td><td colspan="5">{html.escape(r.error or "no result")}</td></tr>')
            continue
        from ..stats.grades import preferred
        best, clear = preferred(r, rec.starting_pot)
        loss = (r.ev_loss or 0) / hand.bb
        mix = " · ".join(f"{_action_name(a, hand.bb)} {100 * f:.0f}%"
                         for a, f in sorted(r.freq.items(), key=lambda kv: -kv[1]) if f >= 0.01)
        rows.append(
            f'<tr><td>{r.width}</td><td>{"--" if r.villain_pct is None else f"{r.villain_pct:.0f}%"}</td>'
            f'<td>{html.escape(_action_name(r.hero_action or "?", hand.bb))}</td>'
            f'<td>{html.escape(_action_name(best, hand.bb))}'
            f'{"" if clear else " <span class=note>(mixed)</span>"}</td>'
            f'<td class="v {"no" if r.approved is False else "ok"}">{-loss:+.1f}bb</td>'
            f'<td class="note" style="text-align:left">{html.escape(mix)}</td></tr>')
    warnings = "".join(f"<li>{html.escape(w)}</li>" for w in rec.warnings)
    stale = "" if rec.version == rec.__class__.__dataclass_fields__["version"].default else \
        '<div class="warn">Graded by an older model; solve again for the current one.</div>'
    warn_block = (f'<details class="note" style="margin-top:.4rem"><summary>{len(rec.warnings)} '
                  f'caveat{"s" if len(rec.warnings) != 1 else ""} on this solve</summary>'
                  f'<ul class="layers">{warnings}</ul></details>' if warnings else "")
    return f"""
    <div class="card">
      <div class="spot"><span class="badge {cls}">{html.escape(rec.headline)}</span>
        <span class="tag">{html.escape(d.label_in(hand.bb))}</span>
        <span class="tag">vs {html.escape(str(d.villain))}</span>
        <span class="tag">graded {rec.graded_at[:10]} · {rec.seconds:.0f}s · {html.escape(rec.model)}</span></div>
      {stale}
      <table class="band"><tr><th>villain</th><th>width</th><th>you</th><th>solver plays</th>
        <th>EV</th><th>solver mix with your hand</th></tr>{"".join(rows)}</table>
      <div class="note">"Solver plays" is its most frequent action with your hand; where two actions
        tie in EV the solver mixes them and which one edges ahead is noise, so a tie is marked
        (mixed). It names a different action only when that one is clearly better — more than
        {int(TOLERANCE_PCT_POT * 100)}% of the pot — and the EV column then shows what your
        action gave up against it.</div>
      {warn_block}
      {_stored_grids(rec, hand, d)}
      {_whatif_html(rec, hand, d, step)}
      <form method="post" action="/replay/{hand.hand_id}/solve" class="btns" style="margin-top:.6rem">
        <input type="hidden" name="step" value="{step}">
        <button>Re-solve live against the full range band</button>
      </form>
    </div>"""


def _stored_grids(rec, hand: ReplayHand, d: Decision) -> str:
    """Both ranges at the node, hero's painted with the solver's mix when the
    solve is still on disk. Rebuilt from the pool, not stored with the verdict."""
    from ..replay.evaluate import reconstruct
    from .grids import legend, mix_text, range_grid, strategy_by_hand
    if not rec.base:
        return ""
    try:
        rc = reconstruct(state.pool(), state.observations(), hand, d, "base",
                         rec.base.degraded, rec.base.digest, rec.base.villain_pct)
    except Exception as exc:                                # noqa: BLE001 - a page must not die on a range
        return f'<div class="note">Ranges unavailable: {html.escape(str(exc))}</div>'
    hero_hand = canonical(list(hand.hero_cards))
    strat = strategy_by_hand(rc.solution, rc.hero_range) if rc.solution else None
    lab = lambda a: _action_name(a, hand.bb)
    rf = rec.base.range_freq
    layers = "".join(f"<li>{html.escape(str(l))}</li>" for l in rc.assignment.layers)
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in rc.assignment.notes)
    return f"""
    <details style="margin-top:.6rem" open><summary class="note" style="cursor:pointer">ranges at this node</summary>
    <div class="grids" style="margin-top:.6rem">
      <div><div class="gt">Your range — {rc.hero_range.pct:.0f}% of hands, {html.escape(hero_hand)} outlined</div>
        {range_grid(rc.hero_range, strat, hero_hand, lab, compact=True)}{legend(bool(strat))}</div>
      <div><div class="gt">{html.escape(rc.assignment.label)} — {rc.assignment.range.pct:.0f}% of hands</div>
        {range_grid(rc.assignment.range, None, None, lab, compact=True)}{legend(False)}</div>
    </div>
    {f'<div class="dist">Over your whole range the solver {html.escape(mix_text(rf, lab))}.</div>' if rf else ''}
    {'' if rc.solution else '<div class="note">The solve behind this verdict is not on disk any more; cells show membership only.</div>'}
    <div class="note">{html.escape(rc.hero_note)}.</div>
    <details class="note"><summary>how the villain range was built</summary>
      <ul class="layers">{layers}</ul><ul class="layers">{notes}</ul></details>
    </details>"""


def _whatif_html(rec, hand: ReplayHand, d: Decision, step: int) -> str:
    """One step ahead: pick an action, see how the villain's range answers it."""
    from .grids import legend, mix_text, range_grid
    if not rec.base:
        return ""
    options = whatif_actions(rec, rec.hero_oop)
    if not options:
        return ""
    lab = lambda a: _action_name(a, hand.bb)
    buttons = "".join(
        f'<button name="action" value="{html.escape(a)}" class="{"r" if a != "check" else "c"}">'
        f'{html.escape(lab(a))}</button>' for a in options)
    panels = []
    for a in options:
        job = _whatifs.get(f"{_job_key(hand.hand_id, step)}:{a}")
        if job is None:
            continue
        if job.status == "running":
            panels.append(f'<div class="card"><meta http-equiv="refresh" content="3">'
                          f'<div class="verdict">After {html.escape(lab(a))}: solving… {job.elapsed:.0f}s</div>'
                          f'<div class="spin">Turn and river answer in seconds; a flop node is a fresh '
                          f'solve of up to a minute. This page refreshes itself.</div></div>')
        elif job.status == "failed":
            panels.append(f'<div class="card"><div class="verdict no">After {html.escape(lab(a))}: '
                          f'failed</div><div class="note mono">{html.escape(job.error)}</div></div>')
        else:
            panels.append(f"""<div class="card">
              <div class="verdict">After {html.escape(lab(a))}, the villain {html.escape(mix_text(job.mix, lab))}</div>
              <div class="grids"><div><div class="gt">{html.escape(rec.base.villain_label)} — response by hand</div>
                {range_grid(job.villain_range, job.strategy, None, lab, compact=True)}{legend(True)}</div></div>
              <div class="note">Range-weighted over the measured villain range; the same tree as the
                verdict, read one action deeper.</div></div>""")
    return f"""
    <div style="margin-top:.8rem"><div class="dist"><b>What if</b> — see the villain's answer to:</div>
      <form method="post" action="/replay/{hand.hand_id}/whatif" class="btns" style="margin-top:.4rem">
        <input type="hidden" name="step" value="{step}">{buttons}
      </form>
      {"".join(panels)}
    </div>"""


def _analysis(hand: ReplayHand, step: int) -> str:
    ds = {d.idx: d for d in decisions(hand)}
    d = ds.get(step)
    if d is None:
        return ('<div class="card"><div class="note">Not one of your decisions — '
                'step to a highlighted action to analyse it.</div></div>')
    if d.street == "PRE-FLOP":
        return _preflop_panel(hand, d)
    if not d.solvable:
        return (f'<div class="card"><div class="verdict">Not solvable</div>'
                f'<div class="note">{html.escape(d.reason)}.</div></div>')

    job = _jobs.get(_job_key(hand.hand_id, step))
    if job is None:
        stored = state.store().get(hand.hand_id).get(step)
        if stored is not None:
            return _stored_panel(stored, hand, d, step)
        return f"""
        <div class="card">
          <div class="spot"><span class="tag">{html.escape(d.label_in(hand.bb))}</span>
            <span class="tag">vs {html.escape(str(d.villain))}</span>
            <span class="tag">{d.effective_stack / hand.bb:.0f}bb behind</span></div>
          <form method="post" action="/replay/{hand.hand_id}/solve" class="btns">
            <input type="hidden" name="step" value="{step}">
            <button class="r">Solve this node against the range band</button>
          </form>
          <div class="note" style="margin-top:.8rem">Three solves: a tight, a measured
            and a loose opponent. A fresh flop node takes up to a minute; the result is
            cached afterwards.</div>
        </div>"""
    if job.status == "running":
        return f"""
        <div class="card">
          <meta http-equiv="refresh" content="3">
          <div class="verdict">Solving…</div>
          <div class="spin">{job.elapsed:.0f}s elapsed · three solves, tight/base/loose.
            This page refreshes itself.</div>
        </div>"""
    if job.status == "failed":
        return (f'<div class="card"><div class="verdict no">Solve failed</div>'
                f'<div class="note mono">{html.escape(job.error)}</div></div>')
    return _verdict_panel(job.verdict, hand)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@router.get("/replay")
def browse():
    """The browser moved to /hands, which knows about verdicts."""
    return RedirectResponse("/hands", status_code=307)


def _review_bar(hand: ReplayHand, step: int, list_key: str) -> str:
    """Prev / next inside a review list, the verdict in one line, and the mark."""
    from .review import neighbours, session_by_key
    rec = state.store().get(hand.hand_id).get(step)
    marks = state.marks()
    key = marks.key(hand.hand_id, step)
    nav = ""
    if list_key:
        sess = session_by_key(list_key)
        if sess is not None:
            prev, nxt, pos, total = neighbours(sess, hand.hand_id, step)
            link = lambda r, label: (f'<a href="/replay/{r.hand_id}?step={r.idx}&list={html.escape(list_key)}">{label}</a>'
                                     if r else f'<span class="note">{label}</span>')
            nav = (f'{link(prev, "&#8592; previous")} <span class="note">{pos} of {total} in this '
                   f'session</span> {link(nxt, "next &#8594;")} · <a href="/review">back to review</a>')
    line = ""
    if rec is not None and rec.base and rec.base.ev:
        from ..stats.grades import preferred
        b = rec.base
        best, clear = preferred(b, rec.starting_pot)
        loss = rec.loss_bb() or 0.0
        cls = {"holds up": "b-ok", "loses EV": "b-no"}.get(rec.headline, "b-mid")
        line = (f'<span class="badge {cls}">{html.escape(rec.headline)}</span> you '
                f'<b>{html.escape(_action_name(rec.hero_step, hand.bb))}</b>, solver '
                f'{"prefers" if clear else "mostly"} <b>{html.escape(_action_name(best, hand.bb))}</b>'
                f'{f" · <span class=neg>−{loss:.1f}bb</span>" if loss > 0.05 else ""}')
    mark = ""
    if rec is not None:
        back = f"/replay/{hand.hand_id}?step={step}&list={list_key}"
        mark = (f'<form method="post" action="/review/mark" style="display:inline">'
                f'<input type="hidden" name="key" value="{html.escape(key)}">'
                f'<input type="hidden" name="back" value="{html.escape(back)}">'
                f'<button class="{"" if key in marks else "c"}" style="padding:.15rem .6rem;font-size:.72rem">'
                f'{"reviewed ✓ (undo)" if key in marks else "mark reviewed"}</button></form>')
    if not (nav or line or mark):
        return ""
    return (f'<div class="card" style="padding:.6rem 1rem;display:flex;gap:1rem;align-items:center;'
            f'flex-wrap:wrap;font-size:.85rem"><span>{nav}</span><span style="flex:1">{line}</span>{mark}</div>')


@router.get("/replay/{hand_id}", response_class=HTMLResponse)
def replay(hand_id: str, step: int = 0, list: str = ""):
    try:
        hand = load_hand(state.con(), hand_id)
    except KeyError:
        return _page('<div class="card"><div class="verdict no">No such hand</div></div>')

    frames = hand.frames()
    step = max(0, min(len(frames) - 1, step))
    frame = frames[step]
    # Villain cards stay face down until the hand is over: seeing them earlier
    # would make every replayed decision a hindsight exercise.
    revealed = step >= len(frames) - 1

    seats = hand.seat_of
    hero_seat = seats.get(hand.hero)
    won = "won" if hand.hero_net > 0 else "lost"
    sub = (f"{hand.played_at:%Y-%m-%d %H:%M} · {hand.hand_id} · "
           f"hero {html.escape(hero_seat.position or '?')} · "
           f"{won} {abs(hand.hero_net) / hand.bb:.1f}bb")

    body = f"""
    {_review_bar(hand, step, list)}
    <div class="grid2">
      <div>
        {_felt(hand, frame, revealed)}
        {_steps(hand, step, len(frames))}
      </div>
      <div>{_log(hand, step)}
        <div class="note" style="margin-top:.6rem">Your decisions are green. Villain
          cards stay face down until the last step — the point is to judge the decision
          with what you knew then.</div>
      </div>
    </div>
    {_analysis(hand, step)}"""
    return _page(body, "Replayer", sub)


@router.post("/replay/{hand_id}/solve")
def start_solve(hand_id: str, step: int = Form(...)):
    hand = load_hand(state.con(), hand_id)
    d = {x.idx: x for x in decisions(hand)}.get(step)
    if d and d.solvable:
        _start_solve(hand, d, step)
    return RedirectResponse(f"/replay/{hand_id}?step={step}", status_code=303)


@router.post("/replay/{hand_id}/whatif")
def start_whatif(hand_id: str, step: int = Form(...), action: str = Form(...)):
    hand = load_hand(state.con(), hand_id)
    d = {x.idx: x for x in decisions(hand)}.get(step)
    rec = state.store().get(hand_id).get(step)
    if d and d.solvable and rec and rec.base and action in whatif_actions(rec, rec.hero_oop):
        _start_whatif(hand, d, step, action, rec)
    return RedirectResponse(f"/replay/{hand_id}?step={step}", status_code=303)


def attach(app, db: Path = DEFAULT_DB) -> None:
    state.configure(db)
    app.include_router(router)
