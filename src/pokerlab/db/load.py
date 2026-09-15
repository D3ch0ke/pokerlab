"""Build the DuckDB analysis database from the raw exports."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from ..parse.betclic import ParseError, parse_hand
from ..parse.corpus import CASH_GAMES, collect, default_sources
from ..parse.model import Hand, Street, Verb
from ..positions import positions
from ..texture import classify

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path("data/pokerlab.duckdb")


def _secs(a: str, b: str) -> float:
    fmt = "%H:%M:%S"
    delta = (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds()
    return delta + 86400 if delta < 0 else delta  # midnight rollover


def _texture(hand: Hand):
    flop = hand.boards.get(Street.FLOP)
    if not flop or len(flop) < 3:
        return (None, None, None, None, None)
    t = classify(flop)
    return (t.paired, t.suitedness, t.high, t.connectivity, t.wet)


def _saw_flop(hand: Hand, hero: str) -> bool:
    """Did *hero* see a flop -- not merely "was a flop dealt"."""
    if Street.FLOP not in hand.boards:
        return False
    return not any(a.name == hero and a.verb is Verb.FOLD and a.street is Street.PREFLOP
                   for a in hand.actions)


def _hero_rake(hand: Hand, hero: str) -> int:
    """Hero's share of the rake, pro-rata on chips contributed.

    The file only reports the whole pot's rake. Attributing all of it to
    hero would overstate their cost several-fold in multiway pots.
    """
    contributions = hand.contributions
    total = sum(contributions.values())
    return round(hand.rake * contributions.get(hero, 0) / total) if total else 0


def _rows(hand: Hand):
    pos = positions(hand)
    hero = hand.hero
    hero_name = hero.name if hero else ""

    hand_row = (
        hand.hand_id, hand.game_name, hand.played_at, hand.table_id,
        hand.sb, hand.bb, hand.total_pot, hand.rake, hand.uncalled,
        len(hand.seats), hero_name, pos.get(hero_name),
        hand.net(hero_name) if hero else 0,
        " ".join(hand.hole_cards.get(hero_name, [])) or None,
        _hero_rake(hand, hero_name),
        _saw_flop(hand, hero_name),
        hero_name in hand.shown,
        hand.collected.get(hero_name, 0) > 0,
        " ".join(c for s in (Street.FLOP, Street.TURN, Street.RIVER)
                 for c in hand.boards.get(s, [])[-(1 if s is not Street.FLOP else 3):]) or None,
        *_texture(hand),
    )

    seat_rows = [
        (hand.hand_id, s.seat_no, s.name, s.stack, pos.get(s.name), s.is_hero, hand.net(s.name),
         " ".join(hand.shown.get(s.name, [])) or None)
        for s in hand.seats
    ]

    action_rows = []
    pot = 0
    prev_time: str | None = None
    for idx, a in enumerate(hand.actions):
        action_rows.append((
            hand.hand_id, idx, str(a.street), a.seat_no, a.name, pos.get(a.name),
            a.name == hero_name, str(a.verb), a.announced, a.contributed, a.effective,
            pot, a.all_in, a.dead,
            _secs(prev_time, a.time) if prev_time else None,
        ))
        pot += a.contributed
        prev_time = a.time

    return hand_row, seat_rows, action_rows


_ARROW = {
    "VARCHAR": pa.string(), "INTEGER": pa.int64(), "BIGINT": pa.int64(),
    "BOOLEAN": pa.bool_(), "DOUBLE": pa.float64(),
    "TIMESTAMP WITH TIME ZONE": pa.timestamp("us", tz="UTC"),
}


def _insert(con, table: str, rows: list[tuple]) -> None:
    """Bulk insert via a registered Arrow table.

    DuckDB's executemany binds one row at a time -- ~2 minutes for the 125k
    action rows, and chunked multi-row INSERT is barely better. Handing it a
    columnar Arrow table instead takes under 2 seconds.
    """
    if not rows:
        return
    types = [_ARROW[t] for _, t, *_ in con.execute(f"DESCRIBE {table}").fetchall()]
    staging = pa.table({
        f"c{i}": pa.array(col, type=typ)
        for i, (col, typ) in enumerate(zip(zip(*rows), types, strict=True))
    })
    con.register("staging", staging)
    con.execute(f"INSERT INTO {table} SELECT * FROM staging")
    con.unregister("staging")


def build(sources: list[Path] | None = None, db_path: Path = DEFAULT_DB,
          games: tuple[str, ...] = CASH_GAMES) -> dict[str, int]:
    sources = sources or default_sources()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA.read_text())

    blocks = collect(sources, games)
    hands, seats, actions, bad = [], [], [], []

    for hid, block in blocks.items():
        try:
            hand = parse_hand(block)
            h, s, a = _rows(hand)
        except (ParseError, ValueError, KeyError) as exc:
            bad.append((f"{type(exc).__name__}: {exc}", block))
            continue
        hands.append(h)
        seats.extend(s)
        actions.extend(a)

    _insert(con, "hands", hands)
    _insert(con, "seats", seats)
    _insert(con, "actions", actions)
    _insert(con, "quarantine", bad)
    con.close()

    return {"hands": len(hands), "actions": len(actions), "quarantined": len(bad),
            "coverage": round(100 * len(hands) / max(len(blocks), 1), 3)}
