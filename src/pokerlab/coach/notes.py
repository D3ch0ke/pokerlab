"""Per-villain notes: the two lines you want in front of you when they sit down.

A note is judgement, not measurement. It is written from the stats as they
stood on a date and at a hand count, and both are stored with it so a note
written on 200 hands is not mistaken for one written on 1,400. The profile
page shows the numbers beside the note so the reader can check it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path("data/villain_notes.json")


@dataclass(slots=True)
class Note:
    text: str
    written: str          # ISO date
    hands: int            # shared hands when it was written
    by: str = "you"       # "you" | "claude"


class Notes:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self._notes: dict[str, Note] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                self._notes = {k: Note(**v) for k, v in raw.items()}
            except (json.JSONDecodeError, OSError, TypeError):
                self._notes = {}

    def get(self, name: str) -> Note | None:
        return self._notes.get(name)

    def __len__(self) -> int:
        return len(self._notes)

    def set(self, name: str, text: str, hands: int, by: str = "you") -> None:
        text = text.strip()
        if not text:
            self._notes.pop(name, None)
        else:
            self._notes[name] = Note(text, datetime.now(timezone.utc).date().isoformat(), hands, by)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in sorted(self._notes.items())},
                                  indent=1, ensure_ascii=False))
        tmp.replace(self.path)
