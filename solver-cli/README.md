# pokerlab-solver

A thin Rust CLI around the [`postflop-solver`](https://github.com/b-inary/postflop-solver) crate.
Reads one JSON spot description on **stdin**, writes one JSON result on **stdout**.
It contains no poker logic of its own — it only marshals JSON in and out of the library.

Intended to be driven from Python via `subprocess`.

## License

`postflop-solver` is **AGPL-3.0-or-later**. Linking against it makes this crate — and anything
that distributes it or serves it over a network — subject to AGPL-3.0-or-later, including the
obligation to provide complete corresponding source. This crate is therefore licensed
AGPL-3.0-or-later. Consider that before shipping the binary or putting it behind a web service.

## Dependency pin

Upstream is archived. The dependency is pinned to the final commit on `master`:

```
rev = "9d1509fe5077d019825f833eed04b16d342dfda1"   # 2023-10-01
```

Built with `default-features = false, features = ["rayon"]`, which enables multi-threaded solving
and drops the `bincode`/`zstd` save-file features (not needed here).

## Build

Rust is installed at `~/.cargo` and is not on `PATH` by default.

```bash
source "$HOME/.cargo/env"
cd solver-cli
cargo build --release
# binary: solver-cli/target/release/pokerlab-solver
```

Verified with `cargo 1.98.1` / `rustc 1.98.1` on macOS (arm64). Builds clean, no patches to
upstream needed.

## Usage

```bash
./target/release/pokerlab-solver < spot.json > result.json
```

Exit code `0` with a result on stdout, or non-zero with `{"error": "..."}` on stderr.

## Input contract (stdin)

```json
{
  "oop_range": "22+, A2s+, ...",
  "ip_range":  "...",
  "board":     ["4c", "Jd", "7s"],
  "starting_pot": 60,
  "effective_stack": 200,
  "bet_sizes":   {"flop": ["50%"], "turn": ["66%"], "river": ["75%"]},
  "raise_sizes": {"flop": ["2.5x"], "turn": ["2.5x"], "river": ["2.5x"]},
  "max_iterations": 300,
  "target_exploitability": 0.5
}
```

| Field | Required | Default | Notes |
|---|---|---|---|
| `oop_range`, `ip_range` | yes | — | Standard range notation, e.g. `"66+,A8s+,AJo+,54s"`. Weights (`AKs:0.5`) are supported by the library. |
| `board` | yes | — | 3, 4 or 5 card strings. Length selects the initial street: 3 → flop, 4 → turn, 5 → river. |
| `starting_pot` | yes | — | Chips. Must be > 0. |
| `effective_stack` | yes | — | Chips, per player, remaining behind. Must be > 0. |
| `bet_sizes` | no | empty | Per street, a list of size tokens. See below. |
| `raise_sizes` | no | empty | Per street, a list of raise tokens. |
| `max_iterations` | no | `1000` | Hard cap on Discounted CFR iterations. |
| `target_exploitability` | no | `0.5` | **Percent of the starting pot.** `0.5` means 0.5% of pot. Converted internally to chips. |
| `rake_rate` | no | `0.0` | e.g. `0.05` for 5%. |
| `rake_cap` | no | `0.0` | Chips. |
| `add_allin_threshold` | no | `1.5` | Add an all-in branch if max bet ≤ this × pot. |
| `force_allin_threshold` | no | `0.15` | Force all-in if SPR after a call ≤ this. |
| `merging_threshold` | no | `0.1` | Merge near-identical bet sizes. |
| `compress_memory` | no | `false` | Store as 16-bit ints instead of 32-bit floats. Roughly halves memory at a small precision cost. |

Bet/raise size tokens are passed straight through to the library's parser. Accepted forms include
`"50%"` (percent of pot), `"2.5x"` (multiple of the previous bet, for raises), `"e"` (geometric),
`"a"` (all-in), and absolute chip amounts. Multiple entries per street produce multiple branches.
The same sizes are used for both players on that street.

Streets other than the initial one still need sizes — a turn-initial solve uses `turn` and
`river`, and `flop` is ignored.

> **Footgun:** if you leave a street's `bet_sizes` empty, the tree for that street has no betting
> in it. The solve then trivially "converges" at exploitability `0.0` because there is nothing to
> solve. The output flags this in `warnings` and you can see it in `root_actions`.

## Output contract (stdout)

```json
{
  "exploitability": 0.1786,
  "exploitability_pct_pot": 0.2977,
  "target_exploitability": 0.3,
  "converged": true,
  "iterations": 40,
  "max_iterations": 300,
  "oop_ev": 40.44,
  "ip_ev": 19.55,
  "oop_equity": 0.6228,
  "ip_equity": 0.3771,
  "root_player": "oop",
  "root_actions": ["check", "bet 45"],
  "root_strategy": [
    {"hand": "8d7d", "actions": {"check": 0.246277, "bet 45": 0.753723}}
  ],
  "warnings": [],
  "memory_usage_bytes": 153268,
  "elapsed_ms": 1
}
```

| Field | Meaning |
|---|---|
| `exploitability` | **Always present.** In chips. Recomputed *after* `finalize()`, so it describes the exact strategy returned in `root_strategy` — not an intermediate value. |
| `exploitability_pct_pot` | Same number as a percent of `starting_pot`. Compare against your input `target_exploitability`. |
| `target_exploitability` | The requested target, converted to chips. |
| `converged` | `exploitability <= target_exploitability`. **Check this.** A `false` here means the strategy is not trustworthy. |
| `iterations` | Iterations actually run. `0` means the initial strategy already met the target (usually a degenerate tree). |
| `oop_ev` / `ip_ev` | Range-weighted average EV in chips at the root. Sums to `starting_pot` when rake is 0 — a useful sanity check. |
| `oop_equity` / `ip_equity` | Range-weighted average equity, 0–1. |
| `root_player` | `"oop"` or `"ip"` — who acts at the root. |
| `root_actions` | Action labels at the root, in order: `check`, `fold`, `call`, `bet N`, `raise N`, `allin N`. |
| `root_strategy` | One entry per combo the root player holds on this board. `actions` maps each label to a frequency; frequencies sum to 1. Rounded to 6 dp. |
| `warnings` | Non-fatal advisories (degenerate tree, iteration limit hit). Usually empty. |
| `memory_usage_bytes` | Solver table size for the built tree. |
| `elapsed_ms` | Wall time for parse + build + solve + extract, inside the process. |

### Errors

Any failure writes a single JSON object to **stderr** and exits non-zero. Nothing is written to
stdout — there is no partial or fabricated result.

```json
{"error": "invalid oop_range: Failed to parse range: ZZ+"}
```

Covered: empty stdin, malformed JSON, unparseable range, wrong board length, bad card strings,
non-positive pot or stack, unparseable bet sizes, action-tree construction failure.

## Convergence

`target_exploitability` is a *target*, not a guarantee. The solver stops on whichever comes first:
the target, or `max_iterations`. Exploitability is only recomputed every 10 iterations (matching
upstream), so `iterations` lands on a multiple of 10 in practice.

Always read `converged` and `exploitability` before using `root_strategy`. An unconverged solve is
a wrong solve.

## Measured performance

Real runs on this machine (Apple Silicon, 11 cores, 18 GB RAM), release build, target 0.5% of pot.
`peak RSS` is `/usr/bin/time -l` maximum resident set size for the whole process.

| Spot | Ranges | Sizings | Iterations | Exploitability | `elapsed_ms` | Peak RSS |
|---|---|---|---|---|---|---|
| River `AhKd7c2s9h`, pot 60, stack 200 | 4 / 4 groups (23 combos OOP) | 75% bet, 2.5x raise | 40 / 300 | 0.179 chips (0.30% pot) | 1 ms | 2.9 MB |
| Turn `Td9d6hQc`, pot 200, stack 900 | wide (167 combos OOP) | 60% bet, 2.5x raise | 60 / 300 | 0.899 chips (0.45% pot) | 73 ms | 13 MB |
| Flop `Td9d6h`, pot 60, stack 200 | wide (180 combos OOP) | 50% bet, 2.5x raise | 80 / 100 | 0.235 chips (0.39% pot) | 5221 ms | 492 MB |

Takeaways for the calling code:

- River and turn spots are effectively interactive.
- Flop solves are the expensive case: seconds of CPU and hundreds of MB, and both scale steeply
  with the number of bet sizes per street. Two or three flop sizings with wide ranges can reach
  multiple GB. Budget for it, set `max_iterations` deliberately, and consider `compress_memory`.
- Memory is allocated up front for the whole tree. `memory_usage_bytes` in the output tells you
  what was allocated after the fact; there is no dry-run mode.

## Calling from Python

```python
import json, subprocess

def solve(spot: dict) -> dict:
    p = subprocess.run(
        ["solver-cli/target/release/pokerlab-solver"],
        input=json.dumps(spot),
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(json.loads(p.stderr)["error"])
    result = json.loads(p.stdout)
    if not result["converged"]:
        raise RuntimeError(
            f"solve did not converge: {result['exploitability_pct_pot']:.2f}% of pot "
            f"after {result['iterations']} iterations"
        )
    return result
```

Consider a `subprocess` timeout for flop spots.
