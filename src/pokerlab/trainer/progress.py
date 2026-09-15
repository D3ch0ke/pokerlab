"""Persistent drill progress with SM-2 style scheduling.

Adapted from SM-2 in one way that matters: intervals are counted in
*questions answered*, not days. You drill in bursts rather than daily, so a
day-based schedule would either bury everything as overdue or space nothing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("data/trainer_progress.json")

MIN_EASE = 1.3
START_EASE = 2.5
#: First two intervals, in questions, before ease takes over.
STEPS = (3, 12)


@dataclass
class Card:
    key: str
    reps: int = 0
    lapses: int = 0
    ease: float = START_EASE
    interval: int = 0
    due: int = 0
    seen: int = 0
    right: int = 0

    def review(self, correct: bool, counter: int) -> None:
        self.seen += 1
        self.right += correct
        if correct:
            if self.reps < len(STEPS):
                self.interval = STEPS[self.reps]
            else:
                self.interval = max(1, round(self.interval * self.ease))
            self.reps += 1
            self.ease = min(3.0, self.ease + 0.1)
        else:
            self.lapses += 1
            self.reps = 0
            self.interval = 1
            self.ease = max(MIN_EASE, self.ease - 0.25)
        self.due = counter + self.interval

    @property
    def accuracy(self) -> float | None:
        return self.right / self.seen if self.seen else None


@dataclass
class Progress:
    counter: int = 0
    cards: dict[str, Card] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Progress":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cls()  # corrupt progress must never block a drill
        return cls(counter=raw.get("counter", 0),
                   cards={k: Card(**v) for k, v in raw.get("cards", {}).items()})

    def save(self, path: Path = DEFAULT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"counter": self.counter, "cards": {k: asdict(v) for k, v in self.cards.items()}},
            indent=1))
        tmp.replace(path)  # atomic: a crash mid-write must not lose history

    def card(self, key: str) -> Card:
        return self.cards.setdefault(key, Card(key=key))

    def review(self, key: str, correct: bool) -> Card:
        self.counter += 1
        c = self.card(key)
        c.review(correct, self.counter)
        return c

    def due(self) -> list[Card]:
        """Cards whose interval has elapsed, worst-remembered first."""
        return sorted((c for c in self.cards.values() if c.due <= self.counter),
                      key=lambda c: (c.accuracy if c.seen else 0, -c.lapses))

    @property
    def stats(self) -> dict:
        seen = sum(c.seen for c in self.cards.values())
        right = sum(c.right for c in self.cards.values())
        return {
            "cards": len(self.cards),
            "answered": seen,
            "accuracy": right / seen if seen else 0.0,
            "due_now": sum(1 for c in self.cards.values() if c.due <= self.counter),
            "struggling": sum(1 for c in self.cards.values() if c.lapses >= 2),
        }
