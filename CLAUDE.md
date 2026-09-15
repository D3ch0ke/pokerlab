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
- **Solves and verdicts are the only copies** (`data/solves/`, `data/verdicts/`).
  Hours of compute; back them up.
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
- **Per-villain postflop narrowing moved 1 verdict in 65** on the Sep 14
  session (widths moved up to 2× for a maniac). Composition, not width, is
  what decides a call-vs-fold verdict, and composition is strongest-first.
- **Showdown-informed filler ordering is a null result** (+0.03 nats on a
  hold-out over 34 cells × 8 splits). `_build_range` accepts an `order` but
  nothing passes one.

## Verification habits

Test hypotheses against the data rather than answering from intuition, and
compare trajectory claims against a null model (`stats/sessions.py`) — most
"I always tilt when..." patterns are what random walks do. When a number looks
wrong, inspect real hands before concluding it is a bug; twice it wasn't.
