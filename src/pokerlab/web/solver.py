"""The solver panel: build any postflop spot, pick every assumption it rests
on, see what the tree costs before it runs, and read the answer node by node.

Nothing here paints a range by hand. Ranges come from named sources -- a
chart node, the pool's measured width at a preflop cell, hero's own
measured play, or a typed spec -- so every range on the page says where it
came from. The pool-specific part lives in a preset (`solver/presets.py`):
size menus for each player, the villain's locked mix per node, the rake and
the convergence target. Presets are edited on their own page and every field
is a number you can see.

A solve runs on a thread like the replayer's; the page polls a fragment.
"""

from __future__ import annotations

import html
import threading
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..ranges.chart import available as charts_available, load as load_chart
from ..ranges.notation import Range, canonical, combos
from ..replay.villain import assign
from ..solver import bridge, presets as P
from ..stats.core import MIN_N
from ..stats.ranges import observed_range
from . import state
from .grids import legend, mix_text, range_grid
from .layout import page

router = APIRouter()

CHIPS_PER_BB = 10
RAKE_CAP_BB = 20          # EUR 1.00 at NL5
MAX_REPORT_NODES = 60

CSS = """
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.6rem 1rem;font-size:.82rem}
.form label{display:flex;flex-direction:column;gap:.2rem;color:var(--dim)}
.form label span{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
.form select,.form input{width:100%}
.form .wide{grid-column:1/-1}
.est{font-size:.82rem;margin-top:.6rem;color:var(--dim)}
.est b{color:var(--fg)} .est.bad b{color:var(--fold)}
table.t{border-collapse:collapse;font-size:.8rem;font-variant-numeric:tabular-nums;width:100%}
table.t th{text-align:left;font-weight:600;color:var(--dim);font-size:.7rem;padding:.25rem .5rem;
border-bottom:1px solid var(--line)}
table.t td{padding:.3rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}
table.t tr.node{cursor:pointer} table.t tr.node:hover td{background:var(--side)}
table.t tr.lockd td:first-child{border-left:3px solid var(--warn)}
.tag{display:inline-block;font-size:.66rem;padding:0 .35rem;border-radius:4px;background:var(--line);
color:var(--dim);margin-left:.3rem;vertical-align:1px}
.tag.lock{background:var(--warnbg);color:var(--warn)}
.tag.eq{background:var(--foldbg)}
.res .head{display:flex;gap:1.2rem;flex-wrap:wrap;font-size:.8rem;color:var(--dim);margin-bottom:.6rem}
.res .head b{color:var(--fg)}
.mixbar{display:flex;height:14px;border-radius:4px;overflow:hidden;min-width:120px}
.mixbar i{display:block;height:100%}
.evrow{display:flex;gap:1.2rem;flex-wrap:wrap;font-size:.82rem;margin:.4rem 0}
.evrow b{font-variant-numeric:tabular-nums}
.pol input[type=number]{width:62px}
.pol td.mixcell{white-space:nowrap}
.pol tr.off td{opacity:.45}
.two{display:grid;grid-template-columns:1fr 1fr;gap:1rem;align-items:start}
@media(max-width:900px){.two{grid-template-columns:1fr}}
.path{font-family:ui-monospace,Menlo,monospace;font-size:.74rem}
"""

# --------------------------------------------------------------------------
# range sources
# --------------------------------------------------------------------------

POSITIONS = ("UTG", "HJ", "CO", "BTN", "SB", "BB")


def _range_options() -> list[tuple[str, list[tuple[str, str]]]]:
    """(group, [(value, label)]) for the two range selectors."""
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    chart_opts = []
    for chart in charts_available():
        for pos in chart.positions:
            for action in chart.actions:
                if action == "fold":
                    continue
                rng = chart.action_range(pos, action)
                if rng.n_combos > 0:
                    chart_opts.append((f"chart|{chart.id}|{pos}|{action}",
                                       f"{chart.id} · {pos} {action} ({rng.pct:.0f}%, {chart.confidence})"))
    groups.append(("Charts", chart_opts))
    pool = state.pool()
    pool_opts = []
    seen = set()
    for (key, spot, verb), cell in sorted(pool.cells.items()):
        # A cell under 2% is a range nobody would solve against (the BB cannot
        # call a limp; a "call" that is really a check).
        if verb not in ("Raises to", "Calls") or cell.frequency is None or "~" in key:
            continue
        if cell.frequency < 0.02:
            continue
        position, _, opener = key.partition("_vs_")
        if position not in POSITIONS:
            continue
        val = f"pool|{position}|{spot}|{verb}|{opener}"
        if val in seen:
            continue
        seen.add(val)
        pool_opts.append((val, f"pool · {key} {spot} {verb.lower()} "
                               f"({100 * cell.frequency:.0f}% of hands, n={cell.opportunities:,})"))
    groups.append(("Pool (measured width)", pool_opts))
    hero_opts = []
    obs = state.observations()
    for pos in POSITIONS:
        for spot, verbs, openers in (("RFI", ("Raises to",), (None,)),
                                     ("vs_open", ("Calls", "Raises to"), POSITIONS)):
            for verb in verbs:
                for opener in openers:
                    if spot == "vs_open" and (opener == pos or not opener):
                        continue
                    key = f"{pos}_vs_{opener}" if opener else pos
                    rng = observed_range(obs, key, spot, action_verbs=(verb,))
                    if rng.n_combos >= 40:
                        hero_opts.append((f"hero|{pos}|{spot}|{verb}|{opener or ''}",
                                          f"hero · {key} {spot} {verb.lower()} ({rng.pct:.0f}% measured)"))
    groups.append(("Hero (own measured play)", hero_opts))
    return groups


