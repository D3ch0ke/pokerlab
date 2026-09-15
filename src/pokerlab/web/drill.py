"""Postflop drill: replay your own graded spots and choose again.

The preflop drill asks charts. This one asks the solver, on spots you were
actually in: the board, your hand, the pot and what had happened on the
street are shown, the villain's cards are not, and the question is what you
do now. It draws from the verdict store, weighted toward decisions that lost
EV, and remembers every answer with the same SM-2 scheduling as the preflop
drill, so a spot you got wrong comes back and one you have learned retires.

The question is asked in two steps — the action, then the size when the
action has several — because choosing to bet is the decision and choosing
40% or 70% is a refinement; scoring them together hid which one was wrong.
"""

from __future__ import annotations

import html
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..replay.evaluate import TOLERANCE_PCT_POT
from ..replay.grader import VerdictRecord
from ..stats import grades
from . import state
from .app import _face
from .grids import family, legend, mix_text, range_grid, strategy_by_hand
from .layout import page
from .replay import bb_amount

router = APIRouter()

#: A mistake is drawn this many times more often than a decision that held up.
MISTAKE_WEIGHT = 4.0
#: Share of questions that re-test a due card before a fresh spot is drawn.
DUE_SHARE = 0.4
#: A card answered right this many times in a row is retired from the random
#: draw; it still comes back when its interval elapses.
LEARNED_REPS = 2
#: Every this many answers the drill shows a per-node summary of the session.
SUMMARY_EVERY = 20
_ORDER = {"fold": 0, "check": 1, "call": 2, "bet": 3, "raise": 4, "allin": 5}
FAMILY_LABEL = {"fold": "Fold", "check": "Check", "call": "Call", "bet": "Bet", "raise": "Raise"}
FAMILY_KEY = {"fold": "f", "check": "x", "call": "c", "bet": "b", "raise": "r"}
STREETS = (("", "any street"), ("FLOP", "flop"), ("TURN", "turn"), ("RIVER", "river"))
MODES = (("", "everything"), ("mistakes", "mistakes only"), ("due", "due for review"),
         ("struggling", "spots you keep missing"))
POSITIONS = ("", "UTG", "HJ", "CO", "BTN", "SB", "BB")


@dataclass(frozen=True, slots=True)
class DrillFilter:
    node: str = ""
    street: str = ""
    pos: str = ""
    tex: str = ""
    days: int = 0
    mode: str = ""
    villain: str = ""

    @classmethod
    def from_query(cls, node="", street="", pos="", tex="", days=0, mode="", villain=""):
        return cls(node, street, pos, tex, int(days or 0), mode, villain.strip())

    def qs(self) -> str:
        d = {k: v for k, v in (("node", self.node), ("street", self.street), ("pos", self.pos),
                               ("tex", self.tex), ("days", self.days or ""),
                               ("mode", self.mode), ("villain", self.villain)) if v}
        return urlencode(d)

    def hidden(self) -> str:
        return "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(str(v))}">'
            for k, v in (("node", self.node), ("street", self.street), ("pos", self.pos),
                         ("tex", self.tex), ("days", self.days or ""), ("mode", self.mode),
                         ("villain", self.villain)) if v)

    def describe(self) -> str:
        from .dashboard import NODE_LABEL
        bits = [NODE_LABEL.get(self.node, self.node) if self.node else "",
                self.street.lower(), self.pos, self.tex,
                f"last {self.days} days" if self.days else "",
                dict(MODES).get(self.mode, ""), f"vs {self.villain}" if self.villain else ""]
        return " · ".join(b for b in bits if b) or "every graded spot"


def _villain_hands(name: str) -> set[str]:
    key = ("drill_villain", name)
    if key not in state._state:
        rows = state.con().execute(
            "SELECT DISTINCT hand_id FROM seats WHERE name = ? AND NOT is_hero", [name]).fetchall()
        state._state[key] = {r[0] for r in rows}
    return state._state[key]


