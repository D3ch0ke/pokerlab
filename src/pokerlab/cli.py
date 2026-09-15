"""pokerlab command line."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from rich.console import Console
from rich.table import Table

from .db.load import DEFAULT_DB, build
from .bankroll import (LADDER, MOVE_DOWN_BUYINS, MOVE_UP_BUYINS, bust_before_target,
                       measure, readiness, risk_of_ruin)
from .ranges.chart import load as load_chart
from .stats.core import DEFAULT_WINDOW_DAYS, EPOCH, FOREVER, NL5, by_position, monthly, summary, window
from .stats.ranges import observations
from .trainer.leakweight import bucket_leaks, leak_trend

console = Console()


def _spark(values: list[float | None]) -> str:
    ticks = "▁▂▃▄▅▆▇█"
    real = [v for v in values if v is not None]
    if not real:
        return ""
    lo, hi = min(real), max(real)
    span = (hi - lo) or 1
    return "".join(" " if v is None else ticks[min(7, int(7 * (v - lo) / span))] for v in values)


def cmd_import(args) -> int:
    stats = build(db_path=Path(args.db))
    console.print(f"[green]imported[/] {stats['hands']} hands, {stats['actions']} actions "
                  f"-> {args.db}")
    if stats["quarantined"]:
        console.print(f"[yellow]quarantined {stats['quarantined']} "
                      f"({stats['coverage']}% coverage)[/]")
    else:
        console.print(f"[green]100% parse coverage[/], nothing quarantined")
    return 0


def cmd_audit(args) -> int:
    con = duckdb.connect(args.db, read_only=True)
    since, until = (EPOCH, FOREVER) if args.all else window(args.days)
    s = summary(con, since, until)

    if not s["hands"]:
        console.print(f"[yellow]no {NL5} hands between {s['since']} and {s['until']}[/]")
        return 1

    scope = "all time" if args.all else f"last {args.days} days"
    console.rule(f"NL5 audit — {scope} ({s['since']} → {s['until']})")

    money = Table(box=None, pad_edge=False)
    money.add_column("", style="dim")
    money.add_column("", justify="right")
    money.add_row("hands", f"{s['hands']:,}")
    colour = "green" if s["bb100"] > 0 else "red"
    money.add_row("net", f"[{colour}]€{s['net_eur']:+.2f}  ({s['bb100']:+.2f} bb/100)[/]")
    money.add_row("rake paid", f"€{s['rake_eur']:.2f}  ({s['rake_bb100']:.1f} bb/100)")
    money.add_row("gross before rake", f"{s['bb100'] + s['rake_bb100']:.2f} bb/100")
    console.print(money)

    core = Table(title="core stats", title_justify="left")
    for name in ("VPIP", "PFR", "3bet", "fold_to_3bet", "limp", "WWSF", "WTSD", "W$SD"):
        core.add_column(name, justify="right")
    core.add_row(*(str(s[n]) for n in
                   ("VPIP", "PFR", "3bet", "fold_to_3bet", "limp", "WWSF", "WTSD", "W$SD")))
    console.print(core)

    pos = Table(title="by position", title_justify="left")
    for col in ("pos", "hands", "VPIP", "PFR", "3bet", "limp", "net €"):
        pos.add_column(col, justify="right")
    for p, n, vpip, pfr, tb, limp, net in by_position(con, since, until):
        pos.add_row(p, f"{n:,}", f"{vpip}%", f"{pfr}%",
                    f"{tb}%" if tb is not None else "--", f"{limp}%",
                    f"[{'green' if net > 0 else 'red'}]{net:+.2f}[/]")
    console.print(pos)

    rows = monthly(con)
    trend = Table(title="monthly trend — the point is the shape, not the mean",
                  title_justify="left")
    for col in ("month", "hands", "VPIP", "PFR", "bb/100"):
        trend.add_column(col, justify="right")
    for month, n, vpip, pfr, bb100 in rows:
        # Months under the threshold are dimmed rather than hidden: the volume
        # itself is information, the rates from it are not.
        cell = (lambda v: f"[dim]{v}[/dim]") if n < 100 else str
        trend.add_row(cell(month), cell(f"{n:,}"), cell(f"{vpip}%"),
                      cell(f"{pfr}%"), cell(f"{bb100:+.1f}"))
    console.print(trend)
    console.print(f"  VPIP {_spark([r[2] for r in rows])}   "
                  f"bb/100 {_spark([r[4] for r in rows])}")
    console.print("[dim]  months under 100 hands are dimmed: too few to read as change[/]")
    return 0


def cmd_leaks(args) -> int:
    con = duckdb.connect(args.db, read_only=True)
    chart = load_chart(args.chart)
    obs = observations(con)
    rfi = [o for o in obs if o.spot == chart.spot and o.position in chart.ranges]

    console.rule(f"preflop leaks — {chart.label}")
    console.print(f"[dim]graded against '{chart.id}' (confidence: {chart.confidence}, "
                  f"assumption: {chart.assumption})[/]")
    console.print(f"[dim]{chart.source}[/]\n")

    blended = Table(title=f"all {len(rfi):,} {chart.spot.replace('_', ' ')} decisions — blended across 11 months",
                    title_justify="left")
    for col in ("pos", "bucket", "you", "chart", "verdict", "n"):
        blended.add_column(col, justify="right")
    for leak in bucket_leaks(obs, chart, half_life=1e9)[:12]:
        colour = "red" if leak.mass > 20 else "yellow"
        blended.add_row(leak.position, leak.bucket, f"{leak.observed:.0%}",
                        f"{leak.prescribed:.0%}", f"[{colour}]{leak.kind}[/]", f"{leak.weight:.0f}")
    console.print(blended)
    console.print("[dim]  blended = includes hands from when you played very differently "
                  "(VPIP was 49% in Oct 2025). Treat as history, not as a diagnosis of today.[/]\n")

    trends = leak_trend(obs, chart, split_days=args.days)
    trend = Table(title=f"then vs now (split at {args.days} days) — only buckets with "
                        f"enough data in BOTH halves", title_justify="left")
    for col in ("bucket", "chart", "then", "now", "verdict", "trend", "n"):
        trend.add_column(col, justify="right")
    for t in trends:
        trend.add_row(t.bucket, f"{t.chart:.0%}", f"{t.early:.0%}", f"{t.recent:.0%}",
                      t.kind, t.trend, f"{t.early_n}/{t.recent_n}")
    console.print(trend)

    recent = len([o for o in rfi if (datetime.now(timezone.utc) - o.at).days <= args.days])
    console.print(
        f"\n[yellow]Underpowered.[/] Only {recent} {chart.spot.replace('_', ' ')} decisions in the last {args.days} days, "
        f"spread over ~15 hand buckets. Most buckets cannot be judged on recent play at all —\n"
        f"they are omitted above rather than guessed at. Roughly 2,000 hands a month would make "
        f"this section diagnostic; right now the audit is the more informative report.")
    return 0


def cmd_bankroll(args) -> int:
    con = duckdb.connect(args.db, read_only=True)
    stake = next(s for s in LADDER if s.name == args.stake)
    rows = con.execute("""
        SELECT h.hero_net / ?, s.stack / ? FROM hands h JOIN seats s USING (hand_id)
        WHERE h.game_name = ? AND s.is_hero ORDER BY h.played_at
    """, [stake.bb_eur * 100, stake.bb_eur * 100, NL5]).fetchall()
    net = [r[0] for r in rows]
    m = measure(net, [r[1] for r in rows])
    roll = args.roll

    console.rule(f"bankroll — measured from your own {m.hands:,} {stake.name} hands")

    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim"); t.add_column("", justify="right")
    t.add_row("winrate", f"{m.winrate:+.2f} bb/100")
    t.add_row("std dev", f"{m.stdev:.0f} bb/100  [dim](typical 6-max is ~100)[/]")
    t.add_row("avg stack", f"{m.avg_stack:.0f} bb  [dim](auto-rebuy floors you at 100bb; "
                           f"the excess is pots you won, not a buy-in choice)[/]")
    t.add_row("worst drawdown", f"{m.max_drawdown:.0f} bb = €{m.max_drawdown * stake.bb_eur:.2f} "
                                f"({m.max_drawdown / 100:.1f} buy-ins)")
    console.print(t)

    lo, hi = m.ci
    console.print(f"\n[bold]95% confidence interval on your winrate: {lo:+.1f} to {hi:+.1f} bb/100[/]")
    if not m.proven_winner:
        console.print(
            f"[yellow]That interval includes zero, so this sample cannot yet distinguish you "
            f"from a break-even or losing player.[/]\n"
            f"[dim]Pinning it to ±5 bb/100 would take about {m.hands_to_resolve():,} hands. "
            f"That is not a criticism of your play — it is how variance works at "
            f"{m.stdev:.0f} bb/100.[/]")

    cycle = Table(title="your current approach: deposit €10, withdraw at €20",
                  title_justify="left")
    for c in ("deposit", "in big blinds", "P(bust before doubling)"):
        cycle.add_column(c, justify="right")
    for dep in (10, 20, 30):
        roll_bb = dep / stake.bb_eur
        p = bust_before_target(net, roll_bb, 2 * roll_bb)
        cycle.add_row(f"€{dep}", f"{roll_bb:.0f} bb", f"[{'red' if p > 0.4 else 'yellow'}]{p:.0%}[/]")
    console.print(cycle)

    ror = Table(title="risk of ruin if you never withdraw", title_justify="left")
    for c in ("buy-ins", f"bankroll at {stake.name}", "risk of ruin"):
        ror.add_column(c, justify="right")
    for bi in (10, 20, 40, 60, 100):
        p = risk_of_ruin(m.winrate, m.stdev, bi * 100)
        ror.add_row(f"{bi}", f"€{bi * stake.buyin:.0f}",
                    f"[{'red' if p > 0.3 else 'green' if p < 0.1 else 'yellow'}]{p:.0%}[/]")
    console.print(ror)
    console.print("[dim]  computed at your measured winrate. If your true winrate is at the "
                  "bottom of the interval above, every one of these is 100%.[/]\n")

    nxt = LADDER[min(LADDER.index(stake) + 1, len(LADDER) - 1)]
    r = readiness(m, roll, nxt, m.hands)
    check = Table(title=f"move up to {nxt.name}? (strict criteria — all must pass)",
                  title_justify="left")
    check.add_column(""); check.add_column("criterion"); check.add_column("you", justify="right")
    for name, (ok, detail) in r.checks.items():
        check.add_row("[green]PASS[/]" if ok else "[red]FAIL[/]", name, detail)
    console.print(check)
    console.print(f"[dim]  move-down rule: drop back to {stake.name} the moment you fall below "
                  f"{MOVE_DOWN_BUYINS} buy-ins ({MOVE_DOWN_BUYINS * nxt.buyin:.0f}€). "
                  f"The move-down rule is what prevents busting, not the move-up one.[/]")
    return 0


def cmd_train(args) -> int:
    from .web.app import serve
    console.print(f"[green]preflop trainer[/] on http://127.0.0.1:{args.port}  (ctrl-c to stop)")
    serve(Path(args.db), args.port)
    return 0


def cmd_replay(args) -> int:
    from .web.app import serve
    where = f"http://127.0.0.1:{args.port}/replay"
    console.print(f"[green]hand replayer[/] on {where}  (ctrl-c to stop)")
    console.print("[dim]  browse your hands, step through one, and solve the node "
                  "you are standing on against a band of villain ranges[/]")
    serve(Path(args.db), args.port)
    return 0


def cmd_coach(args) -> int:
    from .coach.report import build as build_report
    con = duckdb.connect(args.db, read_only=True)
    md = build_report(con, days=args.days)
    if args.out:
        Path(args.out).write_text(md)
        console.print(f"[green]written[/] {args.out}")
    else:
        console.print(md, markup=False, highlight=False)
    return 0


def cmd_volume(args) -> int:
    from .stats.volume import monthly, project
    con = duckdb.connect(args.db, read_only=True)
    rows = monthly(con)
    console.rule("volume — hands, hours and tables")
    t = Table()
    for c in ("month", "hands", "hours", "hands/hr", "avg tables", "max", "€", "bb/100"):
        t.add_column(c, justify="right")
    for mo, hands, hours, hph, avg_tab, max_tab, eur, bb100 in rows:
        faint = "dim" if hands < 300 else ""
        w = (lambda v: f"[{faint}]{v}[/{faint}]") if faint else str
        t.add_row(w(mo), w(f"{hands:,}"), w(f"{hours:.1f}"), w(f"{hph:.0f}"),
                  w(f"{avg_tab:.2f}"), w(str(max_tab)),
                  w(f"{eur:+.2f}"), w(f"{bb100:+.1f}"))
    console.print(t)

    # The hands/hr column is the aggregate across however many tables were
    # open, so it must be divided by concurrency to get a per-table rate.
    recent = [r for r in rows if r[1] >= 300][-6:]
    rate = (sum(r[3] / r[4] for r in recent) / len(recent)) if recent else 80
    agg = sum(r[3] for r in recent) / len(recent) if recent else 0
    console.print(f"\n[dim]measured {rate:.0f} hands/hour/table "
                  f"({agg:.0f}/hour aggregate at your recent {sum(r[4] for r in recent) / len(recent):.1f} "
                  f"tables)[/]")
    p = Table(title=f"projection at {args.hours}h/day on {args.tables} tables",
              title_justify="left")
    for c in ("days/week", "hands/month", "hands/year", "months to 25k"):
        p.add_column(c, justify="right")
    for d in (3, 5, 7):
        pr = project(rate, args.hours, args.tables, d)
        p.add_row(str(d), f"{pr['per_month']:,}", f"{pr['per_year']:,}",
                  f"{25_000 / pr['per_month']:.1f}")
    console.print(p)
    console.print("[dim]  adding tables costs bb/100 for almost everyone. Ramp one at a time "
                  "and re-run `pokerlab audit` after ~5k hands at each step —\n"
                  "  if your winrate drops more than a few bb/100, you have found your cap.[/]")
    return 0


def cmd_solve(args) -> int:
    from .solver.bridge import Spot, available, solve
    if not available():
        console.print("[red]solver not built.[/] Run: cd solver-cli && cargo build --release")
        return 1
    spot = Spot(
        oop_range=args.oop, ip_range=args.ip, board=tuple(args.board.split()),
        starting_pot=args.pot, effective_stack=args.stack,
        bet_sizes={s: [args.bet] for s in ("flop", "turn", "river")},
        raise_sizes={s: ["2.5x"] for s in ("flop", "turn", "river")},
        max_iterations=args.iterations,
    )
    sol = solve(spot, timeout=args.timeout)

    colour = "green" if sol.trustworthy else "red"
    console.rule(f"solve — {args.board}")
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim"); t.add_column("", justify="right")
    t.add_row("exploitability", f"[{colour}]{sol.exploitability_pct_pot:.3f}% of pot[/]")
    t.add_row("iterations", f"{sol.iterations}")
    t.add_row("EV", f"OOP {sol.oop_ev:.2f} / IP {sol.ip_ev:.2f}")
    t.add_row("memory", f"{sol.memory_usage_bytes / 1e6:.0f} MB in {sol.elapsed_ms} ms")
    console.print(t)

    if not sol.trustworthy:
        console.print("[red]NOT TRUSTWORTHY[/] — do not read the strategy below as solved:")
        for w in sol.warnings or ["did not converge to the exploitability target"]:
            console.print(f"  [red]•[/] {w}")

    strat = Table(title=f"root strategy ({sol.root_player} to act)", title_justify="left")
    strat.add_column("hand")
    for a in sol.root_actions:
        strat.add_column(a, justify="right")
    for row in sol.root_strategy[:args.rows]:
        strat.add_row(row["hand"], *(f"{row['actions'].get(a, 0):.0%}" for a in sol.root_actions))
    console.print(strat)
    console.print(f"[dim]  showing {min(args.rows, len(sol.root_strategy))} of "
                  f"{len(sol.root_strategy)} combos[/]")
    return 0


def cmd_sessions(args) -> int:
    from .stats.sessions import detectable_shift, sessions, tilt_tests
    con = duckdb.connect(args.db, read_only=True)
    sess = sessions(con)
    console.rule(f"sessions — {len(sess)} reconstructed (30 min gap)")

    lengths = sorted(s.hands for s in sess)
    console.print(f"[dim]median {lengths[len(lengths) // 2]} hands · "
                  f"{sum(1 for s in sess if s.hands >= 50)} sessions of 50+ hands[/]\n")

    tests = tilt_tests(sess, min_peak=args.peak)
    if not tests:
        console.print(f"[yellow]Not enough sessions peaking at +{args.peak}bb to test.[/]")
        return 0

    t = Table(title=f'"I climb then give it back" — tested against your own hands reshuffled',
              title_justify="left")
    for c in ("statistic", "you", "null median", "null 90% range", "percentile"):
        t.add_column(c, justify="right")
    for r in tests:
        colour = "red" if r.significant else "green"
        t.add_row(r.name, f"{r.observed:.0f}", f"{r.null_median:.0f}",
                  f"{r.null_lo:.0f} .. {r.null_hi:.0f}",
                  f"[{colour}]{r.percentile:.0f}%[/]")
    console.print(t)

    if any(r.significant for r in tests):
        console.print("\n[red]At least one statistic falls outside chance.[/] "
                      "Worth investigating.")
    else:
        console.print(
            "\n[green]Every statistic sits inside what chance produces.[/] Giving back part "
            "of a peak is not evidence of tilt — a peak is by definition a maximum, so play\n"
            "after it regresses even with no change in how you play. The sessions where you "
            "gave it back are simply the memorable ones.")
    n = sum(s.hands for s in sess)
    thr = tests[0].threshold
    console.print(f"[dim]  null model: {n:,} of your own hands resampled, session lengths kept, "
                  f"order destroyed. Flagged only outside {thr:.1f}%-{100 - thr:.1f}% —\n"
                  f"  Bonferroni-corrected for the {len(tests)} statistics tested, since an "
                  f"uncorrected 5%/95% would flag ~40% of players doing nothing wrong.[/]")
    return 0


def cmd_allin(args) -> int:
    from .solver.bridge import SolverError
    from .stats.allin import collect_allins
    try:
        summ = collect_allins()
    except SolverError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    if not summ.n:
        console.print("[yellow]no evaluable all-in spots found[/]")
        return 0

    console.rule("all-in equity — were you ahead when the money went in?")
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim"); t.add_column("", justify="right")
    t.add_row("spots evaluated", f"{summ.n}")
    t.add_row("average equity", f"{summ.avg_equity:.1%}")
    t.add_row("expected wins", f"{summ.expected_wins:.1f}")
    t.add_row("actual wins", f"{summ.actual_wins}")
    colour = "green" if summ.luck > 0 else "red"
    t.add_row("luck", f"[{colour}]{summ.luck:+.1f} all-ins ({summ.sigmas:+.1f} sigma)[/]")
    console.print(t)

    worst = sorted(summ.spots, key=lambda s: s.equity - (1 if s.won else 0), reverse=True)[:args.rows]
    w = Table(title="biggest run-bad: highest equity, still lost", title_justify="left")
    for c in ("hand", "street", "yours", "villain", "board", "equity", "bb"):
        w.add_column(c, justify="right")
    for s in (x for x in worst if not x.won):
        w.add_row(s.hand_id[-6:], s.street, "".join(s.hero), "".join(s.villain),
                  " ".join(s.board), f"{s.equity:.0%}", f"{s.net_bb:+.0f}")
    console.print(w)
    console.print(
        f"[dim]  Heads-up postflop all-ins that reached showdown only — multiway spots and "
        f"all-ins nobody called are not here.\n  {summ.n} spots is a small sample: at this "
        f"size roughly ±{1.96 * (summ.n * 0.25) ** 0.5:.0f} all-ins of swing is ordinary.[/]")
    return 0


def cmd_postflop(args) -> int:
    from .stats.postflop import by_street, by_texture
    con = duckdb.connect(args.db, read_only=True)
    since, until = (EPOCH, FOREVER) if args.all else window(args.days)
    scope = "all time" if args.all else f"last {args.days} days"
    console.rule(f"postflop — {scope}")

    t = Table(title="by street", title_justify="left")
    for c in ("street", "hands", "c-bet", "fold to c-bet", "raise vs c-bet",
              "donk", "stab", "AF"):
        t.add_column(c, justify="right")
    rows = by_street(con, since, until)
    for s in rows:
        t.add_row(*(str(x) for x in s.row()))
    console.print(t)
    console.print("[dim]  donk = you bet before the preflop aggressor acts · "
                  "stab = they checked, you bet · AF = (bets+raises)/calls[/]\n")

    tex = Table(title=f"flop by texture ({args.dimension})", title_justify="left")
    for c in (args.dimension, "hands", "c-bet", "fold to c-bet", "stab"):
        tex.add_column(c, justify="right")
    for label, s in by_texture(con, since, until, args.dimension):
        tex.add_row(str(label), str(s.hands), str(s.cbet), str(s.fold_to_cbet), str(s.stab))
    console.print(tex)
    console.print("[dim]  texture is the flop only — turn and river depend on the runout, "
                  "so labelling them with flop texture would mislead.[/]")
    return 0


def cmd_grade(args) -> int:
    """Solve every decision the filter admits and keep the verdicts on disk."""
    from .replay.grader import Filter, Grader, Store, candidates
    from .replay.pool import build as build_pool, postflop_rates, villain_postflop_rates
    from .solver.bridge import available
    if not available():
        console.print("[red]solver binary not built[/] — cd solver-cli && cargo build --release")
        return 1
    from .solver.bridge import throttle
    throttle(args.threads, nice=10)
    con = duckdb.connect(args.db, read_only=True)
    since, until = (EPOCH, FOREVER) if args.all else window(args.days)
    filt = Filter(since=since, until=until,
                  streets=tuple(s.upper() for s in args.street) if args.street else ("FLOP", "TURN", "RIVER"),
                  nodes=tuple(args.node) if args.node else (),
                  first_only=args.first, widths=tuple(args.widths.split("+")), limit=args.limit,
                  refresh=args.refresh, mistakes_only=args.mistakes)
    store = Store()
    items = candidates(con, filt, store)
    console.rule(f"grade — {filt.describe()}")
    if not items:
        console.print("[green]nothing to grade[/]: every matching decision is already on disk")
        return 0
    console.print(f"{len(items)} decisions to solve; verdicts go to {store.root}/ "
                  f"and the run can be interrupted and resumed. Solver on "
                  f"{args.threads or 'all'} of {__import__('os').cpu_count()} cores, niced.\n")
    pool = build_pool(con)
    pool.rates = postflop_rates(con)
    pool.villain_rates = villain_postflop_rates(con)
    grader = Grader(pool, observations(con), store)

    def log(rec):
        p = grader.progress
        loss = rec.loss_bb()
        eta = p.eta_seconds
        console.print(
            f"[dim]{p.done + p.failed:4d}/{p.total}[/] {rec.hand_id} {rec.street.lower():5s} "
            f"{rec.hero_step:12s} {rec.headline:20s} "
            f"{'' if loss is None else f'{loss:+.1f}bb':>8s}  {rec.seconds:5.1f}s"
            f"{'' if eta is None else f'   eta {eta / 60:.0f} min'}")

    from .replay.grader import AlreadyRunning
    try:
        p = grader.run_sync(items, filt, log)
    except AlreadyRunning as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/] — what was solved is kept; run again to resume")
        return 130
    console.print(f"\n{p.done} graded, {p.failed} failed, {p.seconds / 60:.0f} min of solving")
    if p.error:
        console.print(f"[red]last error:[/] {p.error}")
    return 0


def cmd_serve(args) -> int:
    from .web.app import serve
    console.print(f"pokerlab at http://127.0.0.1:{args.port}  (ctrl-c to stop)")
    serve(Path(args.db), port=args.port)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pokerlab")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import", help="parse the exports into DuckDB").set_defaults(fn=cmd_import)
    audit = sub.add_parser("audit", help="hero statistics")
    audit.add_argument("--days", type=int, default=90)
    audit.add_argument("--all", action="store_true", help="ignore the window (labelled as such)")
    audit.set_defaults(fn=cmd_audit)
    leaks = sub.add_parser("leaks", help="preflop deviations from a reference chart")
    leaks.add_argument("--chart", default="rfi_6max_100bb")
    leaks.add_argument("--days", type=int, default=90)
    leaks.set_defaults(fn=cmd_leaks)
    br = sub.add_parser("bankroll", help="variance, risk of ruin and stake readiness")
    br.add_argument("--roll", type=float, default=20.0, help="current bankroll in EUR")
    br.add_argument("--stake", default="NL5")
    br.set_defaults(fn=cmd_bankroll)
    train = sub.add_parser("train", help="preflop drill in the browser")
    train.add_argument("--port", type=int, default=8000)
    train.set_defaults(fn=cmd_train)
    rep = sub.add_parser("replay", help="browse and replay hands with solver output")
    rep.add_argument("--port", type=int, default=8000)
    rep.set_defaults(fn=cmd_replay)
    coach = sub.add_parser("coach", help="weekly markdown report")
    coach.add_argument("--days", type=int, default=7)
    coach.add_argument("--out", help="write to a file instead of stdout")
    coach.set_defaults(fn=cmd_coach)
    vol = sub.add_parser("volume", help="hands/hours/tables over time, with projections")
    vol.add_argument("--hours", type=float, default=1.0)
    vol.add_argument("--tables", type=int, default=4)
    vol.set_defaults(fn=cmd_volume)
    sv = sub.add_parser("solve", help="solve a postflop spot (needs the Rust binary)")
    sv.add_argument("--oop", required=True, help="out-of-position range")
    sv.add_argument("--ip", required=True, help="in-position range")
    sv.add_argument("--board", required=True, help='e.g. "Ah Kd 7c"')
    sv.add_argument("--pot", type=int, default=60)
    sv.add_argument("--stack", type=int, default=200)
    sv.add_argument("--bet", default="50%")
    sv.add_argument("--iterations", type=int, default=300)
    sv.add_argument("--timeout", type=int, default=300)
    sv.add_argument("--rows", type=int, default=12)
    sv.set_defaults(fn=cmd_solve)
    ses = sub.add_parser("sessions", help="session trajectories and tilt testing")
    ses.add_argument("--peak", type=float, default=50, help="min session peak in bb")
    ses.set_defaults(fn=cmd_sessions)
    ai = sub.add_parser("allin", help="all-in equity: run-good vs run-bad")
    ai.add_argument("--rows", type=int, default=8)
    ai.set_defaults(fn=cmd_allin)
    gr = sub.add_parser("grade", help="solve every decision in a window and keep the verdicts")
    gr.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    gr.add_argument("--all", action="store_true")
    gr.add_argument("--street", action="append", help="flop/turn/river, repeatable")
    gr.add_argument("--node", action="append",
                    help="srp_pfa, srp_caller, 3bet_pfa, 3bet_caller, limped, 4bet_*; repeatable")
    gr.add_argument("--first", action="store_true", help="only hero's first decision of the street")
    gr.add_argument("--widths", default="base", help="base, or tight+base+loose for the band")
    gr.add_argument("--limit", type=int, default=0)
    gr.add_argument("--refresh", action="store_true",
                    help="also re-grade verdicts solved under an older range model")
    gr.add_argument("--mistakes", action="store_true",
                    help="only decisions already graded as losing EV (for adding the band)")
    gr.add_argument("--threads", type=int, default=max(2, (__import__("os").cpu_count() or 4) - 4),
                    help="cores the solver may use (default leaves four free); 0 = all")
    gr.set_defaults(fn=cmd_grade)
    sv2 = sub.add_parser("serve", help="the dashboard: hands, replayer, drill, grades")
    sv2.add_argument("--port", type=int, default=8000)
    sv2.set_defaults(fn=cmd_serve)
    pf = sub.add_parser("postflop", help="c-bet, fold-to-c-bet, donk, stab, aggression")
    pf.add_argument("--days", type=int, default=90)
    pf.add_argument("--all", action="store_true")
    pf.add_argument("--dimension", default="wet",
                    choices=["wet", "paired", "suits", "high", "connect"])
    pf.set_defaults(fn=cmd_postflop)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