def resolve_range(value: str, width_scale: dict[str, float] | None = None) -> tuple[Range, str]:
    """A Range and a one-line provenance from a selector value."""
    kind, _, rest = value.partition("|")
    if kind == "spec":
        rng = Range.parse(rest)
        return rng, f"typed spec ({rng.pct:.0f}% of hands)"
    if kind == "chart":
        cid, pos, action = rest.split("|")
        chart = load_chart(cid)
        rng = chart.action_range(pos, action)
        return rng, f"{cid} {pos} {action} ({rng.pct:.0f}%, {chart.confidence}, {chart.source})"
    if kind == "pool":
        pos, spot, verb, opener = rest.split("|")
        scale = 1.0
        if width_scale:
            scale = width_scale.get("raise" if verb == "Raises to" else "call", 1.0)
        a = assign(state.pool(), "", pos, spot, verb, opener or None, width_scale=scale)
        note = "; ".join(str(l) for l in a.layers)
        if scale != 1.0:
            note += f"; width ×{scale:.2f} from the preset"
        return a.range, f"{a.label} ({a.width_pct:.0f}%): {note}"
    if kind == "villain":
        name, pos, spot, verb, opener = rest.split("|")
        a = assign(state.pool(), name, pos, spot, verb, opener or None)
        return a.range, f"{name}: {a.label} ({a.width_pct:.0f}%): " + "; ".join(str(l) for l in a.layers)
    if kind == "hero":
        pos, spot, verb, opener = rest.split("|")
        key = f"{pos}_vs_{opener}" if opener else pos
        rng = observed_range(state.observations(), key, spot, action_verbs=(verb,))
        return rng, f"hero's own {spot} {verb.lower()} as {key} ({rng.pct:.0f}%, recency-weighted)"
    raise ValueError(f"unknown range source {value!r}")


# --------------------------------------------------------------------------
# the spot form
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Form_:
    board: str = "Ah Kd 7c"
    pot: float = 5.5
    stack: float = 97.5
    pot_type: str = "srp"
    hero_oop: bool = False
    hero_lead: bool = True
    hero_range: str = "chart|rfi_6max_100bb|BTN|raise"
    villain_range: str = "pool|BB|vs_open|Calls|BTN"
    preset: str = "nl5-pool"
    compare: bool = True

    def query(self) -> str:
        return urlencode({"board": self.board, "pot": self.pot, "stack": self.stack,
                          "pot_type": self.pot_type, "hero_oop": int(self.hero_oop),
                          "hero_lead": int(self.hero_lead), "hero_range": self.hero_range,
                          "villain_range": self.villain_range, "preset": self.preset,
                          "compare": int(self.compare)})


def _form_from(q) -> Form_:
    f = Form_()
    g = q.get
    f.board = (g("board") or f.board).strip()
    try:
        f.pot = float(g("pot") or f.pot)
        f.stack = float(g("stack") or f.stack)
    except ValueError:
        pass
    f.pot_type = g("pot_type") or f.pot_type
    f.hero_oop = g("hero_oop") in ("1", "true", "on")
    f.hero_lead = g("hero_lead") in ("1", "true", "on") if g("hero_lead") is not None else f.hero_lead
    f.hero_range = g("hero_range") or f.hero_range
    f.villain_range = g("villain_range") or f.villain_range
    if g("hero_spec"):
        f.hero_range = "spec|" + g("hero_spec").strip()
    if g("villain_spec"):
        f.villain_range = "spec|" + g("villain_spec").strip()
    f.preset = g("preset") or f.preset
    f.compare = g("compare") in ("1", "true", "on") if g("compare") is not None else f.compare
    return f


def _board(text: str) -> tuple[str, ...]:
    cards = tuple(c[0].upper() + c[1].lower() for c in text.replace(",", " ").split() if len(c) == 2)
    if len(cards) not in (3, 4, 5):
        raise ValueError("board needs 3, 4 or 5 cards like 'Ah Kd 7c'")
    return cards


def build_spot(f: Form_, preset: P.Preset) -> tuple[bridge.Spot, Range, Range, str, str]:
    hero, hero_note = resolve_range(f.hero_range)
    villain, villain_note = resolve_range(f.villain_range, preset.width_scale)
    if hero.n_combos <= 0 or villain.n_combos <= 0:
        raise ValueError("a range is empty")
    hb, hr, vb, vr = preset.hero_bets, preset.hero_raises, preset.villain_bets, preset.villain_raises
    spot = bridge.Spot(
        oop_range=(hero if f.hero_oop else villain).to_spec(),
        ip_range=(villain if f.hero_oop else hero).to_spec(),
        board=_board(f.board),
        starting_pot=int(round(f.pot * CHIPS_PER_BB)),
        effective_stack=int(round(f.stack * CHIPS_PER_BB)),
        bet_sizes=hb, raise_sizes=hr,
        oop_bet_sizes=hb if f.hero_oop else vb, oop_raise_sizes=hr if f.hero_oop else vr,
        ip_bet_sizes=vb if f.hero_oop else hb, ip_raise_sizes=vr if f.hero_oop else hr,
        donk_sizes=preset.donk_sizes if any(preset.donk_sizes.values()) else None,
        max_iterations=preset.max_iterations, target_exploitability=preset.target_exploitability,
        compress_memory=preset.compress_memory,
        rake_rate=preset.rake_rate, rake_cap=int(round(RAKE_CAP_BB * CHIPS_PER_BB)),
        max_memory_bytes=int(preset.memory_budget_gb * 1e9),
        add_allin_threshold=preset.add_allin_threshold,
        force_allin_threshold=preset.force_allin_threshold,
        merging_threshold=preset.merging_threshold,
    )
    return spot, hero, villain, hero_note, villain_note


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

