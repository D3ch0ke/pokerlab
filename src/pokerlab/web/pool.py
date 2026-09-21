"""The pool page: who sits at your NL5 tables and what they do at every node,
with the sample behind each number. This is what the solver presets are
generated from, so the assumptions a pool-locked solve makes can be read here
before they are trusted.
"""

from __future__ import annotations

import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..solver import presets as P
from ..stats.pool_profile import SIZE_BUCKETS
from . import state
from .layout import page, pct

router = APIRouter()

CSS = """
table.t{border-collapse:collapse;font-size:.8rem;font-variant-numeric:tabular-nums;width:100%}
table.t th{text-align:left;font-weight:600;color:var(--dim);font-size:.7rem;padding:.25rem .5rem;border-bottom:1px solid var(--line)}
table.t td{padding:.28rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}
.hb{display:inline-block;height:9px;border-radius:2px;background:var(--accent);vertical-align:middle;margin-right:.35rem}
.over{color:var(--raise);font-weight:600} .under{color:var(--fold);font-weight:600}
details summary{cursor:pointer;color:var(--accent);font-size:.8rem}
.members{font-size:.76rem;color:var(--dim);line-height:1.6}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin-bottom:1rem}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.7rem .9rem}
.tile b{display:block;font-size:1.15rem} .tile span{font-size:.72rem;color:var(--dim)}
"""


def _bar(x: float | None, w: int = 70) -> str:
    if x is None:
        return ""
    return f'<i class="hb" style="width:{int(w * max(0.0, min(1.0, x)))}px"></i>'


def _tiles(p) -> str:
    regs = next((t for t in p.tiers if t.tier.startswith("regular")), None)
    items = [
        (f"{p.hands:,}", "NL5 hands with a flop"),
        (f"{p.villains:,}", "opponents seen"),
        (f"{regs.players if regs else 0}", "regulars (100+ hands)"),
        (f"{p.silhouette:.2f}" if p.silhouette is not None else "—", "archetype silhouette (weak: a continuum)"),
        (p.built_at[:10], "measured on"),
    ]
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><b>{html.escape(v)}</b><span>{html.escape(l)}</span></div>' for v, l in items) + "</div>"


def _tiers(p) -> str:
    rows = "".join(
        f'<tr><td>{html.escape(t.tier)}</td><td>{t.players:,}</td><td>{_bar(t.share)}{t.share:.0%}</td>'
        f'<td>{pct(t.vpip)}</td><td>{pct(t.pfr)}</td><td>{pct(t.limp)}</td><td>{pct(t.cbet)}</td>'
        f'<td>{pct(t.fold_cbet)}</td><td>{pct(t.fold_turn)}</td><td>{pct(t.fold_river)}</td>'
        f'<td>{pct(t.afq)}</td><td>{t.hero_bb100:+.0f} <span class="note">({t.hero_hands:,})</span></td></tr>'
        for t in p.tiers)
    return (f'<table class="t"><tr><th>players by hands seen</th><th>players</th><th>share of villain-hands</th>'
            f'<th>VPIP</th><th>PFR</th><th>limp</th><th>c-bet</th><th>fold to c-bet</th><th>fold turn</th>'
            f'<th>fold river</th><th>aggression</th><th>hero bb/100 with one at the table (hands)</th></tr>{rows}</table>'
            f'<div class="note">The transient players cannot be read individually but are a distinct population: '
            f'far looser preflop, more c-bets, fewer folds. A model built from the regulars alone is too tight.</div>')


