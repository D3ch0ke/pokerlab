"""Process-wide state shared by every page: the database, the villain pool,
hero's preflop observations, the verdict store and the one grader.

A DuckDB connection is not safe to drive from two threads, and the grader
solves on a thread while pages keep being served, so every caller gets a
cursor of its own from `con()`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

from ..db.load import DEFAULT_DB
from ..replay.grader import Grader, Store, VerdictRecord
from ..replay.pool import build as build_pool, postflop_rates, villain_postflop_rates
from ..stats.ranges import observations as load_observations

_state: dict = {}
_lock = threading.RLock()


def configure(db: Path = DEFAULT_DB) -> None:
    _state.clear()
    _state["db"] = db


def con():
    with _lock:
        if "con" not in _state:
            _state["con"] = duckdb.connect(str(_state.get("db", DEFAULT_DB)), read_only=True)
    return _state["con"].cursor()


def pool():
    with _lock:
        if "pool" not in _state:
            c = con()
            p = build_pool(c)
            p.rates = postflop_rates(c)
            p.villain_rates = villain_postflop_rates(c)
            _state["pool"] = p
        return _state["pool"]


def observations():
    with _lock:
        if "obs" not in _state:
            _state["obs"] = load_observations(con())
        return _state["obs"]


def store() -> Store:
    return _state.setdefault("store", Store())


def grader() -> Grader:
    with _lock:
        if "grader" not in _state:
            _state["grader"] = Grader(pool(), observations(), store())
        return _state["grader"]


def verdicts() -> list[VerdictRecord]:
    """Every stored verdict at the current version, reloaded when the store changed.

    Cheap staleness check: file count plus the newest mtime. The grader writes
    one file per hand, so either moves on every graded decision.
    """
    root = store().root
    stamp = (0, 0.0)
    if root.exists():
        files = list(root.glob("*.json"))
        stamp = (len(files), max((f.stat().st_mtime for f in files), default=0.0))
    with _lock:
        if _state.get("verdicts_stamp") != stamp:
            _state["verdicts"] = store().all()
            _state["verdicts_stamp"] = stamp
        return _state["verdicts"]


def by_hand() -> dict[str, list[VerdictRecord]]:
    out: dict[str, list[VerdictRecord]] = {}
    for r in verdicts():
        out.setdefault(r.hand_id, []).append(r)
    return out


def marks():
    from ..trainer.marks import Marks
    with _lock:
        if "marks" not in _state:
            _state["marks"] = Marks()
        return _state["marks"]


def notes():
    from ..coach.notes import Notes
    with _lock:
        if "notes" not in _state:
            _state["notes"] = Notes()
        return _state["notes"]


def postflop_progress():
    from pathlib import Path
    from ..trainer.progress import Progress
    with _lock:
        if "postflop_progress" not in _state:
            _state["postflop_progress"] = Progress.load(Path("data/postflop_progress.json"))
        return _state["postflop_progress"]


def reimport() -> dict:
    """Rebuild the database from the exports and drop everything derived from it.

    The read-only connection must be closed first: DuckDB refuses a second
    connection to the same file with a different configuration in one
    process, and `build()` unlinks the file anyway. Refused while the grader
    is running, since it is solving on the pool built from the old database.
    """
    from ..db.load import build
    with _lock:
        g = _state.get("grader")
        if g is not None and g.running:
            raise RuntimeError("a grading batch is running; wait for it or stop it first")
        con = _state.pop("con", None)
        if con is not None:
            con.close()
        stats = build(db_path=_state.get("db", DEFAULT_DB))
        for key in ("pool", "obs", "grader", "tilt"):
            _state.pop(key, None)
        for key in [k for k in _state if isinstance(k, tuple)]:
            _state.pop(key, None)          # per-page caches keyed on (name, ...)
        return stats