@dataclass
class Job:
    key: str
    form: Form_
    preset: P.Preset
    status: str = "running"
    spot: bridge.Spot | None = None
    hero: Range | None = None
    villain: Range | None = None
    hero_note: str = ""
    villain_note: str = ""
    solution: bridge.Solution | None = None
    applied: list[dict] = field(default_factory=list)
    tree_nodes: int = 0
    error: str = ""
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def elapsed(self) -> float:
        return (datetime.now(timezone.utc) - self.started).total_seconds()


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def prepare(f: Form_, preset: P.Preset) -> tuple[bridge.Spot, Range, Range, str, str, bridge.Solution, list[dict]]:
    """Everything before the solve: the spot, a dry run, the locks."""
    spot, hero, villain, hn, vn = build_spot(f, preset)
    dry = bridge.dry_run(spot)
    locks, applied = P.locks_for(preset, dry.tree or [], spot.starting_pot, f.hero_oop,
                                 f.hero_lead, f.pot_type)
    street0 = ("flop", "turn", "river")[len(spot.board) - 3]
    paths = [tuple(n["path"]) for n in (dry.tree or []) if n["street"] == street0][:MAX_REPORT_NODES]
    spot = replace(spot, locks=tuple(locks), report_paths=tuple(paths))
    return spot, hero, villain, hn, vn, dry, applied


def start(f: Form_, preset: P.Preset) -> Job:
    key = f"{preset.id}:{hash((f.board, f.pot, f.stack, f.pot_type, f.hero_oop, f.hero_lead, f.hero_range, f.villain_range)) & 0xffffffff:x}"
    with _lock:
        existing = _jobs.get(key)
        if existing and existing.status in ("running", "done"):
            return existing
        job = _jobs[key] = Job(key, f, preset)

    def run() -> None:
        try:
            spot, hero, villain, hn, vn, dry, applied = prepare(f, preset)
            job.spot, job.hero, job.villain, job.hero_note, job.villain_note = spot, hero, villain, hn, vn
            job.applied, job.tree_nodes = applied, len(dry.tree or [])
            job.solution = bridge.solve(spot, timeout=1800)
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

def _opt(options, selected: str) -> str:
    out = []
    for group, items in options:
        out.append(f'<optgroup label="{html.escape(group)}">')
        for value, label in items:
            sel = " selected" if value == selected else ""
            out.append(f'<option value="{html.escape(value)}"{sel}>{html.escape(label)}</option>')
        out.append("</optgroup>")
    return "".join(out)


def _preset_options(presets: list[P.Preset], selected: str) -> str:
    return "".join(f'<option value="{html.escape(p.id)}"{" selected" if p.id == selected else ""}>'
                   f'{html.escape(p.name)}</option>' for p in presets)


def _form_html(f: Form_, presets: list[P.Preset]) -> str:
    opts = _range_options()
    hero_spec = f.hero_range[5:] if f.hero_range.startswith("spec|") else ""
    villain_spec = f.villain_range[5:] if f.villain_range.startswith("spec|") else ""
    return f"""
<form method="get" action="/solver" id="spot" class="card">
<div class="form">
 <label><span>Board</span><input type="text" name="board" value="{html.escape(f.board)}" placeholder="Ah Kd 7c"></label>
 <label><span>Pot (bb)</span><input type="number" step="any" name="pot" value="{f.pot:g}"></label>
 <label><span>Effective stack (bb)</span><input type="number" step="any" name="stack" value="{f.stack:g}"></label>
 <label><span>Pot type</span><select name="pot_type">
   {"".join(f'<option value="{v}"{" selected" if f.pot_type == v else ""}>{l}</option>' for v, l in (("srp", "single-raised"), ("3bet", "3-bet"), ("limped", "limped")))}
 </select></label>
 <label><span>Hero position</span><select name="hero_oop">
   <option value="0"{"" if f.hero_oop else " selected"}>in position</option>
   <option value="1"{" selected" if f.hero_oop else ""}>out of position</option></select></label>
 <label><span>Hero role</span><select name="hero_lead">
   <option value="1"{" selected" if f.hero_lead else ""}>preflop aggressor (leads)</option>
   <option value="0"{"" if f.hero_lead else " selected"}>caller (villain leads)</option></select></label>
 <label class="wide"><span>Hero range</span><select name="hero_range">{_opt(opts, f.hero_range)}</select>
   <input type="text" name="hero_spec" value="{html.escape(hero_spec)}" placeholder="…or type a spec: AA,KK,AKs:0.5 (overrides the pick)"></label>
 <label class="wide"><span>Villain range</span><select name="villain_range">{_opt(opts, f.villain_range)}</select>
   <input type="text" name="villain_spec" value="{html.escape(villain_spec)}" placeholder="…or type a spec (overrides the pick)"></label>
 <label><span>Assumptions preset</span><select name="preset">{_preset_options(presets, f.preset)}</select></label>
 <label><span>Compare</span><select name="compare">
   <option value="1"{" selected" if f.compare else ""}>with equilibrium side by side</option>
   <option value="0"{"" if f.compare else " selected"}>preset only</option></select></label>
</div>
<div class="btns" style="margin-top:.8rem">
 <button type="button" id="estimate">Estimate tree</button>
 <button type="submit" formmethod="post" formaction="/solver/solve" class="primary">Solve</button>
 <a href="/solver/presets/{html.escape(f.preset)}" style="align-self:center;font-size:.85rem">edit preset →</a>
</div>
<div class="est" id="est"></div>
</form>
<script>
(function(){{
 const form=document.getElementById('spot'), est=document.getElementById('est');
 async function estimate(){{
   est.textContent='estimating…'; est.className='est';
   const q=new URLSearchParams(new FormData(form)).toString();
   try{{ const r=await fetch('/solver/estimate?'+q); const d=await r.json();
     if(d.error){{ est.className='est bad'; est.innerHTML='<b>'+d.error+'</b>'; return; }}
     est.className='est'+(d.fits?'':' bad');
     est.innerHTML='tree <b>'+d.gb.toFixed(2)+' GB</b> ('+d.compressed_gb.toFixed(2)+' compressed) · budget '+d.budget_gb+' GB · <b>'+d.nodes+'</b> decision nodes · villain nodes locked <b>'+d.locked+'</b> of '+d.villain_nodes+(d.fits?'':' · <b>will not fit: drop a size or lower the stack</b>')+' · hero '+d.hero_pct+'% · villain '+d.villain_pct+'%';
   }}catch(e){{ est.className='est bad'; est.innerHTML='<b>estimate failed</b>'; }}
 }}
 document.getElementById('estimate').addEventListener('click',estimate);
 form.querySelectorAll('select,input').forEach(el=>el.addEventListener('change',estimate));
 estimate();
}})();
</script>"""


