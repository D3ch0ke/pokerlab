# pokerlab

Personal poker audit, trainer and coach for Betclic NL5 6-max cash.

Built on one principle: **deterministic analysis makes every claim; prose only
narrates it.** Every number traces to a row in the database or to a chart whose
provenance is recorded. At NL5 a confident wrong explanation costs more than no
explanation, so the tool says "not enough data" rather than guessing.

## Quick start

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e .
./.venv/bin/pokerlab import      # parse the exports into DuckDB
./.venv/bin/pokerlab audit       # your numbers, last 90 days
```

| command | what it does |
|---|---|
| `pokerlab import` | Parse and dedupe the Betclic exports into `data/pokerlab.duckdb` |
| `pokerlab audit [--days N] [--all]` | Winrate, rake, VPIP/PFR/3bet by position, monthly trend |
| `pokerlab leaks [--chart ID]` | Preflop deviations from a reference chart, then vs now |
| `pokerlab serve [--port N]` | The dashboard: overview, hands, replayer, postflop/preflop/sessions, grading, drills |
| `pokerlab grade [--days N] [--street S] [--node N] [--first] [--widths W]` | Solve every decision in a window and keep the verdicts |
| `pokerlab train [--port N]` / `pokerlab replay [--port N]` | Aliases for `serve` |
| `pokerlab bankroll [--roll €]` | Measured variance, risk of ruin, stake readiness |
| `pokerlab volume [--hours H] [--tables N]` | Hands/hours/tables over time, with projections |
| `pokerlab coach [--days N] [--out FILE]` | Weekly markdown report |
| `pokerlab solve --oop R --ip R --board "Ah Kd 7c"` | Solve a postflop spot |

## What it does and does not measure

**Scope is cash games only.** Spins and MTTs are not imported at all — pooling
hyper-turbo numbers would corrupt every NL5 stat and there is no clean way to
separate them afterwards. Short deck is excluded as a different game.

**Every statistic is time-scoped.** There is no lifetime default anywhere,
because play changed materially over 11 months and an all-time average blends
several different players.

**Rates below a minimum sample are suppressed**, not estimated. Where a leak
cannot be judged, the tool says so and explains why.

## The money model

Validated by reconciling computed contributions against each hand's own
`Total Pot` and `Rake`, across all NL5 hands — currently **9,845 / 9,845**,
cross-checked against Xeester's independent total. Four rules, each of which
broke an earlier version:

1. In cash games `Total Pot` counts **called money only**; `wins main pot of X`
   equals `Total Pot − Rake`.
2. Therefore `uncalled = Σ contributions − Total Pot`. The files never state the
   returned amount, but it is exactly derivable — and 4.1% of hands have one.
3. `Raises to X` is cumulative over **live** street wagers. A blind posted by a
   player not tagged for it is dead money and does not count toward their
   raise-to baseline.
4. Side pots carry an ordinal: `wins 1st side pot of €X`.

**Bet sizes use `effective`, never `announced`.** Shoving €5 into a €2 pot
against a villain with €1 behind is a 50%-pot bet, not a 250% one. Using
announced amounts would paint a short-stacked player as a habitual overbetter.

## Charts

Charts are versioned JSON carrying `source`, `confidence` and `assumption`.
v1 charts are **hand-authored references, not solver output**, and say so — you
should always know what you are being graded against. `iso_vs_limp_6max` is
marked `reference-low`: no GTO equivalent exists, because solvers are never
given a limping opponent.

Charts marked `derived` are neither authored nor solved: `ranges/generate.py`
cuts them mechanically out of the measured ordering in `ranges/strength.py` at a
target width read from `ranges/frequencies.json`, where every target states its
basis. They fill the nodes nobody wrote (`HJ_vs_UTG`, `SB_vs_UTG`, and the whole
vs-3-bet tree) and carry the model's own limitation in their `source`: a
strength cut is nested and real defence ranges are polarised, so a derived chart
has roughly the right width and the wrong shape. Regenerate with
`python -m pokerlab.ranges.generate`; it refuses to touch the authored files.

`pokerlab train` also serves a range viewer at `/ranges` for browsing any
chart's grid, with its confidence and source on the page.

## The dashboard

`pokerlab serve` puts everything behind one browser page, server-rendered with
no build step:

| page | what it shows |
|---|---|
| `/` | winrate with its 95% interval, rake, core stats, the last session's card (result, EV given up, stop-loss), EV lost per decision by node, by position, monthly trend, biggest graded mistakes (window or last session), recent sessions |
| `/review` | the post-session page: one session at a time, its graded decisions costliest first, a "done" mark per decision, and an import-and-grade button that rebuilds the database from the exports and grades whatever the last week added |
| `/hands` | every hand, filterable by window, seat, pot node, flop texture, villain, result and grading state; each row carries its verdict |
| `/villains` | every player you have shared 50+ hands with: VPIP/PFR/3-bet/fold-to-3-bet/limp/c-bet/fold-to-c-bet, their own bb/100, a coarse style, and their note under the row |
| `/villains/<name>` | one opponent in depth: the note (two lines, editable, stamped with the date and hand count it was written on), your result against them and in contested pots, raise-first-in and defence by seat, c-bet / lead / fold / raise / check-raise / aggression by street, every hand they showed down with its made-hand category and river action, the biggest pots between you, recent hands, graded decisions against them, and when you shared a table |
| `/replay/<id>` | step through a hand without a reload (every frame is pre-rendered; ← → step, ⇧← ⇧→ jump between your decisions, p / n change hand); opens on your first decision, ends on the settled hand where villain cards are turned up and every stack shows its net; the line under the board says who is to act, what it costs and what just happened; a stored verdict is shown at once with both ranges as 13×13 grids (hero's painted with the solver's mix when the solve is on disk), a "what if" that reads the villain's answer to any action one node deeper, and a live band solve on click; next/prev hand walks the `/hands` filter the hand was opened from, a session's review list, or the clock |
| `/when` | when the games are soft: the share of opponents voluntarily in the pot by hour, by weekday and as a weekday×hour heatmap, each with n and ±SE and conditioned on the hours you played; the same by month as a check that the hour effect is not a period artefact; and for every known fish (VPIP ≥ 35%, 100+ hands) the hours they are at your table most, as a share of your hands in their active span |
| `/postflop` | c-bet / fold-to-c-bet / donk / stab by street and by texture, beside the solver c-bet baseline once graded |
| `/preflop` | chart deviations, then-vs-now, and the blind-defence curve |
| `/sessions` | session list and the tilt null-model tests |
| `/grades` | start and watch a grading batch; EV given up by street, node, texture and action |
| `/train` | the preflop chart drill: leak-weighted, SM-2 scheduled, with a "due" and a "keep missing" draw; facing an open from the blinds the answer shows the whole defence curve |
| `/train/postflop` | a drill built from your own graded spots: filter by node, street, seat, texture, window, villain or mode (mistakes / due / keep missing); the action is asked first and the size second; every answer is scheduled with SM-2 in `data/postflop_progress.json`; the answer shows both ranges and the solver's mix; keyboard f x c b r 1–9 n |
| `/leaks` | the trend page: blind defence by opener seat, c-bet by texture, donk by street, raise-vs-c-bet and graded EV by node over equal-hand periods, each with n and ±SE, and a then-vs-now z-test — the only page that can show improvement at this volume |

Every window is explicit and every rate carries its n, as everywhere else.

### The verdict store

`pokerlab grade` (or the Grading page) runs `evaluate()` over every solvable
decision a filter admits and writes one JSON file per hand under
`data/verdicts/`. A decision already graded at the current `VERSION` is
skipped, so a run can be stopped and resumed; bump `VERSION` in
`replay/grader.py` when the model behind a verdict changes, because an older
verdict is a different answer, not a stale copy.

Each record keeps the decision, the verdict, every read of the band with its
per-action EV and frequency, the solve's convergence and cost — and the
solver's action mix over hero's *whole range* at that node. That last field is
what the **solver c-bet baseline** on the postflop page is built from: for each
texture, the solver's bet frequency with the range you actually arrive with,
beside what you did on those same flops.

`stats/grades.py` aggregates the store under the tool's rules: a verdict that
is not trustworthy, was solved on a reduced tree, or that the band could not
settle is counted but never costed, and a bucket under `MIN_GRADED` decisions
shows totals only. "EV given up per 100 hands" is per 100 *graded* hands and
is labelled as such.

`data/verdicts/` and `data/solves/` are the only copies of hours of solving.
Back them up, along with `data/villain_notes.json` — the notes are written by
hand and stored nowhere else.

### The replayer

Filter your hands, step through one action at a time, and solve the node you
are standing on. Villain cards stay face down until the last step, so a
decision is judged on what you knew then.

Not every decision can be solved, and the replayer says which and why rather
than solving something adjacent:

| node | what happens | why |
|---|---|---|
| preflop | graded against a reference chart | a postflop solver does not model it |
| heads-up postflop | solved | 5,204 of your decisions qualify |
| multiway postflop | refused | `postflop-solver` is a two-player engine and a third range cannot be faked |

### Villain range assignment

The open problem: your cards are known, the villain's are not, and a solve is
only as good as its ranges. The answer here is not to pick the right range —
there isn't one — but to build it from layers whose provenance is recorded
separately, and then to report the decision across a **band** of ranges rather
than at a point. Width and composition are knowable to very different degrees,
so they are kept apart:

1. **Width is measured.** Every preflop decision every villain made is on file
   whether or not they showed a hand, so an action frequency rests on thousands
   of observations. A player is dealt a uniformly random hand, so the share of
   hands on which they take an action *is* the width of the range they take it
   with. Where a named villain has ≥ `MIN_N` of their own spots, their frequency
   is used instead of the pool's — the regulars recur in 400–950 hands.
   A villain who acted **twice** preflop — limped then called a raise, opened
   then called a 3-bet — is on the product of both decisions, each measured in
   its own cell conditioned on what they had already done (`SB_vs_BB~after_call`).
   The first version used the first decision alone, which put an opener who
   called a 3-bet on their *opening* range: 40% of c-bet spots and nearly every
   3-bet pot were being graded against the wrong villain.
2. **Composition is observed, and biased.** 4,801 villain hands reached showdown,
   and 30 of 45 (position × spot × action) cells clear `MIN_N`. That subset
   over-represents hands that flopped well enough to keep paying, so it marks the
   **top** of a range and never its bottom. Counts are read as *relative*
   frequencies, never as membership, and they earn `n / (n + 100)` of the width.
3. **The rest is assumed.** The remaining width is filled from `ranges/strength.py`
   — chart tiers first, measured equity below them. This is the weakest layer and
   the one the band exists to stress.

Postflop, the range narrows by what the villain **did**, at the pool's measured
rate for that action on that street: at NL5 they call 49% of flop bets and raise
only 7.9% of them, so scoring a raise as "did not fold" leaves a raiser's range
six times too wide. Hands are ranked by equity **on the board that is out**, not
by preflop rank, which is what lets a flush draw survive a continue that a small
pair does not. The ranking is exact: the engine enumerates every runout in
under a second, where the pure-Python evaluator it replaced took minutes on a
river range (34 million hand evaluations to order 377 hands) and dwarfed the
solve itself.

**The band is the output.** The same node is solved against a tight, a measured
and a loose opponent, and the finding is usually whether they agree. When they
do, the range assumption did not decide it. When they do not, that *is* the
result: the decision was never settled by the cards. The spread is the wider of
the Wilson interval on the measured frequency and a flat ±25% allowance for the
ordering — which is a stated judgement, not a measurement — and it stresses the
postflop narrowing by the same factor, since that is usually the dominant term.

**What the band does not test:** it varies how wide the opponent is, not which
hands fill that width. Two ranges of equal width but different composition can
still disagree. A verdict that only just holds is more fragile than the band
alone makes it look.

Your own range is the one range here that is not an assumption — your cards are
in the file for every hand, not just those that showed down, so there is no
selection bias to correct. Where your measured range omits the hand you actually
held, it is forced in and the panel says so.

## Layout

```
src/pokerlab/
  parse/     betclic.py, corpus.py, model.py   hand histories -> Hand
  db/        schema.sql, load.py               DuckDB, Arrow bulk insert
  stats/     core.py, ranges.py, volume.py     windowed statistics
  ranges/    notation.py, buckets.py, chart.py, charts/*.json
             strength.py, generate.py, frequencies.json
  trainer/   leakweight.py, quiz.py, progress.py
  replay/    hand.py, node.py, pool.py         replay state, decision nodes
             villain.py, evaluate.py           range assignment, the band
             grader.py                         batch grading, the verdict store
  stats/     grades.py                         aggregates over stored verdicts
             villains.py, villain_profile.py  the table of opponents; one opponent in depth
             when.py                           looseness by hour and weekday; fish presence
  coach/     report.py, notes.py                the coaching report; per-villain notes
  web/       layout.py, state.py               shell, shared process state
             dashboard.py, replay.py, villain.py, when.py  pages
             app.py, drill.py                  chart drill, postflop drill
solver-cli/                                    Rust bridge (AGPL, see its README)
```

## Tests

```bash
./.venv/bin/python -m pytest tests -q
```

`test_reconciliation.py` is the gate: the money model must reproduce every NL5
hand on disk. It skips when the exports are absent.

## Solver

`solver-cli/` is a Rust binary wrapping `postflop-solver`, pinned to an exact
revision. Build it once:

```bash
cd solver-cli && ~/.cargo/bin/cargo build --release
```

**Exploitability is reported on every solve, and the bridge refuses to call a
result trustworthy unless it converged.** It also guards the mirror-image trap:
a tree with no bet sizes returns exploitability 0.0 at 0 iterations, which reads
as a flawless solve — that is flagged, not passed through.

Memory is allocated up front and grows steeply with bet sizes per street and
with stack-to-pot ratio. River and turn spots are milliseconds. Measured on
the real c-bet node (2026-09-12): the median spot sits at SPR 13 and its full
tree needs 2.5–4.8 GB, solving in 30–60 s; a tree with only a flop raise fits
in 1–1.8 GB at 12–15 s; a tree with no raises takes 3–5 s — and answers a
materially different question, moving the solver's c-bet frequency by up to
50 points on the same spot. Solves are cached by content hash under
`data/solves/`, with the question stored beside the answer.

What does *not* speed it up, measured: running solves concurrently (rayon
already saturates every core — 1, 2 and 4 in parallel take the same wall
time), `target-cpu=native`, and `compress_memory` (2–10× slower; it is a
fit-in-memory tool). A looser `target_exploitability` of 1% is ~30% faster
and moves aggregate frequencies by under a point but individual combos by up
to 0.45, so verdicts keep 0.5%.

`max_memory_bytes` refuses a tree larger than the budget **before allocating**,
because the size is known in advance: without it, a flop node with 100bb behind
in a limped pot pages the machine for minutes and then fails. The budget is a
guard, not an input — a tree that fits under one budget solves identically
under a larger one — so it is not part of the cache key. The replayer and the
grader set 6 GB (one solve at a time on an 18 GB machine) and retreat to a
coarser tree in order — dropping turn and river raises but keeping the flop
raise, then all raises, then compressing storage — reporting which retreat it
took, since a coarser tree answers a different question.

**Rake is in the tree**: 5.5% of the pot capped at €1.00, measured from the
hands on file. A rake-free solve overstates every marginal bet and call at a
table where the house takes 30 bb/100.

`action_path` walks the tree to a chosen node before reading the strategy, so a
decision that is not the first of the street can be examined — which is most of
them. Steps are written either exactly (`"bet 30"`) or by proximity (`"bet~35"`,
the nearest size the tree carries); a substitution is reported in `warnings`, and
warnings make a solve untrustworthy. Every node also reports **per-action EV**
with fold as the zero, so a decision can be costed and not just described.

## Licensing

The Python code here is yours. `solver-cli/` links `postflop-solver`, which is
**AGPL-3.0** — no obligations for local personal use, but serving it over a
network to others would require publishing your server source. It is kept
behind a subprocess JSON boundary so it can be swapped.
