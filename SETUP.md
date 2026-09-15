# SETUP.md — adapting pokerlab for a new owner

**To the new owner:** open Claude Code in this folder and say
`read SETUP.md and do it`. Everything below is addressed to Claude.

**To Claude:** this is a personal poker analysis tool (audit, dashboard,
replayer, solver-backed grader) written by a friend of the person you are
working with. It was built for **Betclic.fr NL5 cash, in euros, hero name
"Deshoke"**. The new owner plays on a **different site**. Your job is to get
it running on *their* machine with *their* hand histories, which means one
real piece of engineering — a new hand-history parser — plus a handful of
constant changes. Work through the phases in order; do not skip the
reconciliation gate in phase 4, it is the only thing that catches a parser
that is silently wrong.

Read `README.md` and `CLAUDE.md` first. `CLAUDE.md` contains the previous
owner's house rules and gotchas; they still apply, except where this file
says otherwise. Ask the owner questions when you hit a fork; do not guess
about their site, stake, or currency.

---

## Phase 0 — toolchain

Check, and install anything missing (ask before installing):

- **Python ≥ 3.13** (`pyproject.toml` requires it). `python3 --version`.
- **Rust toolchain** via `rustup` (https://rustup.rs). The solver is a Rust
  binary; `cargo` is needed once to build it. If cargo is not on PATH after
  install it is at `~/.cargo/bin/cargo`.
- **git** (cargo fetches the `postflop-solver` crate from a pinned GitHub
  commit — needs internet the first time).

## Phase 1 — install

```bash
python3 -m venv .venv
./.venv/bin/pip install -e .
cd solver-cli && ~/.cargo/bin/cargo build --release && cd ..
ls -la solver-cli/target/release/pokerlab-solver   # must exist
./.venv/bin/python -m pytest tests -q               # baseline: passes except reconciliation (skipped, no data yet)
```

The cargo build takes a few minutes. Do not proceed until the binary exists.

`.claude/launch.json` already defines a dev server (uvicorn with reload on
:8011) — use it for the dashboard while iterating.

## Phase 2 — hand histories

Ask the owner:

1. **Which site** (PokerStars, Winamax, GGPoker, PartyPoker, 888, Unibet,
   iPoker skin, …) and where their exported hand histories live on disk.
2. **Which stake(s)** they play and in **which currency**.
3. **Whether the site is Betclic/iPoker format after all.** If the files
   begin with `*** HEADER ***` and have `Hand ID:` / `Game Name:` /
   `Total Pot:` / `Rake:` lines and `[BTN SB Hero]` seat tags, the existing
   parser works and phase 3 is skipped — only phase 5 constants change.

Then:

- Create a `hands/` directory at the project root and copy (not move) the
  exports into it. `hands/` is git-ignored.
- Edit `default_sources()` in `src/pokerlab/parse/corpus.py` to return
  `[root / "hands"]` (plus any `*.zip` at the root if the site exports
  zips). Update its docstring. `_raw_blocks` walks `*.txt` recursively and
  inside zips; if the site exports another extension (`.log`, `.xml`, …),
  extend it.
- Look at **real files** before writing any code. Read 20+ hands including:
  a walk, a multiway all-in with side pots, an uncalled bet, a hand where a
  blind is posted out of position (dead blind / new player), a straddle if
  the site has them, a run-it-twice if the site has them. Note every
  section marker, every action verb, how money is written, whether
  `Total pot`, `Rake`, and `Uncalled bet returned` are stated, and how hero
  is identified (usually `Dealt to <name>`).

## Phase 3 — write the parser (the real work)

### Where the Betclic parser is wired in

Five places import `pokerlab.parse.betclic` directly:

```
src/pokerlab/parse/corpus.py     from .betclic import split_hands
src/pokerlab/db/load.py          from ..parse.betclic import ParseError, parse_hand
src/pokerlab/stats/allin.py      from ..parse.betclic import parse_hand
scripts/reconcile.py             from pokerlab.parse.betclic import ParseError, parse_hand
tests/test_parser.py, tests/test_reconciliation.py
```

Write the new parser as `src/pokerlab/parse/<site>.py` (e.g. `stars.py`),
then make `src/pokerlab/parse/__init__.py` re-export
`parse_hand, split_hands, parse_file, ParseError` from the active site
module, and point all five call sites at `pokerlab.parse`. Leave
`betclic.py` in the tree; it is the reference implementation for the
semantics below. `ParseError` lives in `parse/model.py`; reuse it.

### The contract

The parser must produce `Hand` objects (`src/pokerlab/parse/model.py`) with
**exactly** these semantics. Every downstream stat assumes them; the
reconciliation gate in phase 4 checks them.

**Module API**

- `split_hands(text: str) -> list[str]` — split one file into raw
  per-hand blocks. `corpus.collect` dedupes on hand id and keeps the
  *longest* copy of a duplicated id, so blocks must be self-contained.
- `parse_hand(block: str) -> Hand` — raise `ParseError` on anything it
  cannot fully parse. **Never** swallow and return a partial hand; a
  partial hand becomes a silently wrong stat. `db/load.py` catches
  `ParseError` and puts the block in the `quarantine` table.
- `parse_file(path) -> tuple[list[Hand], list[tuple[str, str]]]` as in
  `betclic.py`.

**Also in `corpus.py`:** `_HAND_ID`, `_GAME_NAME`, `_GAME_MODE` are regexes
run on the *raw block* to dedupe and filter before parsing. Rewrite them
for the new format, or restructure `collect()` to get id / game / mode from
a cheap header-only parse. The filter must keep **cash games only** —
tournaments, spins/expressos/sit-and-gos, and short deck are excluded at
import, never afterwards. Set `CASH_GAMES` to the owner's stake strings
(see phase 5 for the string format).

**Money is integer cents, everywhere.** `_cents()` in `betclic.py` shows
the exact string→int conversion. Never a float. If the currency has no
subunit (play money, some crypto sites), pick a unit and be consistent.

**`Hand` header fields**

| field | meaning |
|---|---|
| `hand_id` | site's id, string |
| `game_name` | normalised to `"NLHE {sb}/{bb} 6 Max"`, e.g. `"NLHE 0.05/0.10 6 Max"`. Every stat filters on this string; produce it in this shape so the constants in phase 5 are the only other change. Use the site's own max-players number (`9 Max`, `HU`). |
| `game_mode` | `"Cash Game"` for cash; anything else is filtered out |
| `played_at` | timezone-aware, converted to **UTC** |
| `table_id` | string, `""` if unknown |
| `sb`, `bb` | cents |
| `total_pot` | **called money only**, cents. See below. |
| `rake` | cents taken from this pot |

**`total_pot`, `rake`, `collected`, `uncalled` — the four money rules.**
These were derived by reconciliation on Betclic and every later report
depends on them (`README.md` § The money model):

1. `total_pot` is the pot **after** uncalled money is returned, i.e. the
   sum of what was actually matched. `Hand.uncalled` is *derived* as
   `Σ contributions − total_pot`; there is no field for it. If the site
   prints `Uncalled bet (X) returned to Y`, do **not** subtract it from the
   player's contributions — record the full wager in `contributed` and let
   `total_pot` be the site's stated total pot (which on most sites already
   excludes the returned amount; verify on a real hand).