def _mixbar(mix: dict[str, float]) -> str:
    from .grids import COLOUR, ORDER, family
    by = {}
    for a, p in mix.items():
        by[family(a)] = by.get(family(a), 0.0) + p
    parts = "".join(f'<i style="width:{100 * by.get(f, 0):.1f}%;background:{COLOUR[f]}"></i>'
                    for f in ORDER if by.get(f, 0) > 0)
    return f'<span class="mixbar" title="{html.escape(mix_text(mix))}">{parts}</span>'


def _bb(chips: float) -> str:
    return f"{chips / CHIPS_PER_BB:+.2f}bb"


def _node_rows(job: Job) -> str:
    sol = job.solution
    hero_side = "oop" if job.form.hero_oop else "ip"
    rows = []
    for r in sol.reports:
        who = "hero" if r["player"] == hero_side else "villain"
        locked = r.get("locked")
        cls = "node" + (" lockd" if locked else "")
        path = " › ".join(r["path"]) or "root"
        tag = '<span class="tag lock">locked</span>' if locked else ""
        rows.append(f'<tr class="{cls}" data-path="{html.escape("|".join(r["path"]))}">'
                    f'<td class="path">{html.escape(path)}</td><td>{who}{tag}</td>'
                    f'<td>{_mixbar(r["aggregate"])}</td>'
                    f'<td>{html.escape(mix_text(r["aggregate"]))}</td></tr>')
    return "".join(rows)


def _hero_first(job: Job) -> dict | None:
    """Hero's first decision on the street: the root if hero acts first, else the
    node after the villain's check."""
    hero_side = "oop" if job.form.hero_oop else "ip"
    for r in job.solution.reports:
        if r["player"] == hero_side and (r["path"] == [] or r["path"] == ["check"]):
            return r
    return next((r for r in job.solution.reports if r["player"] == hero_side), None)


def _report_strategy(report: dict, rng: Range) -> dict[str, dict[str, float]]:
    from collections import defaultdict
    acc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    n: dict[str, int] = defaultdict(int)
    for row in report["strategy"]:
        combo = row["hand"]
        hand = canonical([combo[:2], combo[2:]])
        if rng.freq(hand) <= 0:
            continue
        n[hand] += 1
        for a, p in row["actions"].items():
            acc[hand][a] += p
    return {h: {a: v / n[h] for a, v in acts.items()} for h, acts in acc.items()}


def _ev_row(report: dict) -> str:
    """Range-weighted EV per action at the node, in bb, fold = 0."""
    tot: dict[str, float] = {}
    for row in report["strategy"]:
        w = row.get("weight", 0.0)
        for a, ev in row.get("ev", {}).items():
            tot[a] = tot.get(a, 0.0) + w * ev
    items = "".join(f'<span>{html.escape(a)} <b>{_bb(v)}</b></span>'
                    for a, v in sorted(tot.items(), key=lambda kv: -kv[1]))
    return f'<div class="evrow">{items}<span class="note">range-weighted EV per action, fold = 0</span></div>'


def _node_panel(job: Job, report: dict) -> str:
    hero_side = "oop" if job.form.hero_oop else "ip"
    rng = job.hero if report["player"] == hero_side else job.villain
    who = "hero" if report["player"] == hero_side else "villain"
    strat = _report_strategy(report, rng)
    path = " › ".join(report["path"]) or "root"
    lock = ' <span class="tag lock">locked to the preset</span>' if report.get("locked") else ""
    return (f'<div class="gt">{who} at <span class="path">{html.escape(path)}</span>{lock}: '
            f'{html.escape(mix_text(report["aggregate"]))}</div>'
            f'{_ev_row(report)}{range_grid(rng, strat)}{legend(True)}')


