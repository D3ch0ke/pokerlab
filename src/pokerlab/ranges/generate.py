"""Mechanically generated charts for the nodes nobody hand-authored.

Thirteen of the fifteen 6-max vs-open nodes exist as authored references and
none of the vs-3-bet ones do, so the trainer silently had nothing to say about
the rest. Filling those gaps by hand would add more numbers of exactly the kind
this repo already distrusts. Filling them by machine at least makes the
judgement auditable: one target width per node, stated in ``frequencies.json``
with its basis, cut against the measured ordering in ``strength.py``.

What that buys is honesty, not accuracy. A top-N% cut is a *linear* model of a
range -- it can only ever produce the N strongest hands. Real defence and
4-betting ranges are polarised: they hold A5s and 76s as bluffs while folding
the 88 that outranks both. So these charts have roughly the right width and the
wrong shape, and their ``source`` says so. They are labelled ``derived``, never
``solver``; the existing authored charts are never touched.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from .notation import Range
from .strength import provenance, top_by_combos

TABLE = Path(__file__).parent / "frequencies.json"
CHARTS_DIR = Path(__file__).parent / "charts"

COMBOS = 1326.0

#: Charts this module owns. Anything not listed here is hand-authored and off
#: limits -- regenerating an authored chart would destroy the judgement in it.
GENERATED = ("vs_open_6max_100bb_derived", "vs_3bet_6max_100bb_derived")


@cache
def table() -> dict:
    return json.loads(TABLE.read_text())


def _target(node: dict, position: str, action: str) -> float:
    """A node's target for one action, resolved to a combo count."""
    share = float(node[action])
    if node["unit"] == "rfi_share":
        from .chart import load
        return share * load("rfi_6max_100bb").action_range(position, "raise").n_combos
    return share * COMBOS


def cut(raise_target: float, call_target: float) -> dict[str, str]:
    """Top-N% by measured strength for the raise, the next N% for the call.

    Nested rather than polarised, which is the model's central weakness: the
    calling range here is always exactly the band below the raising range.
    """
    raises = top_by_combos(raise_target)
    ranges = {"raise": Range({h: 1.0 for h in raises}).to_spec()}
    if call_target > 0:
        calls = top_by_combos(call_target, exclude=frozenset(raises))
        ranges["call"] = Range({h: 1.0 for h in calls}).to_spec()
    return ranges


def _widths(chart_id: str) -> dict[str, tuple[float, float]]:
    """Realised (raise%, call%) per node, after the combo-count rounding."""
    out = {}
    for position, ranges in build(chart_id)["ranges"].items():
        out[position] = (Range.parse(ranges["raise"]).pct,
                         Range.parse(ranges.get("call", "")).pct if "call" in ranges else 0.0)
    return out


_PREAMBLE = (
    "MECHANICALLY GENERATED, NOT SOLVER OUTPUT AND NOT HAND-AUTHORED. Each node "
    "below is a top-N%-by-strength cut of the measured ordering in "
    "ranges/strength.py (chart-nesting tier first, then Monte Carlo equity "
    "against a random hand: {prov}), taken at a target width read from "
    "ranges/frequencies.json. The raising range is the strongest N% of combos; "
    "the calling range is the band immediately below it. "
)

_LIMITATION = (
    "KNOWN LIMITATION, do not read past it: a pure strength cut is a linear "
    "model and real ranges are not linear. A genuine defence or 4-bet range is "
    "polarised -- it holds A5s, 76s and 65s as bluffs and blockers while "
    "folding the 88 and A9o that outrank them on raw strength. This generator "
    "cannot express that, so every node here is nested: it has approximately "
    "the right WIDTH and demonstrably the WRONG SHAPE. Judge yourself against "
    "the width, not against any individual hand near the boundary. "
    "The output is also only as good as the target it was fed, and those "
    "targets are hand-authored judgement (see the 'basis' field on each row of "
    "frequencies.json), not solved results -- the generator is deterministic, "
    "which makes it reproducible, not correct."
)


def _source(chart_id: str, per_node: str) -> str:
    prov = provenance().get("source", "see ranges/strength.json")
    return _PREAMBLE.format(prov=prov) + per_node + " " + _LIMITATION


def _vs_open() -> dict:
    nodes = {k: v for k, v in table()["vs_open_6max_100bb_derived"].items()
             if not k.startswith("_")}
    ranges = {
        position: cut(_target(node, position, "raise"), _target(node, position, "call"))
        for position, node in nodes.items()
    }
    per_node = "Targets: " + "; ".join(
        f"{position} raise {node['raise']:.1%} / call {node['call']:.1%} of all combos, "
        f"extrapolated from the neighbouring hand-authored nodes in "
        f"vs_open_6max_100bb"
        for position, node in nodes.items()
    ) + (". These two nodes are the only ones missing from the authored chart "
         "(the complete 6-max set is 15; that chart carries 13). They are kept "
         "in a separate file precisely so they cannot be mistaken for the "
         "authored ones, which are untouched.")
    return {
        "id": "vs_open_6max_100bb_derived",
        "label": "Facing a single raise - 6-max, 100bb (derived)",
        "spot": "vs_open",
        "assumption": "gto",
        "confidence": "derived",
        "source": _source("vs_open_6max_100bb_derived", per_node),
        "version": 1,
        "actions": ["raise", "call", "fold"],
        "ranges": ranges,
    }


def _vs_3bet() -> dict:
    raw = table()["vs_3bet_6max_100bb_derived"]
    nodes = {k: v for k, v in raw.items() if not k.startswith("_")}
    ranges = {
        position: cut(_target(node, position, "raise"), _target(node, position, "call"))
        for position, node in nodes.items()
    }
    per_node = (
        "Targets: " + raw["_rule"]["basis"] + " "
        "Keyed by hero's OPENING seat alone, not by the 3-bettor's seat, "
        "because stats/ranges.py keys a vs_3bet observation that way too -- a "
        "chart keyed on the 3-bettor could never be compared against a single "
        "hand hero has actually played. That is a real loss: facing a button "
        "3-bet and a big-blind 3-bet from the same seat are different problems, "
        "and this chart averages them."
    )
    return {
        "id": "vs_3bet_6max_100bb_derived",
        "label": "Facing a 3-bet after opening - 6-max, 100bb (derived)",
        "spot": "vs_3bet",
        "assumption": "gto",
        "confidence": "derived",
        "source": _source("vs_3bet_6max_100bb_derived", per_node),
        "version": 1,
        "actions": ["raise", "call", "fold"],
        "ranges": ranges,
    }


_BUILDERS = {"vs_open_6max_100bb_derived": _vs_open,
             "vs_3bet_6max_100bb_derived": _vs_3bet}


def build(chart_id: str) -> dict:
    if chart_id not in _BUILDERS:
        raise KeyError(f"{chart_id!r} is not generated; it is hand-authored and stays that way")
    return _BUILDERS[chart_id]()


def serialise(chart_id: str) -> str:
    """The exact bytes written to disk. Same input, same string, always."""
    return json.dumps(build(chart_id), indent=2, sort_keys=False) + "\n"


def write_all(charts_dir: Path = CHARTS_DIR) -> list[Path]:
    out = []
    for chart_id in GENERATED:
        path = charts_dir / f"{chart_id}.json"
        path.write_text(serialise(chart_id))
        out.append(path)
    return out


if __name__ == "__main__":
    for path in write_all():
        print(f"wrote {path}")
    for chart_id in GENERATED:
        for position, (r, c) in _widths(chart_id).items():
            print(f"  {chart_id:32} {position:10} raise {r:5}%  call {c:5}%")
