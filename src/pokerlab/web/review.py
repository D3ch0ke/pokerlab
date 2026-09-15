"""The post-session page: what today cost, and which decisions to look at.

One entry point for the daily loop. A session is the unit (30-minute gap,
same as `stats.sessions`), the list is every graded decision in it ordered by
what it cost, and a decision drops off the list once it has been marked as
looked at. The sync button rebuilds the database from the exports and grades
whatever the new hands added, so the page is one click from a finished
session to a review list.
"""

from __future__ import annotations

import html
from datetime import timedelta

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..replay.grader import MODEL, Filter as GradeFilter, VerdictRecord, candidates
from ..stats import grades
from ..stats.sessions import STOP_BB, Session, sessions
from . import state
from .layout import mini_cards, page, signed

router = APIRouter()

#: How far back the sync grades. New hands are rarely older than this, and
#: bounding it keeps the batch to minutes rather than hours.
SYNC_DAYS = 7


def _sessions() -> list[Session]:
    return sessions(state.con())


def session_at(index: int) -> Session | None:
    sess = _sessions()
    return sess[-1 - index] if 0 <= index < len(sess) else None


def session_key(s: Session) -> str:
    """URL-safe: an ISO stamp carries a '+' that a query string reads as a space."""
    return s.start.strftime("%Y%m%d%H%M%S")


def session_by_key(key: str) -> Session | None:
    return next((s for s in _sessions() if session_key(s) == key), None)


def verdicts_in(s: Session) -> list[VerdictRecord]:
    ids = set(s.hand_ids)
    return [r for r in state.verdicts() if r.hand_id in ids]


def ordered(s: Session) -> list[VerdictRecord]:
    """The review order: mistakes by cost, then the rest by cost, then ungradable."""
    recs = verdicts_in(s)

    def rank(r: VerdictRecord):
        ok = grades._ok(r)
        loss = r.loss_bb() or 0.0
        mistake = ok and r.base is not None and r.base.approved is False
        return (0 if mistake else 1 if ok else 2, -loss, r.played_at, r.idx)

    return sorted(recs, key=rank)


def neighbours(s: Session, hand_id: str, idx: int) -> tuple[VerdictRecord | None, VerdictRecord | None, int, int]:
    """(previous, next, position, total) for one decision inside a session's list."""
    lst = ordered(s)
    pos = next((i for i, r in enumerate(lst) if r.hand_id == hand_id and r.idx == idx), None)
    if pos is None:
        return None, None, 0, len(lst)
    prev = lst[pos - 1] if pos > 0 else None
    nxt = lst[pos + 1] if pos + 1 < len(lst) else None
    return prev, nxt, pos + 1, len(lst)


def pending(s: Session) -> int:
    filt = GradeFilter(since=s.start, until=s.end + timedelta(seconds=1))
    return len(candidates(state.con(), filt, state.store()))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def session_card(s: Session, index: int | None = None, link: bool = False) -> str:
    """The tiles for one session; shared with the overview."""
    recs = verdicts_in(s)
    tot = grades.totals(recs)
    past = s.played_past_stop()
    stop_i = s.stop_crossed()
    if stop_i is not None and past is not None:
        hands_after, mins_after = past
        when = s.times[stop_i].strftime("%H:%M")
        stop = (f'<div class="warn">Stop-loss (−{STOP_BB:.0f}bb) crossed at {when}; '
                f'you played {hands_after} more hands over {mins_after:.0f} minutes after it.</div>'
                if hands_after > 0 else
                f'<div class="note">Stop-loss crossed at {when} and you stopped there.</div>')
    else:
        stop = ""
    ev = (f"{tot.loss_bb:.1f} bb over {tot.usable} usable decisions"
          if tot.usable else "nothing graded yet")
    title = f"{s.start:%a %d %b %H:%M}–{s.end:%H:%M}"
    if link and index is not None:
        title = f'<a href="/review?s={index}">{title}</a>'
    return f"""
    <div class="card">
      <h2>{title}</h2>
      <div class="tiles" style="margin-bottom:.6rem">
        <div class="tile"><div class="k">hands</div><div class="v">{s.hands}</div>
          <div class="n">{s.minutes:.0f} min · {s.tables} table{'s' if s.tables != 1 else ''}</div></div>
        <div class="tile"><div class="k">result</div><div class="v">{signed(s.net_eur, ' €', 2)}</div>
          <div class="n">{signed(s.final, ' bb')} · {signed(100 * s.final / max(s.hands, 1), ' bb/100')}</div></div>
        <div class="tile"><div class="k">EV given up</div><div class="v">{f'{tot.loss_bb:.1f} bb' if tot.usable else '--'}</div>
          <div class="n">{ev}</div></div>
        <div class="tile"><div class="k">graded</div><div class="v">{tot.graded}</div>
          <div class="n">{tot.hands} hands · {sum(1 for r in recs if grades._ok(r) and r.base and r.base.approved is False)} mistakes</div></div>
      </div>
      {stop}
    </div>"""


