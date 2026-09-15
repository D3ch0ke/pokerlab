"""Reference charts: versioned, provenance-tagged range tables.

Every chart states where it came from and how much to trust it. v1 charts are
hand-authored references, not solver output, and say so -- you should always
know what you are being graded against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .notation import Range

CHARTS_DIR = Path(__file__).parent / "charts"


@dataclass(frozen=True, slots=True)
class Chart:
    id: str
    label: str
    spot: str
    assumption: str      # "gto" | "exploit_vs_loose_passive"
    confidence: str      # "reference" | "reference-low" | "derived" | "solver"
    source: str
    version: int
    actions: tuple[str, ...]
    ranges: dict[str, dict[str, Range]]

    def action_range(self, position: str, action: str) -> Range:
        return self.ranges.get(position, {}).get(action, Range())

    def prescribed(self, position: str, hand: str) -> dict[str, float]:
        """Frequency per action for one hand. Missing mass is 'fold'."""
        out = {a: self.action_range(position, a).freq(hand) for a in self.actions if a != "fold"}
        out["fold"] = max(0.0, 1.0 - sum(out.values()))
        return out

    @property
    def positions(self) -> list[str]:
        return list(self.ranges)


def _load(path: Path) -> Chart:
    raw = json.loads(path.read_text())
    return Chart(
        id=raw["id"], label=raw["label"], spot=raw["spot"],
        assumption=raw["assumption"], confidence=raw["confidence"],
        source=raw["source"], version=raw["version"],
        actions=tuple(raw["actions"]),
        ranges={pos: {act: Range.parse(spec) for act, spec in acts.items()}
                for pos, acts in raw["ranges"].items()},
    )


@cache
def load(chart_id: str) -> Chart:
    path = CHARTS_DIR / f"{chart_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no chart {chart_id!r} in {CHARTS_DIR}")
    return _load(path)


@cache
def available() -> tuple[Chart, ...]:
    return tuple(_load(p) for p in sorted(CHARTS_DIR.glob("*.json")))


#: Preference when more than one chart covers the same node. A derived chart is
#: a mechanical width cut and the README is explicit that it has roughly the
#: right width and the wrong shape, so an authored one wins wherever both exist.
_CONFIDENCE_RANK = {"solver": 0, "reference": 1, "reference-low": 2, "derived": 3}


def chart_for(spot: str, key: str | None = None) -> Chart | None:
    """The best chart covering this node, or None.

    Several charts can share a `spot` -- an authored one and a derived one that
    fills the positions nobody wrote. Picking the first match would silently
    drop whichever node the other one covers, so selection is by key first and
    provenance second.
    """
    candidates = [c for c in available() if c.spot == spot]
    if key is not None:
        covering = [c for c in candidates if key in c.ranges]
        candidates = covering or []
    return min(candidates, key=lambda c: _CONFIDENCE_RANK.get(c.confidence, 9),
               default=None)