def _applied_html(job: Job) -> str:
    from collections import Counter
    if not job.preset.locks_anything:
        return '<div class="note">No locks: both players free at every node (equilibrium).</div>'
    c: Counter = Counter()
    mixes: dict = {}
    unmatched = 0
    for a in job.applied:
        k = (a["street"], a["situation"], a["size"])
        if a["rule"] is None:
            unmatched += 1
            continue
        c[k] += 1
        mixes[k] = a["actions"]
    rows = "".join(
        f'<tr><td>{s}</td><td>{html.escape(dict(P.SITUATIONS).get(sit, sit))}</td><td>{sz}</td>'
        f'<td>{n}</td><td>{html.escape(mix_text(mixes[(s, sit, sz)]))}</td></tr>'
        for (s, sit, sz), n in sorted(c.items()))
    note = (f'<div class="note">{unmatched} villain node(s) had no matching rule and were left free.</div>'
            if unmatched else "")
    return (f'<table class="t"><tr><th>street</th><th>villain situation</th><th>size faced</th>'
            f'<th>nodes</th><th>locked mix (dealt strongest-first, blend '
            f'{", ".join(f"{k} {v:g}" for k, v in job.preset.blend.items())})</th></tr>{rows}</table>{note}')


def _result_html(job: Job) -> str:
    if job.status == "running":
        return (f'<div class="card"><b>{html.escape(job.preset.name)}</b> · solving… '
                f'{job.elapsed:.0f}s <div class="progress"><i style="width:30%"></i></div>'
                f'<div class="note">flop trees take 15–60 s; turn and river spots are instant</div></div>')
    if job.status == "failed":
        return f'<div class="card"><b>{html.escape(job.preset.name)}</b><div class="warn">{html.escape(job.error)}</div></div>'
    sol = job.solution
    first = _hero_first(job)
    warn = "".join(f'<div class="warn">{html.escape(w)}</div>' for w in sol.warnings
                   if "node(s) locked" not in w)
    tag = ('<span class="tag lock">pool-locked</span>' if sol.locked_nodes
           else '<span class="tag eq">equilibrium</span>')
    hero_ev = sol.oop_ev if job.form.hero_oop else sol.ip_ev
    hero_eq = sol.oop_equity if job.form.hero_oop else sol.ip_equity
    head = (f'<div class="head"><span>exploitability <b>{sol.exploitability_pct_pot:.2f}%</b> of pot'
            f'{"" if sol.converged else " (not converged)"}</span>'
            f'<span>{sol.iterations} it · {sol.elapsed_ms / 1000:.1f}s · {sol.memory_usage_bytes / 1e9:.2f} GB</span>'
            f'<span>hero EV <b>{_bb(hero_ev)}</b> at the root (equity {100 * hero_eq:.1f}%)</span>'
            f'<span>{sol.locked_nodes} villain nodes locked of {job.tree_nodes}</span></div>')
    if sol.locked_nodes:
        head += ('<div class="note">With villain locked, the exploitability is hero\'s distance from '
                 'a best response to the assumed play, and hero EV is against that play — not '
                 'against an equilibrium opponent.</div>')
    panel = _node_panel(job, first) if first else '<div class="note">hero has no node on this street</div>'
    return f"""<div class="card res" data-job="{html.escape(job.key)}">
<h2>{html.escape(job.preset.name)} {tag}</h2>{head}{warn}
<div class="note">hero: {html.escape(job.hero_note)}<br>villain: {html.escape(job.villain_note)}</div>
<div id="panel-{html.escape(job.key)}" style="margin-top:.8rem">{panel}</div>
<h2>Every node on this street</h2>
<div class="note">click a row to see that node's grid</div>
<table class="t nodes"><tr><th>after</th><th>to act</th><th>mix</th><th></th></tr>{_node_rows(job)}</table>
<h2>What was assumed about the villain</h2>{_applied_html(job)}
</div>"""


def _results_block(keys: list[str]) -> str:
    jobs = [_jobs.get(k) for k in keys]
    jobs = [j for j in jobs if j]
    if not jobs:
        return ""
    body = "".join(_result_html(j) for j in jobs)
    cls = "two" if len(jobs) > 1 else ""
    running = any(j.status == "running" for j in jobs)
    return f'<div id="results" class="{cls}" data-running="{int(running)}">{body}</div>'


def _page(body: str, sub: str = "") -> HTMLResponse:
    return page(body, "Solver", "/solver", sub, extra_css=CSS, wide=True)


POLL_JS = """
<script>
(function(){
 const res=document.getElementById('results'); if(!res) return;
 const keys=res.dataset.keys;
 async function poll(){
   const r=await fetch('/solver/job/'+keys); const t=await r.text();
   const tmp=document.createElement('div'); tmp.innerHTML=t; const fresh=tmp.firstElementChild;
   res.replaceWith(fresh); wire(fresh);
   if(fresh.dataset.running==='1') setTimeout(poll,2000);
 }
 function wire(root){
   root.dataset.keys=keys;
   root.querySelectorAll('.res').forEach(card=>{
     card.querySelectorAll('tr.node').forEach(tr=>tr.addEventListener('click',async()=>{
       const p=card.querySelector('[id^=panel-]');
       p.style.opacity=.4;
       const r=await fetch('/solver/job/'+card.dataset.job+'/node?path='+encodeURIComponent(tr.dataset.path));
       p.innerHTML=await r.text(); p.style.opacity=1;
     }));
   });
 }
 wire(res);
 if(res.dataset.running==='1') setTimeout(poll,1500);
})();
</script>"""


