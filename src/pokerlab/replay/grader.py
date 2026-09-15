"""Grade every solvable decision and keep the verdicts.

A verdict exists only while someone is looking at one hand in the replayer.
This module runs the same `evaluate()` over a whole window and writes what it
found to `data/verdicts/<hand_id>.json`, one file per hand, so that the coach
and the dashboard can ask "where do the big blinds go" instead of "was this
one hand right".

The files are the only copy, and a flop node costs 30-60 s to solve, so the
grader is resumable: a decision already graded at the current `VERSION` is
skipped. Bump `VERSION` when the model behind a verdict changes -- tree
shape, rake, range assignment -- because a verdict from an older model is a
different answer, not a stale copy of this one.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..ranges.notation import canonical
from ..stats.core import EPOCH, FOREVER, NL5
from .evaluate import Verdict, evaluate
from .hand import ReplayHand, load as load_hand
from .node import Decision, decisions

VERDICT_DIR = Path("data/verdicts")
#: 1: rake-free, 2 GB budget. 2: rake 5.5%/€1, 6 GB budget, flop-raise retreat.
#: 3: villain ranges chain every preflop decision (a limp-caller or an opener
#:    who called a 3-bet was on their first decision's range before).
VERSION = 3
#: The range model a verdict was solved under, within a VERSION. Bumping
#: VERSION hides every old verdict until it is re-graded; a model tag lets a
#: refinement be adopted for new grades and re-applied selectively with
#: `Filter.refresh`, while the old verdicts stay visible and say what they are.
MODEL = "villain-narrowing"

STREETS = ("FLOP", "TURN", "RIVER")


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ReadRecord:
    width: str
    hero_action: str | None = None
    freq: dict[str, float] = field(default_factory=dict)
    ev: dict[str, float] = field(default_factory=dict)
    ev_loss: float | None = None            # chips
    approved: bool | None = None
    range_freq: dict[str, float] = field(default_factory=dict)
    """Solver's action mix over hero's whole range at this node, range-weighted.

    This is the number a baseline is built from: not what the solver does with
    the hand hero held, but what it does with everything hero arrives with.
    """
    degraded: str = ""
    error: str | None = None
    converged: bool | None = None
    trustworthy: bool | None = None
    exploitability_pct_pot: float | None = None
    iterations: int | None = None
    elapsed_ms: int | None = None
    memory_bytes: int | None = None
    villain_label: str = ""
    villain_pct: float | None = None
    digest: str = ""
    """Cache key of the solve behind this read, so the per-combo strategy can
    be shown later without solving again. Empty on verdicts graded before it
    was recorded; those fall back to a search of the cache by spot."""


@dataclass(slots=True)
class VerdictRecord:
    hand_id: str
    idx: int
    played_at: str
    street: str
    board: list[str]
    texture: dict
    node: str                    # "srp_pfa" | "srp_caller" | "3bet_pfa" | "limped_first" ...
    hero_pos: str | None
    villain_pos: str | None
    hero_oop: bool
    hero_combo: str | None
    hero_verb: str
    hero_step: str
    first_of_street: bool
    action_path: list[str]
    starting_pot: int
    effective_stack: int
    bb: int
    headline: str
    stable: bool | None
    trustworthy: bool
    warnings: list[str]
    reads: list[ReadRecord]
    version: int = VERSION
    graded_at: str = ""
    seconds: float = 0.0
    model: str = "pool-narrowing"     # verdicts before MODEL existed carry the default

    @property
    def base(self) -> ReadRecord | None:
        return next((r for r in self.reads if r.width == "base" and r.ev), None)

    def loss_bb(self, width: str = "base") -> float | None:
        r = next((r for r in self.reads if r.width == width), None)
        if r is None or r.ev_loss is None:
            return None
        return r.ev_loss / self.bb

    @property
    def spr(self) -> float:
        return self.effective_stack / max(self.starting_pot, 1)


def _range_freq(read, hero_range) -> dict[str, float]:
    sol = read.solution
    if sol is None:
        return {}
    totals: dict[str, float] = {}
    mass = 0.0
    for row in sol.root_strategy:
        combo = row["hand"]
        w = hero_range.freq(canonical([combo[:2], combo[2:]]))
        if w <= 0:
            continue
        mass += w
        for action, p in row["actions"].items():
            totals[action] = totals.get(action, 0.0) + w * p
    return {a: round(v / mass, 4) for a, v in totals.items()} if mass else {}


def _pot_kind(hand: ReplayHand) -> str:
    raises = sum(1 for a in hand.actions if a.street == "PRE-FLOP" and a.verb == "Raises to")
    return {0: "limped", 1: "srp", 2: "3bet"}.get(raises, "4bet")


def _hero_is_pfa(hand: ReplayHand) -> bool:
    last = None
    for a in hand.actions:
        if a.street == "PRE-FLOP" and a.verb == "Raises to":
            last = a
    return bool(last and last.is_hero)


def node_of(hand: ReplayHand) -> str:
    """The pot the flop was seen in, and hero's role in it.

    Limped pots have no aggressor, so "pfa" is undefined there — the same
    reason donk and stab are undefined in them.
    """
    kind = _pot_kind(hand)
    if kind == "limped":
        return "limped"
    return f"{kind}_{'pfa' if _hero_is_pfa(hand) else 'caller'}"


def to_record(verdict: Verdict, hand: ReplayHand, seconds: float) -> VerdictRecord:
    d = verdict.decision
    reads = []
    for r in verdict.reads:
        sol = r.solution
        reads.append(ReadRecord(
            width=r.width, hero_action=r.hero_action, freq=r.freq, ev=r.ev,
            ev_loss=r.ev_loss, approved=r.approved(d.starting_pot),
            range_freq=_range_freq(r, verdict.hero_range),
            degraded=r.degraded, error=r.error,
            converged=sol.converged if sol else None,
            trustworthy=sol.trustworthy if sol else None,
            exploitability_pct_pot=sol.exploitability_pct_pot if sol else None,
            iterations=sol.iterations if sol else None,
            elapsed_ms=sol.elapsed_ms if sol else None,
            memory_bytes=sol.memory_usage_bytes if sol else None,
            villain_label=r.assignment.label, villain_pct=r.assignment.range.pct,
            digest=r.digest,
        ))
    hero_seat = hand.seat_of.get(hand.hero)
    villain_seat = hand.seat_of.get(d.villain) if d.villain else None
    return VerdictRecord(
        hand_id=hand.hand_id, idx=d.idx, played_at=hand.played_at.isoformat(),
        street=d.street, board=list(d.board), texture=dict(hand.texture),
        node=node_of(hand),
        hero_pos=hero_seat.position if hero_seat else None,
        villain_pos=villain_seat.position if villain_seat else None,
        hero_oop=_hero_oop(hand, d), hero_combo=d.hero_combo,
        hero_verb=d.action.verb, hero_step=d.hero_step(),
        first_of_street=not any(s not in ("check",) for s in d.action_path),
        action_path=list(d.action_path),
        starting_pot=d.starting_pot, effective_stack=d.effective_stack, bb=hand.bb,
        headline=verdict.headline, stable=verdict.stable,
        trustworthy=verdict.trustworthy, warnings=verdict.warnings,
        reads=reads, graded_at=datetime.now(timezone.utc).isoformat(),
        seconds=round(seconds, 1), model=MODEL,
    )


def _hero_oop(hand: ReplayHand, d: Decision) -> bool:
    from .evaluate import _hero_is_oop
    return _hero_is_oop(hand, d)


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

class Store:
    """One JSON file per hand under `VERDICT_DIR`."""

    def __init__(self, root: Path = VERDICT_DIR):
        self.root = root

    def path(self, hand_id: str) -> Path:
        return self.root / f"{hand_id}.json"

    def get(self, hand_id: str) -> dict[int, VerdictRecord]:
        p = self.path(hand_id)
        if not p.exists():
            return {}
        raw = json.loads(p.read_text())
        return {int(k): _from_dict(v) for k, v in raw.get("decisions", {}).items()}

    def put(self, rec: VerdictRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path(rec.hand_id)
        raw = json.loads(p.read_text()) if p.exists() else {"decisions": {}}
        raw["decisions"][str(rec.idx)] = asdict(rec)
        p.write_text(json.dumps(raw))

    def all(self, version: int | None = VERSION) -> list[VerdictRecord]:
        out: list[VerdictRecord] = []
        if not self.root.exists():
            return out
        for p in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            for v in raw.get("decisions", {}).values():
                rec = _from_dict(v)
                if version is None or rec.version == version:
                    out.append(rec)
        return out


def _from_dict(v: dict) -> VerdictRecord:
    reads = [ReadRecord(**r) for r in v.get("reads", [])]
    return VerdictRecord(**{**v, "reads": reads})


# --------------------------------------------------------------------------
# the batch
# --------------------------------------------------------------------------

_CANDIDATES = """
WITH live AS (
    SELECT hand_id, count(DISTINCT name) AS n
    FROM actions WHERE street = 'FLOP' GROUP BY hand_id)