def _eligible(filt: DrillFilter = DrillFilter()) -> list[VerdictRecord]:
    recs = [r for r in state.verdicts() if grades._ok(r) and r.base and r.base.ev]
    if filt.node:
        recs = [r for r in recs if r.node == filt.node]
    if filt.street:
        recs = [r for r in recs if r.street == filt.street]
    if filt.pos:
        recs = [r for r in recs if r.hero_pos == filt.pos]
    if filt.tex:
        recs = [r for r in recs if r.street == "FLOP" and grades.texture_label(r) == filt.tex]
    if filt.days:
        since = (datetime.now(timezone.utc) - timedelta(days=filt.days)).isoformat()
        recs = [r for r in recs if r.played_at >= since]
    if filt.villain:
        ids = _villain_hands(filt.villain)
        recs = [r for r in recs if r.hand_id in ids]
    prog = state.postflop_progress()
    if filt.mode == "mistakes":
        recs = [r for r in recs if r.base.approved is False]
    elif filt.mode == "due":
        recs = [r for r in recs if _key(r) in prog.cards and prog.cards[_key(r)].due <= prog.counter]
    elif filt.mode == "struggling":
        recs = [r for r in recs if _key(r) in prog.cards and prog.cards[_key(r)].lapses >= 2]
    return recs


def _key(rec: VerdictRecord) -> str:
    return f"{rec.hand_id}:{rec.idx}"


def _pick(filt: DrillFilter, exclude: str = "") -> VerdictRecord | None:
    pool = [r for r in _eligible(filt) if _key(r) != exclude]
    if not pool:
        return None
    prog = state.postflop_progress()
    cards = prog.cards
    # Due cards first: re-testing what you got wrong beats a fresh question.
    due = [r for r in pool if _key(r) in cards and cards[_key(r)].due <= prog.counter]
    if due and (filt.mode == "due" or random.random() < DUE_SHARE):
        due.sort(key=lambda r: (cards[_key(r)].accuracy or 0, -cards[_key(r)].lapses))
        return due[0]
    fresh = [r for r in pool
             if _key(r) not in cards or cards[_key(r)].reps < LEARNED_REPS
             or cards[_key(r)].due <= prog.counter]
    pool = fresh or pool
    weights = [MISTAKE_WEIGHT if r.base.approved is False else 1.0 for r in pool]
    return random.choices(pool, weights)[0]


def _action_label(name: str, bb: int) -> str:
    verb, _, amount = name.partition(" ")
    if not amount:
        return verb
    money = bb_amount(int(float(amount)), bb)
    return {"bet": f"bet {money}", "raise": f"raise to {money}",
            "allin": f"all-in {money}"}.get(verb, name)


def _story(rec: VerdictRecord) -> str:
    """What happened on this street before the decision, in words."""
    if not rec.action_path:
        return "You are first to act."
    # Steps alternate between the two players, starting with whoever is OOP.
    first = "you" if rec.hero_oop else "villain"
    other = "villain" if first == "you" else "you"
    bits = []
    for i, step in enumerate(rec.action_path):
        actor = first if i % 2 == 0 else other
        verb, _, amount = step.partition(" ")
        money = bb_amount(int(float(amount)), rec.bb) if amount else ""
        phrase = {"check": ("check", "checks"), "call": ("call", "calls"),
                  "bet": (f"bet {money}", f"bets {money}"),
                  "raise": (f"raise to {money}", f"raises to {money}"),
                  "allin": (f"go all-in {money}", f"goes all-in {money}")}.get(verb, (step, step))
        bits.append(f"{actor} {phrase[0] if actor == 'you' else phrase[1]}")
    return (", ".join(bits) + ".").capitalize()


def _families(rec: VerdictRecord) -> dict[str, list[str]]:
    """Family -> the solver's actions in it, sizes ascending."""
    out: dict[str, list[str]] = {}
    for a in sorted(rec.base.ev, key=lambda a: (_ORDER.get(a.split()[0], 9), a)):
        fam = family(a)
        fam = "bet" if fam == "bet" and a.split()[0] != "raise" else ("raise" if a.split()[0] == "raise" else fam)
        out.setdefault(fam, []).append(a)
    return out