@router.get("/solver", response_class=HTMLResponse)
def solver_page(request: Request):
    f = _form_from(request.query_params)
    presets = state.presets()
    keys = [k for k in (request.query_params.get("job") or "").split(",") if k]
    results = _results_block(keys)
    if results:
        results = results.replace('id="results"', f'id="results" data-keys="{html.escape(",".join(keys))}"', 1)
    sub = ("Solve any spot against explicit assumptions: chart or measured ranges, each player's "
           "size menu, and a villain locked to the pool's measured play at every node. "
           f"Presets on <a href='/solver/presets'>their own page</a>; the pool numbers on <a href='/pool'>/pool</a>.")
    return _page(_form_html(f, presets) + results + POLL_JS, sub)


@router.get("/solver/estimate")
def estimate(request: Request):
    f = _form_from(request.query_params)
    preset = P.get(state.presets(), f.preset) or P.equilibrium()
    try:
        spot, hero, villain, _, _ = build_spot(f, preset)
        dry = bridge.dry_run(spot)
        locks, applied = P.locks_for(preset, dry.tree or [], spot.starting_pot, f.hero_oop,
                                     f.hero_lead, f.pot_type)
    except Exception as exc:                          # noqa: BLE001 - shown to the user
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"})
    gb = dry.memory_usage_bytes / 1e9
    used = dry.memory_usage_compressed_bytes / 1e9 if preset.compress_memory else gb
    return JSONResponse({
        "gb": gb, "compressed_gb": dry.memory_usage_compressed_bytes / 1e9,
        "budget_gb": preset.memory_budget_gb, "fits": used <= preset.memory_budget_gb,
        "nodes": len(dry.tree or []), "villain_nodes": len(applied), "locked": len(locks),
        "hero_pct": round(hero.pct), "villain_pct": round(villain.pct),
    })


@router.post("/solver/solve")
async def solve(request: Request):
    form = await request.form()
    f = _form_from(form)
    presets = state.presets()
    preset = P.get(presets, f.preset) or P.equilibrium()
    keys = [start(f, preset).key]
    if f.compare and preset.id != "equilibrium":
        eq = P.get(presets, "equilibrium") or P.equilibrium()
        # Same sizes as the preset so the two trees are the same tree.
        eq = replace(eq, hero_bets=preset.hero_bets, hero_raises=preset.hero_raises,
                     villain_bets=preset.villain_bets, villain_raises=preset.villain_raises,
                     donk_sizes=preset.donk_sizes, max_iterations=preset.max_iterations,
                     target_exploitability=preset.target_exploitability,
                     memory_budget_gb=preset.memory_budget_gb, compress_memory=preset.compress_memory)
        keys.append(start(f, eq).key)
    return RedirectResponse(f"/solver?{f.query()}&job={','.join(keys)}", status_code=303)


@router.get("/solver/job/{keys}", response_class=HTMLResponse)
def job_fragment(keys: str):
    return HTMLResponse(_results_block(keys.split(",")) or '<div id="results"></div>')


@router.get("/solver/job/{key}/node", response_class=HTMLResponse)
def job_node(key: str, path: str = ""):
    job = _jobs.get(key)
    if not job or job.status != "done":
        return HTMLResponse('<div class="note">no solve</div>')
    want = [p for p in path.split("|") if p]
    report = next((r for r in job.solution.reports if r["path"] == want), None)
    if report is None:
        return HTMLResponse('<div class="note">that node was not reported</div>')
    return HTMLResponse(_node_panel(job, report))


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------

def _sizes_field(name: str, label: str, sizes: dict[str, list[str]]) -> str:
    cells = "".join(
        f'<label><span>{label} {s}</span><input type="text" name="{name}_{s}" '
        f'value="{html.escape(", ".join(sizes.get(s, [])))}"></label>' for s in P.STREETS)
    return cells


def _policy_rows(preset: P.Preset) -> str:
    rows = []
    for i, r in enumerate(preset.policy):
        keys = ("bet", "check") if "bet" in r.mix or "check" in r.mix else ("fold", "call", "raise")
        mix = "".join(f'{k} <input type="number" step="any" min="0" max="1" name="r{i}_{k}" '
                      f'value="{r.mix.get(k, 0):g}"> ' for k in keys)
        rows.append(
            f'<tr class="{"" if r.lock else "off"}"><td><input type="checkbox" name="r{i}_lock"{" checked" if r.lock else ""}></td>'
            f'<td>{r.street}</td><td>{html.escape(dict(P.SITUATIONS).get(r.situation, r.situation))}</td>'
            f'<td>{r.pot_type}</td><td>{r.position}</td><td>{r.size}</td><td>{r.n:,}</td>'
            f'<td class="mixcell">{mix}</td><td class="note">{html.escape(r.basis)}</td>'
            f'<input type="hidden" name="r{i}_key" value="{html.escape("|".join((r.street, r.situation, r.pot_type, r.position, r.size)))}"></tr>')
    return "".join(rows)