def _archetypes(p) -> str:
    if not p.archetypes:
        return '<div class="note">Clustering needs numpy and scikit-learn in the venv.</div>'
    out = []
    for a in p.archetypes:
        c = a.centroid
        fs = a.flop_fold_by_size
        rs = a.river_fold_by_size

        def f(d, k):
            v = d.get(k)
            return f"{v[0]:.0%} <span class='note'>({v[1]})</span>" if v else "—"

        pid = f"vs-{P.slug(a.name)}"
        out.append(
            f'<tr><td><b>{html.escape(a.name)}</b><br><span class="note">{a.players} players · '
            f'<a href="/solver/presets/{pid}">preset</a></span></td>'
            f'<td>{_bar(a.share)}{a.share:.0%}</td>'
            f'<td>{pct(c["vpip"])} / {pct(c["pfr"])}</td><td>{pct(c["threebet"])}</td><td>{pct(c["limp"])}</td>'
            f'<td>{pct(c["afq"])}</td><td>{pct(c["cbet"])}</td>'
            f'<td>{f(fs, "small")} · {f(fs, "mid")} · {f(fs, "big")}</td>'
            f'<td>{a.turn_fold[0]:.0%} <span class="note">({a.turn_fold[1]})</span></td>'
            f'<td>{f(rs, "small")} · {f(rs, "mid")} · {f(rs, "big")}</td>'
            f'<td>{pct(c["wtsd"])}</td><td>{c["bb100"]:+.0f}</td></tr>'
            f'<tr><td colspan="12"><details><summary>members</summary><div class="members">'
            f'{", ".join(f"<a href=/villains/{html.escape(m)}>{html.escape(m)}</a>" for m in a.members)}'
            f'</div></details></td></tr>')
    return (f'<table class="t"><tr><th>archetype</th><th>share of regular-hands</th><th>VPIP / PFR</th>'
            f'<th>3-bet</th><th>limp</th><th>aggr.</th><th>c-bet</th><th>fold to c-bet: small · mid · big (n)</th>'
            f'<th>fold turn</th><th>fold river: small · mid · big</th><th>WTSD</th><th>their bb/100</th></tr>'
            f'{"".join(out)}</table>'
            f'<div class="note">k-means on VPIP, PFR, 3-bet, limp, aggression, fold-to-c-bet, WTSD and c-bet over '
            f'regulars with 100+ hands, hands-weighted. Silhouette {p.silhouette:.2f}: these are regions of a '
            f'continuum, not distinct types — read the numbers, not the names. Their bb/100 is against the whole '
            f'table and survivorship-biased (players who stay are the ones who win).</div>')


def _nodes(p) -> str:
    labels = {"cbet": "c-bet (lead, flop)", "vs_cbet": "vs c-bet", "barrel": "barrel (lead, turn)",
              "vs_barrel": "vs turn barrel", "river_bet": "river bet (lead)", "vs_river_bet": "vs lead's river bet",
              "donk": "donk (OOP, no lead, first)", "stab": "stab (lead checked)", "vs_stab": "lead vs stab",
              "vs_raise": "bet, then raised", "nolead_bet": "bet, nobody leads", "vs_nolead_bet": "vs bet, nobody leads"}
    rows = []
    for node in p.nodes:
        if node.position == "any" or node.n < 30:
            continue
        mix = " · ".join(f"{k} {v:.0%}" for k, v in node.mix.items())
        size = (f"{100 * node.size[0]:.0f} / {100 * node.size[1]:.0f} / {100 * node.size[2]:.0f}%"
                if node.size else "")
        rx = f"{node.raise_x:.1f}×" if node.raise_x else ""
        rows.append(f'<tr><td>{labels.get(node.node, node.node)}</td><td>{node.pot_type}</td>'
                    f'<td>{node.position}</td><td>{node.n:,}</td><td>{mix}</td><td>{size}</td><td>{rx}</td></tr>')
    return (f'<table class="t"><tr><th>node</th><th>pot</th><th>villain</th><th>n</th><th>mix</th>'
            f'<th>bet size q25 / median / q75</th><th>raise</th></tr>{"".join(rows)}</table>'
            f'<div class="note">Villain decisions in heads-up pots only, all 11 months. "lead" is the last '
            f'aggressor of the previous street (the preflop raiser on the flop).</div>')