def _spot_header(rec: VerdictRecord) -> str:
    hero = "".join(_face(c) for c in (rec.hero_combo[:2], rec.hero_combo[2:])) if rec.hero_combo else ""
    board = "".join(_face(c) for c in rec.board)
    from .dashboard import NODE_LABEL
    return f"""
      <div class="spot">
        <span class="tag">{rec.street.lower()}</span>
        <span class="tag">{html.escape(NODE_LABEL.get(rec.node, rec.node))}</span>
        <span class="tag">{html.escape(rec.hero_pos or '?')} vs {html.escape(rec.villain_pos or '?')}</span>
        <span class="tag">{html.escape(grades.texture_label(rec)) if rec.street == 'FLOP' else ''}</span>
        <span class="tag">pot {bb_amount(rec.starting_pot, rec.bb)} · {rec.effective_stack / rec.bb:.0f}bb behind</span>
      </div>
      <div style="display:flex;gap:2rem;align-items:center;margin:1rem 0;flex-wrap:wrap">
        <div><div class="note" style="margin-bottom:.3rem">board</div><div class="cards">{board}</div></div>
        <div><div class="note" style="margin-bottom:.3rem">you</div><div class="cards">{hero}</div></div>
      </div>
      <div class="note">{html.escape(_story(rec))} {html.escape(rec.base.villain_label)}.</div>"""


def _spot_html(rec: VerdictRecord, filt: DrillFilter, tally: dict) -> str:
    fams = _families(rec)
    buttons = "".join(
        f'<button name="fam" value="{fam}" data-key="{FAMILY_KEY.get(fam, "")}" '
        f'class="{"r" if fam in ("bet", "raise") else "f" if fam == "fold" else "c"}">'
        f'{FAMILY_LABEL.get(fam, fam)}'
        f'{f"<small> {len(acts)} sizes</small>" if len(acts) > 1 else ""}</button>'
        for fam, acts in fams.items())
    score = (f"{tally['right']}/{tally['asked']} this session" if tally["asked"] else "")
    return f"""
    <div class="card">
      {_spot_header(rec)}
      <form method="post" action="/train/postflop/answer" class="btns" id="q">
        <input type="hidden" name="key" value="{html.escape(_key(rec))}">
        {filt.hidden()}
        {buttons}
      </form>
      <div class="score" style="margin-top:.6rem">{score} &middot; keys: f x c b r</div>
    </div>{_KEYS_JS}"""


def _size_html(rec: VerdictRecord, fam: str, filt: DrillFilter) -> str:
    acts = _families(rec).get(fam, [])
    buttons = "".join(
        f'<button name="action" value="{html.escape(a)}" data-key="{i + 1}" class="r">'
        f'{html.escape(_action_label(a, rec.bb))}'
        f'<small> {100 * int(float(a.split()[1])) / max(rec.starting_pot, 1):.0f}% pot</small></button>'
        for i, a in enumerate(acts))
    return f"""
    <div class="card">
      {_spot_header(rec)}
      <div class="verdict" style="margin-top:1rem">{FAMILY_LABEL.get(fam, fam)} — how much?</div>
      <form method="post" action="/train/postflop/answer" class="btns" id="q">
        <input type="hidden" name="key" value="{html.escape(_key(rec))}">
        <input type="hidden" name="fam" value="{html.escape(fam)}">
        {filt.hidden()}
        {buttons}
      </form>
      <div class="score" style="margin-top:.6rem">keys: 1–{len(acts)}</div>
    </div>{_KEYS_JS}"""


