"""Collect and dedupe hand-history blocks from directories and zip archives.

The exports overlap heavily: 70,054 ``Hand ID`` lines across the trees
resolve to 19,018 unique hands. Worse, 3,530 of those ids appear with
*differing* content, so dedupe cannot take first-seen -- it keeps the most
complete copy of each hand.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterator

from .betclic import split_hands

_HAND_ID = re.compile(r"^Hand ID: (\S+)", re.M)
_GAME_NAME = re.compile(r"^Game Name: (.+)$", re.M)
_GAME_MODE = re.compile(r"^Game Mode: (.+)$", re.M)

#: The only games we analyse. Short deck is a different game; Spins and MTTs
#: are a different discipline whose stats would corrupt these if pooled.
CASH_GAMES = ("NLHE 0.02/0.05 6 Max", "NLHE 0.05/0.1 6 Max")
NL5 = CASH_GAMES[0]


def _raw_blocks(root: Path) -> Iterator[str]:
    if root.is_file() and root.suffix == ".zip":
        with zipfile.ZipFile(root) as zf:
            for info in zf.infolist():
                if info.filename.endswith(".txt"):
                    yield from split_hands(zf.read(info).decode("utf-8", "replace"))
        return
    for path in sorted(root.rglob("*.txt")):
        yield from split_hands(path.read_text(encoding="utf-8", errors="replace"))
    for path in sorted(root.rglob("*.zip")):
        yield from _raw_blocks(path)


def collect(sources: list[Path], games: tuple[str, ...] | None = CASH_GAMES) -> dict[str, str]:
    """Deduped ``{hand_id: block}``, filtered to ``games`` (None = everything)."""
    best: dict[str, str] = {}
    for source in sources:
        for block in _raw_blocks(source):
            hid = _HAND_ID.search(block)
            if not hid:
                continue
            if games is not None:
                name = _GAME_NAME.search(block)
                mode = _GAME_MODE.search(block)
                if not name or name.group(1).strip() not in games:
                    continue
                if not mode or mode.group(1).strip() != "Cash Game":
                    continue
            key = hid.group(1)
            # Prefer the most complete copy; truncated duplicates exist.
            if key not in best or len(block) > len(best[key]):
                best[key] = block
    return best


def default_sources(root: Path = Path(".")) -> list[Path]:
    """The processed tree plus any top-level Betclic export zips.

    Deliberately explicit: globbing the project root would rescan the tree
    through nested zips and walk .venv, which is both slow and pointless.
    """
    sources = [p for p in (root / "processed copy",) if p.exists()]
    sources += sorted(root.glob("Betclic_*.zip"))
    return sources