SELECT h.hand_id
FROM hands h JOIN live USING (hand_id)
WHERE h.game_name = ? AND h.played_at >= ? AND h.played_at < ?
  AND h.hero_cards IS NOT NULL AND live.n = 2
ORDER BY h.played_at DESC
"""


@dataclass(slots=True)
class Filter:
    since: datetime = EPOCH
    until: datetime = FOREVER
    streets: tuple[str, ...] = STREETS
    nodes: tuple[str, ...] = ()           # empty = every node
    first_only: bool = False              # only hero's first decision of the street
    widths: tuple[str, ...] = ("base",)
    limit: int = 0
    refresh: bool = False                 # re-grade verdicts from an older range model
    mistakes_only: bool = False           # only decisions already graded as losing EV

    def describe(self) -> str:
        bits = [f"{'/'.join(self.streets).lower()}"]
        if self.nodes:
            bits.append("/".join(self.nodes))
        if self.first_only:
            bits.append("first decision of the street")
        if self.refresh:
            bits.append(f"re-grading verdicts older than {MODEL}")
        if self.mistakes_only:
            bits.append("graded mistakes only")
        bits.append(f"reads: {'+'.join(self.widths)}")
        if self.limit:
            bits.append(f"limit {self.limit}")
        return " · ".join(bits)


@dataclass
class Progress:
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    seconds: float = 0.0
    current: str = ""
    status: str = "idle"                  # idle | running | stopping | done | stopped | failed
    started: datetime | None = None
    error: str = ""
    filter: str = ""

    @property
    def remaining(self) -> int:
        return max(self.total - self.done - self.skipped - self.failed, 0)

    @property
    def per_solve(self) -> float | None:
        return self.seconds / self.done if self.done else None

    @property
    def eta_seconds(self) -> float | None:
        p = self.per_solve
        return None if p is None else p * self.remaining


def candidates(con, filt: Filter, store: Store | None = None) -> list[tuple[ReplayHand, Decision]]:
    """Every decision the filter admits, minus those already graded at this VERSION."""
    store = store or Store()
    out: list[tuple[ReplayHand, Decision]] = []
    ids = [r[0] for r in con.execute(_CANDIDATES, [NL5, filt.since, filt.until]).fetchall()]
    for hid in ids:
        hand = load_hand(con, hid)
        if filt.nodes and node_of(hand) not in filt.nodes:
            continue
        done = store.get(hid)
        seen_streets: set[str] = set()
        for d in decisions(hand):
            if not d.solvable or d.street not in filt.streets:
                continue
            if filt.first_only:
                if d.street in seen_streets:
                    continue
                seen_streets.add(d.street)
            prior = done.get(d.idx)
            if filt.mistakes_only and not (
                    prior and prior.base and prior.base.approved is False):
                continue
            if prior and prior.version == VERSION and set(filt.widths) <= {
                    r.width for r in prior.reads} and not (filt.refresh and prior.model != MODEL):
                continue
            out.append((hand, d))
            if filt.limit and len(out) >= filt.limit:
                return out
    return out


class AlreadyRunning(RuntimeError):
    """Another grader owns the store. Two writers would race on the same hand files."""


class _Lock:
    """A pid file under the store: the CLI and the dashboard must not both grade."""

    def __init__(self, root: Path):
        self.path = root / ".grading.lock"

    def acquire(self) -> None:
        import os
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
                os.kill(pid, 0)
                raise AlreadyRunning(f"a grader is already running (pid {pid}); stop it first "
                                     f"or remove {self.path} if it is stale")
            except (ValueError, ProcessLookupError, PermissionError):
                pass                        # stale lock from a dead process
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()))

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class Grader:
    """Runs a batch on a thread; the dashboard polls `progress`."""

    def __init__(self, pool, observations, store: Store | None = None):
        self.pool = pool
        self.observations = observations
        self.store = store or Store()
        self.progress = Progress()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, items: list[tuple[ReplayHand, Decision]], filt: Filter,
              threads: int | None = None) -> None:
        if self.running:
            return
        _Lock(self.store.root).acquire()
        # The dashboard's process keeps serving pages while this runs, so the
        # batch is throttled the same way the CLI one is.
        from ..solver.bridge import throttle
        import os
        throttle(threads if threads is not None else max(2, (os.cpu_count() or 4) - 4), nice=10)
        self._stop.clear()
        self.progress = Progress(total=len(items), status="running",
                                 started=datetime.now(timezone.utc), filter=filt.describe())
        self._thread = threading.Thread(target=self._run, args=(items, filt), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.running:
            self._stop.set()
            self.progress.status = "stopping"

    def run_sync(self, items, filt: Filter, log=None) -> Progress:
        _Lock(self.store.root).acquire()
        self.progress = Progress(total=len(items), status="running",
                                 started=datetime.now(timezone.utc), filter=filt.describe())
        self._run(items, filt, log)
        return self.progress

    def _run(self, items, filt: Filter, log=None) -> None:
        p = self.progress
        lock = _Lock(self.store.root)
        try:
            for hand, d in items:
                if self._stop.is_set():
                    p.status = "stopped"
                    return
                p.current = f"{hand.hand_id} · {d.street.lower()} · {d.action.verb.lower()}"
                t = time.time()
                try:
                    v = evaluate(self.pool, self.observations, hand, d, widths=filt.widths)
                    rec = to_record(v, hand, time.time() - t)
                    self.store.put(rec)
                    p.done += 1
                    p.seconds += time.time() - t
                    if log:
                        log(rec)
                except Exception as exc:                    # noqa: BLE001 - one bad hand must not end the night
                    p.failed += 1
                    p.error = f"{hand.hand_id}: {type(exc).__name__}: {exc}"
                    traceback.print_exc()
            p.status = "done"
        except Exception as exc:                            # noqa: BLE001
            p.status = "failed"
            p.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            p.current = ""
            lock.release()
