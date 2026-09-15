"""Weekly coaching report.

Structure is deliberate: a deterministic pass finds and ranks every claim,
and only then may prose be written about it. Nothing in the output originates
from a model's poker priors -- every number here traces to a row in the
database or to a chart with stated provenance. At NL5 a confident wrong
explanation costs more than no explanation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import duckdb

from ..ranges.buckets import bucket as bucket_of
from ..ranges.chart import Chart, available
from ..stats.core import NL5, summary
from ..stats.ranges import Observation, observations
from ..trainer.leakweight import (MIN_TREND_N, bucket_leaks, curve_is_flat,
                                  defence_curve)

#: A bucket needs this many observations in the window before it is discussed.
MIN_EVIDENCE = 20


@dataclass(slots=True)
class Finding:
    """One claim, with the evidence required to make it."""

    headline: str
    detail: str
    evidence: str
    bb_per_100: float | None
    n: int

    @property
    def rank(self) -> float:
        return abs(self.bb_per_100 or 0) * self.n


@dataclass(slots=True)
class Hand:
    hand_id: str
    played_at: datetime
    position: str
    cards: str
    net_bb: float
    reason: str


def _preflop_findings(obs: list[Observation], charts: list[Chart],
                      since: datetime, now: datetime) -> list[Finding]:
    out: list[Finding] = []
    recent = [o for o in obs if o.at >= since]
    for chart in charts:
        for leak in bucket_leaks(recent, chart, half_life=1e9, now=now):
            if leak.weight < MIN_EVIDENCE:
                continue
            verb = {"RFI": "opening", "vs_limp": "isolating", "vs_open": "3-betting",
                    "vs_3bet": "4-betting"}.get(chart.spot, "raising")
            out.append(Finding(
                headline=f"{leak.position}: {verb} {leak.bucket} too "
                         f"{'wide' if leak.gap > 0 else 'tight'}",
                detail=f"You raise {leak.observed:.0%} of these; the chart says "
                       f"{leak.prescribed:.0%}.",
                evidence=f"{leak.weight:.0f} hands in window · chart {chart.id} "
                         f"(confidence: {chart.confidence})",
                bb_per_100=None, n=int(leak.weight),
            ))
    return out


def _biggest_losses(con, since: datetime, until: datetime, limit: int = 10) -> list[Hand]:
    rows = con.execute("""
        SELECT hand_id, played_at, hero_pos, hero_cards, hero_net / bb, saw_flop, showdown
        FROM hands
        WHERE game_name = ? AND played_at >= ? AND played_at < ? AND hero_net < 0
        ORDER BY hero_net ASC LIMIT ?
    """, [NL5, since, until, limit]).fetchall()
    out = []
    for hid, at, pos, cards, net, flop, sd in rows:
        if not flop:
            reason = "lost preflop"
        elif sd:
            reason = "lost at showdown"
        else:
            reason = "folded after the flop"
        out.append(Hand(hid, at, pos or "?", cards or "??", round(net, 1), reason))
    return out


def _ev_section(con, since: datetime, until: datetime) -> list[str]:
    """Where the big blinds went, from stored solver verdicts."""
    from ..replay.grader import Store
    from ..stats import grades
    from ..web.dashboard import NODE_LABEL

    # Verdicts belong to hands; a store written against another database
    # (or a test's) must not leak into this report.
    known = {r[0] for r in con.execute("SELECT hand_id FROM hands").fetchall()}
    recs = [r for r in grades.in_window(Store().all(), since, until) if r.hand_id in known]
    if not recs:
        return ["## EV", "",
                "No graded decisions this week. `pokerlab grade --days 7` solves them; "
                "a flop node takes about half a minute, turns and rivers seconds.", ""]
    tot = grades.totals(recs)
    lines = ["## EV given up (solver-graded decisions)", "",
             f"- {tot.graded} decisions graded in {tot.hands} hands · {tot.usable} usable · "
             f"{tot.unstable} unsettled by the range band · {tot.untrusted} untrusted",
             f"- **{tot.loss_bb:.1f} bb given up**, "
             f"{tot.loss_per_100_hands:.1f} bb per 100 *graded* hands" if tot.loss_per_100_hands is not None else "",
             ""]
    for title, key, label in (("by street", lambda r: r.street.lower(), str),
                              ("by node", lambda r: r.node, lambda k: NODE_LABEL.get(k, k))):
        rows = [b for b in grades.by(recs, key) if b.usable]
        if not rows:
            continue
        lines += [f"**{title}**", "", "| | usable | mistakes | bb / decision | total |",
                  "|---|---|---|---|---|"]
        for b in rows:
            rate = "--" if b.mistake_rate is None else f"{b.mistake_rate:.0%}"
            per = "--" if b.loss_per_decision is None else f"{b.loss_per_decision:.2f}"
            lines.append(f"| {label(b.key)} | {b.usable} | {rate} | {per} | {b.loss_bb:.1f} bb |")
        lines.append("")
    worst = grades.biggest(recs, 5)
    if worst:
        lines += ["**Biggest mistakes**", "", "| hand | street | you did | solver prefers | cost |",
                  "|---|---|---|---|---|"]
        for r in worst:
            b = r.base
            best = max(b.ev, key=b.ev.get)
            lines.append(f"| `{r.hand_id[-6:]}` | {r.street.lower()} | {r.hero_step} | {best} | "
                         f"{r.loss_bb():.1f} bb |")
        lines.append("")
    lines += ["> Under " f"{grades.MIN_GRADED} usable decisions a bucket shows totals only. "
              "A verdict the tight/base/loose band could not settle is counted but never costed.", ""]
    return lines


def build(con: duckdb.DuckDBPyConnection, days: int = 7,
          now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    s = summary(con, since, now)

    lines = [f"# Poker report — {since.date()} to {now.date()}", ""]

    if not s["hands"]:
        lines += [f"No {NL5} hands in the last {days} days. Nothing to report.", ""]
        return "\n".join(lines)

    lines += [
        "## Results", "",
        f"- **{s['hands']:,} hands**, {s['net_eur']:+.2f} EUR ({s['bb100']:+.2f} bb/100)",
        f"- rake paid: {s['rake_eur']:.2f} EUR ({s['rake_bb100']:.1f} bb/100)",
        f"- VPIP {s['VPIP']} · PFR {s['PFR']} · 3bet {s['3bet']} · "
        f"WWSF {s['WWSF']} · WTSD {s['WTSD']} · W$SD {s['W$SD']}",
        "",
        f"> {s['hands']:,} hands is far too few to read the result as a verdict on how you "
        f"played. Treat the process stats above as the signal and the euro figure as noise.",
        "",
    ]

    obs = observations(con)
    findings = sorted(_preflop_findings(obs, list(available()), since, now),
                      key=lambda f: f.rank, reverse=True)

    lines += ["## Preflop", ""]
    if findings:
        for f in findings[:6]:
            lines += [f"**{f.headline}**", "", f"- {f.detail}", f"- _{f.evidence}_", ""]
    else:
        lines += [
            f"No preflop deviation cleared the evidence bar this week "
            f"(at least {MIN_EVIDENCE} hands in a bucket).",
            "",
            "That is a statement about sample size, not about your play. At this volume a "
            "week rarely contains enough of any one spot to judge it.",
            "",
        ]

    # Blind defence, over all history: a week never holds enough of these.
    vs_open = next((c for c in available() if c.spot == "vs_open"), None)
    if vs_open:
        cells = defence_curve(obs, vs_open)
        if len(cells) >= 3:
            lines += ["## Blind defence (all history, not just this week)", "",
                      "| opener | you defend | reference | gap | n |", "|---|---|---|---|---|"]
            for c in cells:
                lines.append(f"| vs {c.opener} | {c.observed:.0%} | {c.prescribed:.0%} | "
                             f"{c.gap:+.0%} | {c.n} |")
            lines.append("")
            if curve_is_flat(cells):
                lo = min(cells, key=lambda c: c.prescribed)
                hi = max(cells, key=lambda c: c.prescribed)
                lines += [
                    f"**Your defence barely moves with the opener's seat.** It should widen "
                    f"sharply from early to late — the reference goes {lo.prescribed:.0%} "
                    f"(vs {lo.opener}) to {hi.prescribed:.0%} (vs {hi.opener}); yours spans "
                    f"only {min(c.observed for c in cells):.0%}–"
                    f"{max(c.observed for c in cells):.0%}.",
                    "",
                    "That defence should widen as the opener's position gets later is ordinary "
                    "poker theory, not a number this tool invented — so this finding survives "
                    "even though the absolute levels in the chart are the least reliable part "
                    "of it.",
                    "",
                ]

    lines += _ev_section(con, since, now)

    losses = _biggest_losses(con, since, now)
    if losses:
        lines += ["## Biggest losses", "",
                  "| hand | pos | cards | bb | ended |", "|---|---|---|---|---|"]
        for h in losses:
            lines.append(f"| `{h.hand_id[-6:]}` | {h.position} | {h.cards} | "
                         f"{h.net_bb:+.1f} | {h.reason} |")
        lines += ["",
                  "> These are the largest *losses*, which is not the same as the largest "
                  "*mistakes* — a correct call that loses belongs in this table too. The "
                  "mistakes are in the EV section above, where the week has graded hands.", ""]

    lines += ["---", "",
              "_Every number above comes from your hand histories or from a chart whose "
              "provenance is recorded in the tool. Nothing here is a model's opinion._", ""]
    return "\n".join(lines)