def _score(rec: VerdictRecord, fam: str, action: str | None) -> tuple[bool, bool | None, str, float]:
    """(family right, size right or None, solver's best, bb lost by the chosen action)."""
    b = rec.base
    best = max(b.ev, key=b.ev.get)
    tol = TOLERANCE_PCT_POT * max(rec.starting_pot, 1)
    fam_acts = _families(rec).get(fam, [])
    fam_best = max((b.ev[a] for a in fam_acts), default=None)
    fam_ok = fam_best is not None and b.ev[best] - fam_best <= tol
    size_ok = None
    if action is not None and len(fam_acts) > 1 and action in b.ev:
        size_ok = b.ev[best] - b.ev[action] <= tol
    chosen = action if action in b.ev else (fam_acts[0] if fam_acts else best)
    if fam_acts and action not in b.ev:
        chosen = max(fam_acts, key=b.ev.get)
    lost = (b.ev[best] - b.ev.get(chosen, b.ev[best])) / rec.bb
    return fam_ok, size_ok, best, lost


def _ranges_html(rec: VerdictRecord) -> str:
    """Hero's and the villain's range at this node, with the solver's mix when
    the solve is still on disk."""
    from ..replay.evaluate import reconstruct
    from ..replay.hand import load as load_hand
    from ..replay.node import decisions
    try:
        hand = load_hand(state.con(), rec.hand_id)
        d = {x.idx: x for x in decisions(hand)}.get(rec.idx)
        if d is None:
            return ""
        rc = reconstruct(state.pool(), state.observations(), hand, d, "base",
                         rec.base.degraded, rec.base.digest, rec.base.villain_pct)
    except Exception as exc:                                # noqa: BLE001 - the drill must not die on a range
        return f'<div class="note">Ranges unavailable: {html.escape(str(exc))}</div>'
    hero_hand = None
    if rec.hero_combo:
        from ..ranges.notation import canonical
        hero_hand = canonical([rec.hero_combo[:2], rec.hero_combo[2:]])
    strat = strategy_by_hand(rc.solution, rc.hero_range) if rc.solution else None
    lab = lambda a: _action_label(a, rec.bb)
    hero_grid = range_grid(rc.hero_range, strat, hero_hand, lab, compact=True)
    vill_grid = range_grid(rc.assignment.range, None, None, lab, compact=True)
    solver_note = ("" if rc.solution else
                   '<div class="note">The solve behind this verdict is no longer on disk, so the '
                   'cells show range membership only, not the solver\'s mix.</div>')
    rf = rec.base.range_freq
    range_line = (f'<div class="dist">Over your whole range here the solver '
                  f'{html.escape(mix_text(rf, lab))}.</div>' if rf else "")
    layers = "".join(f"<li>{html.escape(str(l))}</li>" for l in rc.assignment.layers)
    return f"""
    <div class="grids" style="margin-top:1rem">
      <div><div class="gt">Your range — {rc.hero_range.pct:.0f}% of hands, your hand outlined</div>
        {hero_grid}{legend(bool(strat))}</div>
      <div><div class="gt">{html.escape(rc.assignment.label)} — {rc.assignment.range.pct:.0f}% of hands</div>
        {vill_grid}{legend(False)}</div>
    </div>
    {range_line}{solver_note}
    <details class="note" style="margin-top:.4rem"><summary>how the villain's range was built</summary>
      <ul class="layers">{layers}</ul></details>"""


def _summary_html(tally: dict) -> str:
    from .dashboard import NODE_LABEL
    rows = "".join(
        f"<tr><td>{html.escape(NODE_LABEL.get(k, k))}</td><td>{v['asked']}</td>"
        f"<td>{v['right']}/{v['asked']}</td><td>{100 * v['right'] / v['asked']:.0f}%</td>"
        f"<td>{v['lost']:.1f} bb</td></tr>"
        for k, v in sorted(tally["nodes"].items(), key=lambda kv: -kv[1]["lost"]))
    sized = (f" · sizing {tally['sized_right']}/{tally['sized']}" if tally["sized"] else "")
    return f"""
    <div class="card"><h2>This session: {tally['right']}/{tally['asked']}{sized}</h2>
      <table class="t"><tr><th>node</th><th>asked</th><th>right</th><th></th><th>EV lost on misses</th></tr>{rows}</table>
      <div class="note" style="margin-top:.5rem">EV lost is what your drill answers gave up against the
        solver's best, summed. The node at the top is the one to filter on next.</div>
    </div>"""


