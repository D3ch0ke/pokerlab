# pokerlab — working notes

Read `README.md` first: it documents the architecture, the command surface, and
the four money-model rules that were derived by reconciliation. Do not re-derive
those.

## Setup

```bash
./.venv/bin/pokerlab import        # rebuild the DuckDB from the exports (~2.5s)
./.venv/bin/pokerlab serve         # the dashboard on :8000
./.venv/bin/pokerlab grade --all --street flop --node srp_pfa --first   # ~10 h, resumable
./.venv/bin/python -m pytest tests -q
```

The pool profile and the solver presets need `numpy`, `scikit-learn` and
`pandas` in the venv (installed 22 Sep 2026; the profile degrades to "no
archetypes" without them).

The solver binary lives at `solver-cli/target/release/pokerlab-solver`; build it
with `~/.cargo/bin/cargo build --release` (cargo is not on PATH).

## House rules

- **The reconciliation gate is non-negotiable.** `scripts/reconcile.py` must
  report N/N over every NL5 hand on disk after any parser change (11,272/11,272
  at the Sep 12 2026 import). It is the only thing that catches silent
  money-model bugs.
- **Never report a rate without its sample size**, and suppress below `MIN_N`.
  Where something cannot be judged, say so rather than estimating.
- **Every stat is time-scoped.** There is no lifetime default; play changed
  materially over 11 months.
- **Charts are the weakest link.** They are hand-authored references carrying
  `source` / `confidence`, not solver output. Findings that depend on a chart's
  absolute level are weaker than findings about its *shape*.
- **Cash only.** Spins, MTTs and short deck are excluded at import.

## Gotchas that have already bitten

- DuckDB's windowed `SUM` is **NULL for a partition's first row** — always
  `COALESCE(..., 0)`, or every stat keyed on "= 0" silently reads false.
- DuckDB `executemany` binds row by row and takes minutes; use the Arrow path in
  `db/load.py`.
- Parameters are not allowed in `CREATE VIEW`; use a parameterised CTE.
- `postflop-solver` requires the **higher rank first** in a combo (`Kc9c` is
  rejected, `KcQc` fine) — see `_combo()` in `stats/allin.py`.
- Seat tags share one bracket, space separated: `[BTN SB Hero]`.
- Donk and stab are different things, and both are undefined in limped pots.
- Texture belongs to the **flop only**; the runout must not relabel it.
- A DuckDB connection is **not safe to drive from two threads**. The replayer
  solves on a background thread while still serving pages; sharing one
  connection handed each thread the other's rows, which surfaced as a hand that
  existed a moment ago no longer being found. Use `con.cursor()` per caller.
- The number of **distinct hands seen at showdown is not a range width**. A
  villain opening the button 26% mixes, so the union of hands they have ever
  been seen opening is far wider. Reading that union as membership produced a
  33% range for a villain measured at 13%. Counts are relative frequencies; the
  measured action frequency is the width.
- `postflop-solver` only ever reports the **root** node. Reading a decision that
  is not the first of its street needs `action_path`, or you silently grade a
  different decision.
- **The tree shape decides the answer more than any solver setting.** Removing
  raises to fit memory moved the solver's c-bet frequency by up to 50 points on
  real spots. Retreat in order (turn/river raises, then all raises), report the
  retreat, and never compare verdicts solved on different trees.
- **Rank ranges with the engine, not in Python.** `narrow()` once spent 218 s
  on a river decision whose solve took 2 ms — 34 million pure-Python hand
  evaluations. `bridge.equity_table()` does it exactly in under a second.
- **Don't put guards in cache keys.** `max_memory_bytes` was hashed into the
  solve digest, so raising the budget would have orphaned every cached solve.
- **A villain's range is every preflop decision they made, not the first.** The
  pool once keyed cells on the first decision only, so an opener who called a
  3-bet sat on their opening range. Cells now carry `~after_call` /
  `~after_raise` and widths multiply along the path.
- **Solves, verdicts, villain notes and edited solver presets are the only
  copies** (`data/solves/`, `data/verdicts/`, `data/villain_notes.json`,
  `data/solver_presets.json`; `reports/` is ignored too). Hours of compute and
  hand-written reads; back them up.
- `layout.py` already owns `.bar` (the inline chart bar). A page-level class
  with that name paints a solid stripe; the replayer's nav bar is `.hbar`.
- A villain note is judgement written from the stats on a date at a hand
  count, and the page says so. Notes for the 200+ hand regulars were written
  on 18 Sep 2026; rewrite one when its hand count has moved materially, not
  because the numbers wobbled.
- The grader runs on a thread inside the web process. `web/state.py` hands out
  a cursor per caller and uses an RLock — a plain Lock deadlocked the first
  page that built the pool.

- A session key in a URL must not be an ISO timestamp: the `+02:00` offset
  arrives as a space. `web/review.py` keys sessions as `%Y%m%d%H%M%S`.
- `pokerlab serve` does not reload. For development use the
  `.claude/launch.json` entry (uvicorn `--reload` on :8011).
- Calendar weeks never reach `MIN_N` on a single preflop cell (a month is ~900
  hands; BB-vs-UTG comes up ten times in it). `/leaks` buckets by equal hand
  counts instead.
- The pool changes with every import, so a verdict's villain range cannot be
  rebuilt bit-exactly later. Reads now store the solve `digest`; older ones
  are matched to the cache by spot and villain width (`bridge.find_cached`),
  through an index at `data/solves/.index.json` built on first use (~5 s).

- **NL5 river bettors barely bluff.** Of 318 villain river bets/raises that
  were called and shown, 3.5% were no-pair, 26% one pair, 70% two pair+. The
  strongest-first narrowing has no bluff layer and the showdowns say it does
  not need one. Do not add air to villain ranges without new evidence.
- **River-call verdicts are not EV.** The villain range has no bluff layer, so
  the solver says fold to every one-pair river call; the outcome audit
  (Sep 18 2026, 27 HU calls) says those calls made +200bb over folding.
  Aggregate the store by street × action before trusting a bucket, and read
  `reports/leaks-research-2026-09-18.md` before re-deriving river leaks.
- **Per-villain postflop narrowing moved 1 verdict in 65** on the Sep 14
  session (widths moved up to 2× for a maniac). Composition, not width, is
  what decides a call-vs-fold verdict, and composition is strongest-first.
- **Showdown-informed filler ordering is a null result** (+0.03 nats on a
  hold-out over 34 cells × 8 splits). `_build_range` accepts an `order` but
  nothing passes one.

- **"When do the fish play" is confounded twice.** Counting known fish per
  hour mirrors your own schedule (the players you know are the ones from the
  hours you play), so `/when` measures table looseness list-free and shows it
  inside each month as well. Measured Sep 18 2026: 02–06h ≈ 41–45% of
  opponents in the pot vs 30% at 16–20h, holds within Dec/Jan/Apr/Sep;
  weekday is flat (37–40%). Hero bb/100 by hour is noise and is shown dimmed.

- **A uniform lock is the wrong opponent model.** Locking a villain to "folds
  50%" with every hand alike makes the solver bet 100% (folding sets is
  absurd); the same 50% dealt weakest-first gives 53%. Ranked mode is the
  default; `blend` is set per street from showdown composition.
