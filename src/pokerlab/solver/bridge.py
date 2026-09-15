"""Python side of the Rust solver bridge.

The engine runs as a subprocess behind a JSON contract. That boundary is
deliberate: `postflop-solver` is AGPL-3.0, so keeping it at arm's length
leaves the option of swapping it later. It also means a solve that runs out
of memory kills a child process rather than this one.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BINARY = Path(__file__).resolve().parents[3] / "solver-cli" / "target" / "release" / "pokerlab-solver"
CACHE_DIR = Path("data/solves")
#: Flop trees allocate up front and can reach multiple GB. Never run unbounded.
DEFAULT_TIMEOUT = 300


class SolverError(RuntimeError):
    pass


class SolverUnavailable(SolverError):
    """The binary is missing. Callers should degrade, not crash."""


@dataclass(frozen=True, slots=True)
class Spot:
    oop_range: str
    ip_range: str
    board: tuple[str, ...]
    starting_pot: int
    effective_stack: int
    bet_sizes: dict[str, list[str]] = field(default_factory=lambda: {"flop": ["50%"],
                                                                    "turn": ["66%"],
                                                                    "river": ["75%"]})
    raise_sizes: dict[str, list[str]] = field(default_factory=lambda: {"flop": ["2.5x"],
                                                                      "turn": ["2.5x"],
                                                                      "river": ["2.5x"]})
    max_iterations: int = 300
    target_exploitability: float = 0.5
    compress_memory: bool = False
    rake_rate: float = 0.0
    """Share of the final pot the house takes, e.g. 0.055. Zero is rake-free poker."""
    rake_cap: int = 0
    """Most rake taken from one pot, in chips. Only meaningful with a rate."""
    max_memory_bytes: int = 0
    """Refuse to allocate a tree bigger than this. 0 disables the check.

    Tree size is known before any memory is committed, so an impossible spot
    costs milliseconds to reject instead of minutes to discover. It is a guard,
    not an input: a tree that fits under one budget solves identically under a
    larger one, so it is left out of the cache key.
    """
    action_path: tuple[str, ...] = ()
    """Actions to walk from the root before reading the strategy.

    Each entry must match how the solver renders it ("check", "bet 30",
    "call"). Without this only the first decision of a street is readable,
    which is the minority of real decisions.
    """

    def payload(self) -> dict:
        return {
            "oop_range": self.oop_range, "ip_range": self.ip_range,
            "board": list(self.board), "starting_pot": self.starting_pot,
            "effective_stack": self.effective_stack, "bet_sizes": self.bet_sizes,
            "raise_sizes": self.raise_sizes, "max_iterations": self.max_iterations,
            "target_exploitability": self.target_exploitability,
            "compress_memory": self.compress_memory,
            "rake_rate": self.rake_rate, "rake_cap": self.rake_cap,
            "max_memory_bytes": self.max_memory_bytes,
            "action_path": list(self.action_path),
        }

    @property
    def digest(self) -> str:
        keyed = {k: v for k, v in self.payload().items() if k != "max_memory_bytes"}
        blob = json.dumps(keyed, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Solution:
    exploitability: float
    exploitability_pct_pot: float
    converged: bool
    iterations: int
    oop_ev: float
    ip_ev: float
    oop_equity: float
    ip_equity: float
    root_player: str
    root_actions: list[str]
    root_strategy: list[dict]
    node_path: list[str]
    warnings: list[str]
    elapsed_ms: int
    memory_usage_bytes: int

    @property
    def trustworthy(self) -> bool:
        """An unconverged solve is a wrong solve, and must never pass silently.

        A zero-exploitability solve with zero iterations is the mirror-image
        trap: it means the tree had no betting at all, which reads as perfect.
        The Rust side flags both in `warnings`.
        """
        return self.converged and not self.warnings

    @property
    def all_in_equity(self) -> float:
        """OOP's share of the pot if all cards run out with no more betting.

        With single-combo ranges this is a plain all-in equity calculation --
        the honest way to ask "was I ahead when the money went in", separate
        from whether the hand won.
        """
        return self.oop_equity

    def strategy_for(self, combo: str) -> dict[str, float]:
        """Action -> frequency at the reported node, for one exact combo."""
        return next((r["actions"] for r in self.root_strategy if r["hand"] == combo), {})

    def ev_for(self, combo: str) -> dict[str, float]:
        """Action -> expected value in chips, for one exact combo.

        A frequency says what the solver does; this says what it costs not to.
        Fold is 0 by construction, so every other number reads as "chips
        better than folding".
        """
        return next((r.get("ev", {}) for r in self.root_strategy if r["hand"] == combo), {})


def available() -> bool:
    return BINARY.exists()


#: How much of the machine a solve may take. The engine uses every core by
#: default, which is the fastest solve and an unusable laptop for the nine
#: hours a batch runs. Both are read at call time so a batch can set them for
#: its own process without touching anything else.
THREADS_VAR = "POKERLAB_SOLVER_THREADS"
NICE_VAR = "POKERLAB_SOLVER_NICE"


def _env() -> dict:
    import os
    env = dict(os.environ)
    threads = env.get(THREADS_VAR)
    if threads and threads.isdigit() and int(threads) > 0:
        env["RAYON_NUM_THREADS"] = threads
    return env


def _lower_priority() -> None:
    """Runs in the child before exec: nice it if asked, so the UI keeps its cores."""
    import os
    try:
        n = int(os.environ.get(NICE_VAR, "0"))
        if n > 0:
            os.nice(n)
    except (ValueError, OSError):
        pass


def throttle(threads: int | None, nice: int = 10) -> None:
    """Make every later solve in this process polite: `threads` cores, niced."""
    import os
    if threads:
        os.environ[THREADS_VAR] = str(threads)
    else:
        os.environ.pop(THREADS_VAR, None)
    os.environ[NICE_VAR] = str(nice)


def solve(spot: Spot, timeout: int = DEFAULT_TIMEOUT, cache: bool = True) -> Solution:
    if not available():
        raise SolverUnavailable(
            f"solver binary not built at {BINARY}. Run: "
            f"cd solver-cli && cargo build --release")

    cached = CACHE_DIR / f"{spot.digest}.json"
    if cache and cached.exists():
        return _to_solution(json.loads(cached.read_text()))

    try:
        proc = subprocess.run([str(BINARY)], input=json.dumps(spot.payload()),
                              capture_output=True, text=True, timeout=timeout,
                              env=_env(), preexec_fn=_lower_priority)
    except subprocess.TimeoutExpired as exc:
        raise SolverError(
            f"solve exceeded {timeout}s. Flop trees with several bet sizes on wide "
            f"ranges are the usual cause — narrow the tree or raise the timeout.") from exc

    if proc.returncode != 0:
        try:
            detail = json.loads(proc.stderr).get("error", proc.stderr)
        except json.JSONDecodeError:
            detail = (proc.stderr or "unknown error").strip()
        raise SolverError(detail)

    raw = json.loads(proc.stdout)
    if cache:
        # The input travels with the output so the cache can be re-keyed or
        # audited later; the answer alone cannot say what question it answers.
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({**raw, "input": spot.payload()}))
    return _to_solution(raw)


def cached(spot: Spot) -> Solution | None:
    """The stored answer to exactly this spot, or None. Never solves."""
    p = CACHE_DIR / f"{spot.digest}.json"
    if not p.exists():
        return None
    try:
        return _to_solution(json.loads(p.read_text()))
    except (json.JSONDecodeError, KeyError):
        return None


def _to_solution(raw: dict) -> Solution:
    return Solution(
        exploitability=raw["exploitability"],
        exploitability_pct_pot=raw["exploitability_pct_pot"],
        converged=raw["converged"], iterations=raw["iterations"],
        oop_ev=raw["oop_ev"], ip_ev=raw["ip_ev"],
        oop_equity=raw["oop_equity"], ip_equity=raw["ip_equity"],
        root_player=raw["root_player"], root_actions=raw["root_actions"],
        root_strategy=raw["root_strategy"], node_path=raw.get("node_path", []),
        warnings=raw.get("warnings", []),
        elapsed_ms=raw["elapsed_ms"], memory_usage_bytes=raw["memory_usage_bytes"],
    )


def equity_table(range_spec: str, board: tuple[str, ...]) -> dict[str, float]:
    """Exact equity of every combo in `range_spec` against a random hand on `board`.

    Built on a tree with no betting and zero iterations: the engine's per-hand
    equity is enumerated from the cards alone, so nothing is solved and the
    "mirror-image" warning that such a tree produces is exactly the expected
    output here. Combos are keyed the way the engine names them ("AhKh",
    higher rank first).
    """
    from ..ranges.notation import all_hands
    spot = Spot(oop_range=range_spec, ip_range=", ".join(all_hands()), board=board,
                starting_pot=100, effective_stack=100,
                bet_sizes={"flop": [], "turn": [], "river": []},
                raise_sizes={"flop": [], "turn": [], "river": []},
                max_iterations=0, target_exploitability=100.0)
    sol = solve(spot, cache=False)
    return {row["hand"]: row["equity"] for row in sol.root_strategy}


# --------------------------------------------------------------------------
# finding an old solve again
# --------------------------------------------------------------------------

INDEX_PATH = CACHE_DIR / ".index.json"


def _spec_pct(spec: str) -> float:
    """Share of the 1326 combos a range spec covers, without the notation module."""
    total = 0.0
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        hand, _, w = token.partition(":")
        weight = float(w) if w else 1.0
        n = 6 if len(hand) == 2 else 4 if hand.endswith("s") else 12
        total += n * weight
    return round(100 * total / 1326, 1)


def _index() -> dict[str, dict]:
    """Every cached solve by its inputs, built once and kept up to date on disk.

    Verdicts graded before the digest was recorded can only be tied back to
    their solve by matching what was solved: board, pot, stacks, path and the
    two range widths. Parsing every file takes seconds, so the index persists.
    """
    idx: dict[str, dict] = {}
    if INDEX_PATH.exists():
        try:
            idx = json.loads(INDEX_PATH.read_text())
        except json.JSONDecodeError:
            idx = {}
    if not CACHE_DIR.exists():
        return idx
    dirty = False
    for p in CACHE_DIR.glob("*.json"):
        digest = p.stem
        if digest in idx:
            continue
        try:
            inp = json.loads(p.read_text()).get("input")
        except (json.JSONDecodeError, OSError):
            continue
        if not inp:
            continue
        idx[digest] = {
            "board": " ".join(inp["board"]), "pot": inp["starting_pot"],
            "stack": inp["effective_stack"], "path": list(inp.get("action_path", [])),
            "oop": _spec_pct(inp["oop_range"]), "ip": _spec_pct(inp["ip_range"]),
            "raises": inp.get("raise_sizes", {}),
        }
        dirty = True
    if dirty:
        tmp = INDEX_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx))
        tmp.replace(INDEX_PATH)
    return idx


def find_cached(board: tuple[str, ...], starting_pot: int, effective_stack: int,
                action_path: tuple[str, ...], villain_pct: float | None,
                villain_is_oop: bool, tolerance: float = 1.5) -> Solution | None:
    """The cached solve of this spot whose villain range is nearest the width
    a verdict recorded, or None when nothing comes close enough."""
    key = (" ".join(board), starting_pot, effective_stack, list(action_path))
    best, gap = None, tolerance
    for digest, e in _index().items():
        if (e["board"], e["pot"], e["stack"], e["path"]) != key:
            continue
        if villain_pct is None:
            return _load(digest)
        d = abs((e["oop"] if villain_is_oop else e["ip"]) - villain_pct)
        if d < gap:
            best, gap = digest, d
    return _load(best) if best else None


def _load(digest: str) -> Solution | None:
    p = CACHE_DIR / f"{digest}.json"
    if not p.exists():
        return None
    try:
        return _to_solution(json.loads(p.read_text()))
    except (json.JSONDecodeError, KeyError):
        return None