def _result_html(rec: VerdictRecord, fam: str, action: str | None, filt: DrillFilter,
                 tally: dict, card) -> str:
    b = rec.base
    fam_ok, size_ok, best, lost = _score(rec, fam, action)
    shown, _clear = grades.preferred(b, rec.starting_pot)
    chosen_label = _action_label(action, rec.bb) if action else FAMILY_LABEL.get(fam, fam).lower()
    mixed = fam_ok and family(best) != (fam if fam != "raise" else "bet") and \
        sum(p for a, p in b.freq.items() if family(a) == family(action or fam)) >= 0.2
    if fam_ok and size_ok is False:
        head = (f'<span class="mix">Right action, wrong size — {html.escape(chosen_label)} '
                f'costs {lost:.1f}bb against {html.escape(_action_label(best, rec.bb))}</span>')
    elif mixed:
        head = f'<span class="mix">Fine — the solver mixes {html.escape(chosen_label)} here</span>'
    elif fam_ok:
        head = '<span class="ok">Correct</span>'
    else:
        head = (f'<span class="no">Costs {lost:.1f}bb — the solver plays '
                f'{html.escape(_action_label(shown, rec.bb))}</span>')
    rows = "".join(
        f"<tr><td>{html.escape(_action_label(a, rec.bb))}</td><td>{100 * b.freq.get(a, 0):.0f}%</td>"
        f"<td>{(b.ev[a] - b.ev[best]) / rec.bb:+.1f}bb</td>"
        f'<td class="note" style="text-align:left">{" · ".join(m for m, on in (("you chose", a == action or (action is None and family(a) == family(fam))), ("you played this", a == b.hero_action)) if on)}</td></tr>'
        for a in sorted(b.ev, key=lambda a: -b.ev[a]))
    hist = (f"seen {card.seen}× · right {card.right} · next in {card.interval} questions"
            if card.seen > 1 else "first time you have drilled this spot")
    summary = _summary_html(tally) if tally["asked"] and tally["asked"] % SUMMARY_EVERY == 0 else ""
    return f"""
    <div class="card">
      <div class="verdict">{head}</div>
      <table class="band"><tr><th>action</th><th>solver</th><th>EV vs best</th><th></th></tr>{rows}</table>
      <div class="note">Against the measured villain range ({b.villain_pct or 0:.0f}% of hands);
        {html.escape(rec.headline)} at the time. {hist}.
        <a href="/replay/{rec.hand_id}?step={rec.idx}">Open the hand →</a></div>
      {_ranges_html(rec)}
      <form method="get" action="/train/postflop" class="btns" id="q">
        <input type="hidden" name="skip" value="{html.escape(_key(rec))}">
        {filt.hidden()}
        <button class="primary" data-key="n">Next spot</button>
      </form>
    </div>{summary}{_KEYS_JS}"""


def _tally() -> dict:
    return state._state.setdefault("postflop_tally", {
        "asked": 0, "right": 0, "sized": 0, "sized_right": 0, "nodes": {}})


def _filter_form(filt: DrillFilter, n: int) -> str:
    from .dashboard import NODES, TEXTURES, _select
    prog = state.postflop_progress()
    st = prog.stats
    return f"""
    <form method="get" action="/train/postflop" class="filter">
      <label>node {_select("node", NODES, filt.node)}</label>
      <label>street {_select("street", STREETS, filt.street)}</label>
      <label>seat {_select("pos", [(p, p or "any") for p in POSITIONS], filt.pos)}</label>
      <label>flop {_select("tex", TEXTURES, filt.tex)}</label>
      <label>window {_select("days", [("", "all time"), ("7", "7 days"), ("30", "30 days"), ("90", "90 days")], str(filt.days or ""))}</label>
      <label>show {_select("mode", MODES, filt.mode)}</label>
      <label>villain <input type="text" name="villain" value="{html.escape(filt.villain)}" size="10" placeholder="name"></label>
      <button type="submit">Apply</button>
      <span class="note">{n} spots · {st['answered']:,} answered all-time ({st['accuracy']:.0%})
        · {st['due_now']} due · {st['struggling']} you keep missing</span>
    </form>"""