- **Pool rules must be line-conditional where the line matters.** The fold
  rate to *any* river bet is 51%; to the lead's third barrel it is 34%
  (n=101). The first river rule used the former and over-folded every
  barrel-caller. Rules are measured facing the lead's bet on all streets now,
  but the policy is still Markov in (street, situation, size): chained locks
  compound whatever error is in each link, so a "raise every donk" result
  is a hypothesis, not a finding.
- **Two sizes per player per street with raises on every street is 6–18 GB
  at 100bb.** The built-in presets default to the documented retreat (raises
  on the flop only, one turn/river size) and the panel prices the tree before
  solving; adding branches is the user's call, with the number in front of
  them.
- **The transient players are a third of villain-hands and a different
  population** (VPIP 58, limp 44%, fold to c-bet 45%). A pool model fitted
  on regulars alone is too tight; `/pool` keeps the tiers apart and
  `vs-unknown` is its own preset.
- The 4 `test_web` failures that pick "the newest flop hand" without a game
  filter are because the newest hands on file are an NL10 session (21 Sep
  2026); not a regression.

## Verification habits

Test hypotheses against the data rather than answering from intuition, and
compare trajectory claims against a null model (`stats/sessions.py`) — most
"I always tilt when..." patterns are what random walks do. When a number looks
wrong, inspect real hands before concluding it is a bug; twice it wasn't.