def _row(r: VerdictRecord, key: str, seen: bool, s_key: str) -> str:
    b = r.base
    ok = grades._ok(r)
    loss = r.loss_bb() or 0.0
    best, clear = grades.preferred(b, r.starting_pot)
    cls = "b-no" if (ok and b and b.approved is False) else "b-ok" if ok else "b-dim"
    head = r.headline if ok else (r.headline if r.headline else "not usable")
    from .dashboard import NODE_LABEL
    from .replay import _action_name
    did = _action_name(r.hero_step.replace("~", " "), r.bb)
    best = (_action_name(best, r.bb) + ("" if clear else " (mixed)")) if best else ""
    return (
        f'<tr class="{"dimrow" if seen else ""}">'
        f'<td><a href="/replay/{r.hand_id}?step={r.idx}&list={html.escape(s_key)}">'
        f'{r.played_at[11:16]}</a></td>'
        f"<td>{mini_cards([r.hero_combo[:2], r.hero_combo[2:]] if r.hero_combo else '')}</td>"
        f"<td>{mini_cards(r.board)}</td><td>{r.street.lower()}</td>"
        f"<td>{html.escape(NODE_LABEL.get(r.node, r.node))}</td>"
        f"<td>{html.escape(did)}</td><td>{html.escape(best)}</td>"
        f"<td>{signed(-loss, ' bb') if ok else '--'}</td>"
        f'<td><span class="badge {cls}">{html.escape(head)}</span></td>'
        f'<td><form method="post" action="/review/mark" style="display:inline">'
        f'<input type="hidden" name="key" value="{html.escape(key)}">'
        f'<input type="hidden" name="back" value="/review?s={{s}}">'
        f'<button class="{"" if seen else "c"}" style="padding:.1rem .5rem;font-size:.7rem">'
        f'{"undo" if seen else "done"}</button></form></td></tr>')