_KEYS_JS = """<script>
document.addEventListener('keydown',function(e){
  if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'||e.metaKey||e.ctrlKey)return;
  var k=e.key.toLowerCase(); if(k==='enter'||k===' ')k='n';
  var b=document.querySelector('#q button[data-key="'+k+'"]'); if(b){e.preventDefault();b.click();}
});
</script>"""


@router.get("/train/postflop", response_class=HTMLResponse)
def postflop_drill(skip: str = "", node: str = "", street: str = "", pos: str = "",
                   tex: str = "", days: str = "", mode: str = "", villain: str = ""):
    filt = DrillFilter.from_query(node, street, pos, tex, days, mode, villain)
    n = len(_eligible(filt))
    rec = _pick(filt, skip)
    sub = f"your own graded spots · {html.escape(filt.describe())}"
    if rec is None:
        why = ("The drill draws from graded decisions. <a href=\"/grades\">Grade a batch</a> first."
               if not _eligible() else "No graded spot matches these filters.")
        body = (f'<div class="card"><div class="verdict">Nothing to drill</div>'
                f'<div class="note">{why}</div></div>')
        return page(_tabs("postflop") + _filter_form(filt, n) + body, "Postflop drill", "/train", sub)
    return page(_tabs("postflop") + _filter_form(filt, n) + _spot_html(rec, filt, _tally()),
                "Postflop drill", "/train", sub)


@router.post("/train/postflop/answer", response_class=HTMLResponse)
def postflop_answer(key: str = Form(...), fam: str = Form(""), action: str = Form(""),
                    node: str = Form(""), street: str = Form(""), pos: str = Form(""),
                    tex: str = Form(""), days: str = Form(""), mode: str = Form(""),
                    villain: str = Form("")):
    filt = DrillFilter.from_query(node, street, pos, tex, days, mode, villain)
    hid, _, idx = key.partition(":")
    rec = state.store().get(hid).get(int(idx)) if idx.isdigit() else None
    if rec is None or not rec.base:
        return RedirectResponse(f"/train/postflop?{filt.qs()}", status_code=303)
    sub = f"your own graded spots · {html.escape(filt.describe())}"
    fams = _families(rec)
    if action:
        fam = fam or family(action)
    if fam not in fams:
        return RedirectResponse(f"/train/postflop?{filt.qs()}", status_code=303)
    if not action and len(fams[fam]) > 1:
        return page(_tabs("postflop") + _size_html(rec, fam, filt), "Postflop drill", "/train", sub)
    if not action:
        action = fams[fam][0]

    fam_ok, size_ok, best, lost = _score(rec, fam, action)
    t = _tally()
    t["asked"] += 1
    t["right"] += fam_ok
    if size_ok is not None:
        t["sized"] += 1
        t["sized_right"] += size_ok
    nd = t["nodes"].setdefault(rec.node, {"asked": 0, "right": 0, "lost": 0.0})
    nd["asked"] += 1
    nd["right"] += fam_ok
    if not fam_ok:
        nd["lost"] += lost
    prog = state.postflop_progress()
    card = prog.review(key, fam_ok)
    prog.save(prog_path())
    return page(_tabs("postflop") + _result_html(rec, fam, action, filt, t, card),
                "Postflop drill", "/train", sub)


def prog_path():
    from pathlib import Path
    return Path("data/postflop_progress.json")


def _tabs(active: str) -> str:
    def tab(href, label, key):
        on = ' style="font-weight:600;color:var(--fg)"' if key == active else ""
        return f'<a href="{href}"{on}>{label}</a>'
    return (f'<div class="filter" style="gap:1rem">{tab("/train", "Preflop (charts)", "preflop")}'
            f'{tab("/train/postflop", "Postflop (your graded spots)", "postflop")}'
            f'{tab("/leaks", "Leak trends", "leaks")}</div>')