2. `collected[name]` is chips **won, net of rake**, summed over main and
   side pots. `Σ collected == total_pot − rake` must hold exactly.
3. `Σ net(name) over seats == −rake` must hold exactly (`Hand.net`).
4. If the site does not print rake per hand, derive it:
   `rake = total_pot − Σ collected`. If it prints neither, stop and ask.

**`Seat`**: `seat_no` (site's seat number), `name`, `stack` in cents
**before blinds**, `tags` a frozenset drawn from `{"BTN", "SB", "BB",
"Hero"}`. Only those four matter. `positions.py` derives every other
position from seat order relative to the `BTN` tag, and handles 2- to
9-handed tables. Hero is whoever the site dealt cards to (`Dealt to X`).
Heads-up: the button *is* the small blind and carries both tags. Empty
seats and sitting-out players are not seats.

**`Action`** — one per line, in order, `street` ∈ `Street`:

| field | rule |
|---|---|
| `verb` | map the site's words onto `Verb`: `FOLD CHECK CALL BET RAISE POST_SB POST_BB POST_ANTE`. Nothing else. Table chatter (`sits out`, `joins`, `is connected`, `has timed out`, `shows`, `mucks`, `collected`) is not an action. A straddle is `POST_BB` with `dead=False` — treat it like a blind and add `"straddle"` handling to `_cap` if the site has them. |
| `time` | `"HH:MM:SS"` string; `db/load.py` computes seconds between consecutive actions from it. If the site has no per-action timestamps, use the hand's time for every action (durations become 0, which downstream treats as unknown — check `_secs` callers still make sense). |
| `announced` | the **cumulative street total** the player declared. `Raises to X` is cumulative; `raises A to B` on Stars means announced = B; `calls X` means announced = their previous street total + X; a bet means X. Blinds: the blind amount. |
| `contributed` | chips this specific action actually added: `announced − what they already had in this street`. |
| `effective` | `announced` capped at the most any *live* opponent could still call. `_cap()` in `betclic.py` is the exact algorithm — port it, do not reinvent it. |
| `all_in` | true if the action put the player all-in |
| `dead` | a blind posted by a player **not tagged** for it (new player posting, returning player, missed blind). Dead money does not count toward that player's raise-to baseline. Some sites print `posts small & big blinds` in one line — split into two actions, the SB dead. |

**Cards**: `hole_cards[name]` for every player whose cards are in the file
(hero, plus anyone shown); `shown[name]` for showdown only; `boards[street]`
the cards **dealt on that street** (flop 3 cards, turn 1, river 1 — check
whether the site prints the full board on each street line and slice).
Card format is `"As"`, `"Td"` — rank `2-9TJQKA`, suit `cdhs`.

**Run-it-twice / bomb pots / rabbit hunts**: raise `ParseError` on the
first version — quarantine them, count them, and only handle them if they
are a meaningful share of hands. Say so in `CLAUDE.md`.

### Iterating

Write the parser against real hands from `hands/`. `betclic.py` is 196
lines; expect similar. Keep `parse_hand` strict. Then go to phase 4 and
loop until it reports N/N.

## Phase 4 — the reconciliation gate (non-negotiable)

```bash
./.venv/bin/python scripts/reconcile.py
```

It must print `RECONCILED: N / N` over every cash hand on disk. Until it
does, nothing downstream can be trusted. It checks the four money rules
above and prints the first example of each failure class — **open that
hand in `hands/` and read it** before changing anything. The previous owner
found twice that the "bug" was a real hand doing something unexpected
(side-pot ordinals, dead blinds), and the fix was in the parser, not the
model. Do not weaken the checks to make them pass.

`scripts/reconcile.py` filters on `f"Game Name: {NL5}" in v` — a raw-text
check tied to the Betclic header. Replace it with the new format's
equivalent, or filter on the parsed `game_name`.

Then update the tests:

- `tests/fixtures/*.txt` are Betclic hands. Replace them with two real
  hands from the owner's site covering the same two cases — a **dead small
  blind** and an **all-in whose effective size is capped by a shorter
  stack** — and rewrite the assertions in `tests/test_parser.py` for them.
  Change the hero name in `test_parser.py`, `test_coach.py`,
  `test_reconciliation.py` from `Deshoke` to the owner's screen name.
- `tests/test_reconciliation.py` skips when `default_sources()` is empty;
  fix the skip message.
- `./.venv/bin/python -m pytest tests -q` must pass.

## Phase 5 — constants that are the previous owner's

Do all of these; grep to confirm nothing is missed. `S` below is the
owner's stake string in the normalised `"NLHE sb/bb N Max"` form.

| file | change |
|---|---|
| `src/pokerlab/parse/corpus.py` | `CASH_GAMES` → the owner's cash stakes; `NL5 = CASH_GAMES[0]` → their main stake (keep the name `NL5` unless you rename it everywhere — it is imported in ~10 places; a rename to `MAIN_STAKE` is fine if done with a project-wide grep). |
| `src/pokerlab/stats/core.py:15` | `NL5 = "NLHE 0.02/0.05 6 Max"` — a **second, independent copy** of the same constant. Set to `S`. Better: import it from `corpus`. |
| `src/pokerlab/bankroll.py` | `LADDER` — stakes and `bb_eur`. Rename `bb_eur` if the currency is not euros, or leave the name and document it. |
| `src/pokerlab/replay/evaluate.py:40` | `RAKE_RATE = 0.055`, `RAKE_CAP = 100` (cents) — Betclic NL5 rake. Set to the owner's site and stake; both feed the solver, and a wrong rake shifts verdicts. Ask the owner or look it up for their site. |
| `src/pokerlab/web/layout.py:178` | sidebar brand `Betclic NL5 · Deshoke` → owner's site, stake, name. |
| `src/pokerlab/web/dashboard.py` | page subtitles say `NL5 · …` in two places; make them use the stake constant. |
| `src/pokerlab/cli.py:177-187` | bankroll tables titled around the previous owner's `deposit €10, withdraw at €20` cycle. Ask the owner for their bankroll rule, or generalise via `--roll`. |
| currency symbol | `€` appears in `cli.py`, `web/layout.py`, `web/review.py`, `web/dashboard.py`, `web/replay.py`, `replay/evaluate.py`, `replay/node.py`, `replay/grader.py`. If the owner plays in `$`, introduce one `CURRENCY = "$"` constant (in `stats/core.py`) and replace the literals. `parse/betclic.py` may keep its `€`. |
| `src/pokerlab/stats/core.py:18` | `MIN_N = 30` — leave it. |
| `README.md` | mentions Betclic and 9,845 / 11,272 hand counts throughout; update the quick start, the money-model paragraph, and the parser description for the new site. |
| `AGENTS.md` | duplicate of `CLAUDE.md`; keep in sync or delete. |

## Phase 6 — what to tell the owner about the charts and the grader

- `src/pokerlab/ranges/charts/` are **hand-authored preflop reference
  charts for the Betclic NL5 pool**, carrying `source` / `confidence`
  fields. They are not solver output. They are probably reasonable for any
  micro-stakes 6-max pool but nothing verified them elsewhere. Tell the
  owner; leave them in place; `pokerlab leaks` uses them.
- `pokerlab grade` solves every decision in a window and takes ~10 h for
  a season of hands. It is resumable and writes to `data/solves/` and
  `data/verdicts/` — the only copies; tell the owner to back those up.
  Run it only after phase 4 is N/N and the owner has confirmed the rake
  constants, because verdicts solved on different settings cannot be
  compared.
- The `CLAUDE.md` section "NL5 river bettors barely bluff" and the
  per-villain narrowing notes are **measurements of the Betclic pool**.
  Keep them as history, mark them as such, and let the owner's data speak
  for itself once there are enough showdowns.

## Phase 7 — run it

```bash
./.venv/bin/pokerlab import          # prints hands / quarantined / coverage
./.venv/bin/python scripts/reconcile.py
./.venv/bin/python -m pytest tests -q
./.venv/bin/pokerlab audit --all
./.venv/bin/pokerlab serve           # http://localhost:8000
```

Check `quarantine` after import (`SELECT reason, count(*) FROM quarantine
GROUP BY 1`) and report the coverage % to the owner. Open the dashboard
and click through Overview, Hands, one hand in the replayer, Postflop,
Sessions. Then rewrite `CLAUDE.md`: replace the Betclic-specific facts
(hand counts, site, hero, rake, pool observations) with the owner's, keep
every house rule and every gotcha, and add the new site's quirks you found
in phase 3. Delete this file's "To the new owner" preamble once done, or
delete `SETUP.md` altogether.