@router.get("/review", response_class=HTMLResponse)
def review(s: int = 0, all: int = 0):
    sess = _sessions()
    if not sess:
        return page('<div class="card"><div class="note">No sessions on file.</div></div>',
                    "Review", "/review", "nothing to review")
    s = max(0, min(s, len(sess) - 1))
    cur = sess[-1 - s]
    marks = state.marks()
    from .dashboard import _progress_card
    g = state.grader()

    opts = "".join(
        f'<option value="{i}"{" selected" if i == s else ""}>'
        f'{x.start:%Y-%m-%d %H:%M} · {x.hands} hands · {x.final:+.0f}bb</option>'
        for i, x in enumerate(reversed(sess[-30:])))
    picker = f"""
    <form method="get" action="/review" class="filter">
      <label>session <select name="s" onchange="this.form.submit()">{opts}</select></label>
      <label><input type="checkbox" name="all" value="1" {"checked" if all else ""}
        onchange="this.form.submit()"> show decisions already reviewed</label>
    </form>
    <form method="post" action="/review/sync" class="filter" style="margin-top:-.5rem">
      <button class="primary" {"disabled" if g.running else ""}>Import new hands &amp; grade</button>
      <span class="note">rebuilds the database from the exports (seconds), then grades every
        decision of the last {SYNC_DAYS} days that has no verdict yet</span>
    </form>"""

    left = pending(cur)
    lst = ordered(cur)
    stale = sum(1 for r in lst if r.model != MODEL)
    s_key = session_key(cur)
    rows = []
    hidden = 0
    for r in lst:
        key = marks.key(r.hand_id, r.idx)
        seen = key in marks
        if seen and not all:
            hidden += 1
            continue
        rows.append(_row(r, key, seen, s_key).replace("{s}", str(s)))
    table = (f'<table class="t"><tr><th>time</th><th>you</th><th>board</th><th>street</th>'
             f'<th>node</th><th>you did</th><th>solver</th><th>EV</th><th></th><th></th></tr>'
             f'{"".join(rows)}</table>' if rows else
             '<div class="note">Nothing left to review in this session.</div>')
    pend = (f'<div class="warn">{left} decision{"s" if left != 1 else ""} in this session not '
            f'graded yet — the sync button grades them (about {left * 0.5:.0f} min).</div>'
            if left else "")
    if stale:
        pend += (f'<form method="post" action="/review/regrade" class="filter">'
                 f'<input type="hidden" name="s" value="{s}">'
                 f'<button {"disabled" if g.running else ""}>Re-grade {stale} verdict{"s" if stale != 1 else ""} '
                 f'under the current range model</button>'
                 f'<span class="note">these were solved before villain-specific postflop narrowing '
                 f'({html.escape(MODEL)}); the old verdicts stay until replaced</span></form>')
    body = f"""
    {picker}
    {_progress_card(g)}
    {session_card(cur)}
    {pend}
    <div class="card"><h2>Decisions, costliest first</h2>
      {table}
      <div class="note" style="margin-top:.6rem">Open a hand and use prev / next to walk the
        list without coming back here. Mark a decision done once you have read why the solver
        differs; {hidden} already marked{" (shown greyed)" if all and hidden else ""}.</div>
    </div>"""
    return page(body, "Review", "/review",
                f"session {s + 1} of {len(sess)} from the end · {cur.start:%Y-%m-%d}")


@router.post("/review/mark")
def mark(key: str = Form(...), back: str = Form("/review")):
    state.marks().toggle(key)
    return RedirectResponse(back if back.startswith("/") else "/review", status_code=303)


@router.post("/review/regrade")
def regrade(s: int = Form(0)):
    """Solve this session's decisions again under the current range model."""
    cur = session_at(int(s))
    g = state.grader()
    if cur is not None and not g.running:
        filt = GradeFilter(since=cur.start, until=cur.end + timedelta(seconds=1), refresh=True)
        items = candidates(state.con(), filt, state.store())
        if items:
            try:
                g.start(items, filt)
            except Exception as exc:                        # noqa: BLE001 - shown on the page
                g.progress.status = "failed"
                g.progress.error = str(exc)
    return RedirectResponse(f"/review?s={s}", status_code=303)


@router.post("/review/sync")
def sync():
    """Import, then grade everything the last week added. Import is seconds;
    grading runs on the grader's thread and the page shows its progress."""
    from datetime import datetime, timezone
    from ..replay.grader import STREETS
    try:
        state.reimport()
    except RuntimeError as exc:
        g = state.grader()
        g.progress.error = str(exc)
        return RedirectResponse("/review", status_code=303)
    g = state.grader()
    now = datetime.now(timezone.utc)
    filt = GradeFilter(since=now - timedelta(days=SYNC_DAYS), until=now + timedelta(days=1),
                       streets=STREETS)
    items = candidates(state.con(), filt, state.store())
    if items:
        try:
            g.start(items, filt)
        except Exception as exc:                            # noqa: BLE001 - shown on the page
            g.progress.status = "failed"
            g.progress.error = str(exc)
    return RedirectResponse("/review", status_code=303)