def _sizes(p) -> str:
    rows = []
    for street in ("FLOP", "TURN", "RIVER"):
        for s in p.size_response:
            if s.street != street:
                continue
            gap = s.fold - s.mdf_fold
            cls = "over" if gap > 0.03 else "under" if gap < -0.03 else ""
            rows.append(f'<tr><td>{street.lower()}</td><td>{s.bucket}% pot</td><td>{s.n:,}</td>'
                        f'<td>{_bar(s.fold)}{s.fold:.0%}</td><td>{s.call:.0%}</td><td>{s.raise_:.0%}</td>'
                        f'<td>{s.mdf_fold:.0%}</td><td class="{cls}">{100 * gap:+.0f} pts</td></tr>')
    dist = " · ".join(f"{k}% {v:.0%}" for k, v in p.cbet_sizes.items())
    return (f'<table class="t"><tr><th>street</th><th>size faced</th><th>n</th><th>fold</th><th>call</th>'
            f'<th>raise</th><th>fold allowed by MDF</th><th>over-fold</th></tr>{"".join(rows)}</table>'
            f'<div class="note">Villain facing the lead\'s bet, heads-up. "Fold allowed" is 1 − MDF at the bucket '
            f'midpoint: folding more than that makes a bet with no equity profitable. The pool\'s own flop c-bet '
            f'sizes in single-raised pots: {dist}.</div>')


def _composition(p) -> str:
    rows = "".join(
        f'<tr><td>{c.street.lower()}</td><td>{html.escape(c.action)}</td><td>{c.n:,}</td>'
        f'<td>{_bar(c.air)}{c.air:.0%}</td><td>{c.pair:.0%}</td><td>{c.strong:.0%}</td></tr>'
        for c in p.composition)
    return (f'<table class="t"><tr><th>street</th><th>villain action</th><th>n shown</th><th>no pair</th>'
            f'<th>one pair</th><th>two pair +</th></tr>{rows}</table>'
            f'<div class="note">Hands villains later showed down, so biased towards hands that kept paying. '
            f'Read as how strength-ordered each action is: this sets the per-street blend of the solver locks '
            f'(river bets are almost never air; flop checks are half air).</div>')


def _quarters(p) -> str:
    rows = "".join(
        f'<tr><td>{q["quarter"][:7]}</td><td>{q["n"]:,}</td><td>{pct(q["fold_cbet"])}</td><td>{pct(q["cbet"])}</td>'
        f'<td>{pct(q["fold_turn"])}</td><td>{pct(q["fold_river"])}</td><td>{pct(q["raise_flop"])}</td></tr>'
        for q in p.quarters)
    return (f'<table class="t"><tr><th>quarter</th><th>n vs c-bet</th><th>fold to c-bet</th><th>c-bet</th>'
            f'<th>fold turn</th><th>fold river</th><th>raise flop bet</th></tr>{rows}</table>'
            f'<div class="note">The pool drifts little quarter to quarter, which is why the profile uses all 11 months.</div>')


@router.get("/pool", response_class=HTMLResponse)
def pool_page(refresh: int = 0):
    if refresh:
        from ..stats.pool_profile import load
        with state._lock:
            state._state["profile"] = load(state.con(), refresh=True)
    p = state.profile()
    body = (_tiles(p)
            + '<div class="card"><h2>Who is at the table</h2>' + _tiers(p) + "</div>"
            + '<div class="card"><h2>Regular archetypes</h2>' + _archetypes(p) + "</div>"
            + '<div class="card"><h2>Fold, call, raise by the size faced</h2>' + _sizes(p) + "</div>"
            + '<div class="card"><h2>What the pool does at each node</h2>' + _nodes(p) + "</div>"
            + '<div class="card"><h2>What villains show when they act</h2>' + _composition(p) + "</div>"
            + '<div class="card"><h2>Stability over the year</h2>' + _quarters(p) + "</div>"
            + '<div class="note"><a href="/pool?refresh=1">re-measure now</a> · the solver presets built from '
            'this are on <a href="/solver/presets">/solver/presets</a>; the write-up is in '
            '<code>reports/pool-research-2026-09-22.md</code>.</div>')
    return page(body, "Pool", "/pool",
                "The NL5 population as measured: who they are, what they do at each node, and what they "
                "show when they do it. Every number carries its n.", extra_css=CSS, wide=True)