def _preset_form(preset: P.Preset, presets: list[P.Preset]) -> str:
    sit_opts = "".join(f'<option value="{k}">{html.escape(v)}</option>' for k, v in P.SITUATIONS)
    return f"""
<form method="post" action="/solver/presets/{html.escape(preset.id)}" class="card">
<div class="form">
 <label><span>Name</span><input type="text" name="name" value="{html.escape(preset.name)}"></label>
 <label class="wide"><span>Description</span><input type="text" name="description" value="{html.escape(preset.description)}"></label>
 <label class="wide"><span>Basis (where the numbers come from)</span><input type="text" name="basis" value="{html.escape(preset.basis)}"></label>
</div>
<h2>Size menus (% of pot for bets, × the bet for raises; comma-separated, e.g. 33%, 75%)</h2>
<div class="note">Each size is a branch with a whole turn and river under it: two per street is the measured 2.5–5 GB flop tree, a third on every street is ~3× that. Use "Estimate tree" on the solver page.</div>
<div class="form">{_sizes_field("hero_bets", "hero bets", preset.hero_bets)}{_sizes_field("hero_raises", "hero raises", preset.hero_raises)}
{_sizes_field("villain_bets", "villain bets", preset.villain_bets)}{_sizes_field("villain_raises", "villain raises", preset.villain_raises)}
 <label><span>donk turn</span><input type="text" name="donk_turn" value="{html.escape(", ".join(preset.donk_sizes.get("turn", [])))}" placeholder="engine default"></label>
 <label><span>donk river</span><input type="text" name="donk_river" value="{html.escape(", ".join(preset.donk_sizes.get("river", [])))}" placeholder="engine default"></label>
</div>
<h2>Villain policy: the locked mix per node</h2>
<div class="note">A checked rule locks every villain node it matches (most specific rule wins). The mix is dealt strongest-first by equity on the board; <b>blend</b> mixes in a uniform version (0 = purely strength-ordered, 1 = every hand plays the same mix). Calibrated on showdowns: flop bets 28% air, turn 14%, river 5%.</div>
<div class="form">
 <label><span>blend flop</span><input type="number" step="any" min="0" max="1" name="blend_flop" value="{preset.blend.get("flop", 0.5):g}"></label>
 <label><span>blend turn</span><input type="number" step="any" min="0" max="1" name="blend_turn" value="{preset.blend.get("turn", 0.3):g}"></label>
 <label><span>blend river</span><input type="number" step="any" min="0" max="1" name="blend_river" value="{preset.blend.get("river", 0.1):g}"></label>
 <label><span>villain preflop width × (calls)</span><input type="number" step="any" min="0.2" max="3" name="ws_call" value="{preset.width_scale.get("call", 1.0):g}"></label>
 <label><span>villain preflop width × (raises)</span><input type="number" step="any" min="0.2" max="3" name="ws_raise" value="{preset.width_scale.get("raise", 1.0):g}"></label>
</div>
<table class="t pol"><tr><th>lock</th><th>street</th><th>situation</th><th>pot</th><th>pos</th><th>size faced</th><th>n</th><th>mix</th><th>basis</th></tr>
{_policy_rows(preset)}
<tr><td colspan="9" class="note">add a rule:
 <select name="new_street"><option value="any">any street</option><option>flop</option><option>turn</option><option>river</option></select>
 <select name="new_situation">{sit_opts}</select>
 <select name="new_pot"><option value="any">any pot</option><option>srp</option><option>3bet</option></select>
 <select name="new_pos"><option value="any">any pos</option><option>ip</option><option>oop</option></select>
 <select name="new_size"><option value="any">any size</option><option>small</option><option>mid</option><option>big</option></select>
 mix <input type="text" name="new_mix" placeholder="fold 0.5, call 0.42, raise 0.08 — or bet 0.6, check 0.4" style="width:280px"></td></tr>
</table>
<h2>Solver settings</h2>
<div class="form">
 <label><span>max iterations</span><input type="number" name="max_iterations" value="{preset.max_iterations}"></label>
 <label><span>target exploitability (% pot)</span><input type="number" step="any" name="target_exploitability" value="{preset.target_exploitability:g}"></label>
 <label><span>rake rate</span><input type="number" step="any" name="rake_rate" value="{preset.rake_rate:g}"></label>
 <label><span>memory budget (GB)</span><input type="number" step="any" name="memory_budget_gb" value="{preset.memory_budget_gb:g}"></label>
 <label><span>compress memory</span><select name="compress_memory"><option value="0"{"" if preset.compress_memory else " selected"}>no</option><option value="1"{" selected" if preset.compress_memory else ""}>yes (halves memory, 2–10× slower)</option></select></label>
 <label><span>add all-in threshold</span><input type="number" step="any" name="add_allin_threshold" value="{preset.add_allin_threshold:g}"></label>
 <label><span>force all-in threshold</span><input type="number" step="any" name="force_allin_threshold" value="{preset.force_allin_threshold:g}"></label>
 <label><span>merging threshold</span><input type="number" step="any" name="merging_threshold" value="{preset.merging_threshold:g}"></label>
</div>
<div class="btns">
 <button type="submit" name="op" value="save" class="primary">Save</button>
 <button type="submit" name="op" value="saveas">Save as new preset</button>
 {"" if preset.builtin else '<button type="submit" name="op" value="delete" class="f">Delete</button>'}
</div>
<div class="note" style="margin-top:.6rem">{"Built-in preset: saving keeps your edits until you rebuild the built-ins from the pool profile." if preset.builtin else "Your preset."}
 <a href="/solver?preset={html.escape(preset.id)}">use it on the solver page →</a></div>
</form>"""


