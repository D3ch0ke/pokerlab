"""Which graded decisions have been looked at. A review list that never
shrinks is a list nobody finishes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path("data/reviewed.json")


class Marks:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self._marks: dict[str, str] = {}
        if path.exists():
            try:
                self._marks = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self._marks = {}

    @staticmethod
    def key(hand_id: str, idx: int) -> str:
        return f"{hand_id}:{idx}"

    def __contains__(self, key: str) -> bool:
        return key in self._marks

    def toggle(self, key: str) -> bool:
        """Flip one decision's mark; returns its new state."""
        if key in self._marks:
            del self._marks[key]
            self._save()
            return False
        self._marks[key] = datetime.now(timezone.utc).isoformat()
        self._save()
        return True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._marks, indent=1))
        tmp.replace(self.path)