@router.get("/solver/presets", response_class=HTMLResponse)
def presets_index():
    presets = state.presets()
    rows = "".join(
        f'<tr><td><a href="/solver/presets/{html.escape(p.id)}">{html.escape(p.name)}</a>'
        f'{" <span class=tag>built-in</span>" if p.builtin else ""}</td>'
        f'<td>{sum(1 for r in p.policy if r.lock and r.mix)} rules</td>'
        f'<td class="note">{html.escape(p.description)}</td></tr>' for p in presets)
    body = (f'<div class="card"><table class="t"><tr><th>preset</th><th>locks</th><th></th></tr>{rows}</table>'
            f'<form method="post" action="/solver/presets/rebuild" style="margin-top:1rem">'
            f'<button type="submit">Rebuild built-ins from the current pool profile</button> '
            f'<span class="note">replaces the built-in presets with fresh measurements; your own presets are kept</span></form></div>')
    return _page(body, "Every assumption a solve rests on, editable. Built-ins are generated from the measured pool.")


@router.get("/solver/presets/{pid}", response_class=HTMLResponse)
def preset_page(pid: str):
    presets = state.presets()
    preset = P.get(presets, pid)
    if preset is None:
        return RedirectResponse("/solver/presets", status_code=303)
    return _page(_preset_form(preset, presets), f"<a href='/solver/presets'>presets</a> › {html.escape(preset.name)}")


def _parse_sizes(text: str) -> list[str]:
    return [t.strip() for t in text.replace(";", ",").split(",") if t.strip()]


def _parse_mix(text: str) -> dict[str, float]:
    out = {}
    for part in text.replace(";", ",").split(","):
        bits = part.split()
        if len(bits) == 2:
            try:
                out[bits[0].strip().lower()] = float(bits[1])
            except ValueError:
                pass
    return out


@router.post("/solver/presets/rebuild")
def presets_rebuild():
    presets = P.regenerate_builtins(state.presets(), state.profile())
    P.save_all(presets)
    state.reset_presets()
    return RedirectResponse("/solver/presets", status_code=303)


@router.post("/solver/presets/{pid}")
async def preset_save(pid: str, request: Request):
    form = await request.form()
    presets = state.presets()
    preset = P.get(presets, pid)
    if preset is None:
        return RedirectResponse("/solver/presets", status_code=303)
    op = form.get("op", "save")
    if op == "delete":
        if not preset.builtin:
            P.save_all([p for p in presets if p.id != pid])
            state.reset_presets()
        return RedirectResponse("/solver/presets", status_code=303)

    g = lambda k, d="": (form.get(k) or d)                     # noqa: E731
    fl = lambda k, d: float(form.get(k) or d)                # noqa: E731
    policy = []
    i = 0
    while f"r{i}_key" in form:
        street, sit, pot, pos, size = form[f"r{i}_key"].split("|")
        old = preset.policy[i] if i < len(preset.policy) else P.Rule(street, sit)
        keys = ("bet", "check") if "bet" in old.mix or "check" in old.mix else ("fold", "call", "raise")
        mix = {k: fl(f"r{i}_{k}", 0) for k in keys}
        mix = {k: v for k, v in mix.items() if v > 0}
        policy.append(P.Rule(street, sit, pot, pos, size, old.n, mix, f"r{i}_lock" in form, old.basis))
        i += 1
    if g("new_mix").strip():
        mix = _parse_mix(g("new_mix"))
        if mix:
            policy.append(P.Rule(g("new_street", "any"), g("new_situation"), g("new_pot", "any"),
                                 g("new_pos", "any"), g("new_size", "any"), 0, mix, True, "added by hand"))
    updated = replace(
        preset, name=g("name", preset.name), description=g("description"), basis=g("basis"),
        hero_bets={s: _parse_sizes(g(f"hero_bets_{s}")) for s in P.STREETS},
        hero_raises={s: _parse_sizes(g(f"hero_raises_{s}")) for s in P.STREETS},
        villain_bets={s: _parse_sizes(g(f"villain_bets_{s}")) for s in P.STREETS},
        villain_raises={s: _parse_sizes(g(f"villain_raises_{s}")) for s in P.STREETS},
        donk_sizes={"turn": _parse_sizes(g("donk_turn")), "river": _parse_sizes(g("donk_river"))},
        policy=policy,
        blend={s: fl(f"blend_{s}", 0.3) for s in P.STREETS},
        width_scale={"call": fl("ws_call", 1.0), "raise": fl("ws_raise", 1.0)},
        max_iterations=int(fl("max_iterations", 300)),
        target_exploitability=fl("target_exploitability", 0.5),
        rake_rate=fl("rake_rate", 0.055), memory_budget_gb=fl("memory_budget_gb", 6.0),
        compress_memory=g("compress_memory") == "1",
        add_allin_threshold=fl("add_allin_threshold", 1.5),
        force_allin_threshold=fl("force_allin_threshold", 0.15),
        merging_threshold=fl("merging_threshold", 0.1),
    )
    if op == "saveas":
        base = P.slug(updated.name)
        new_id = base
        n = 2
        while P.get(presets, new_id):
            new_id = f"{base}-{n}"
            n += 1
        updated = replace(updated, id=new_id, builtin=False)
    P.save_all(P.upsert(presets, updated))
    state.reset_presets()
    return RedirectResponse(f"/solver/presets/{updated.id}", status_code=303)
