"""Phase 1 acceptance test: does the money model reproduce the files?

For every hand we recompute contributions from the action stream and assert
against the file's own Total Pot and Rake. A failure here means a silently
wrong report later, so this gates everything downstream.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pokerlab.parse.betclic import ParseError, parse_hand           # noqa: E402
from pokerlab.parse.corpus import NL5, collect, default_sources                      # noqa: E402


def main() -> int:
    sources = default_sources()
    blocks = {k: v for k, v in collect(sources).items() if f"Game Name: {NL5}" in v}
    print(f"NL5 hands after dedupe: {len(blocks)}")

    ok = 0
    fails: Counter[str] = Counter()
    examples: dict[str, tuple] = {}

    for hid, block in blocks.items():
        try:
            h = parse_hand(block)
        except (ParseError, ValueError, KeyError) as exc:
            fails["parse-error"] += 1
            examples.setdefault("parse-error", (hid, str(exc)))
            continue

        contributed = sum(h.contributions.values())
        collected = sum(h.collected.values())
        problems = []
        if h.uncalled < 0:
            problems.append("negative-uncalled")
        if contributed - h.uncalled != h.total_pot:
            problems.append("pot-mismatch")
        if collected != h.total_pot - h.rake:
            problems.append("payout-mismatch")
        if sum(h.net(s.name) for s in h.seats) != -h.rake:
            problems.append("zero-sum-violation")

        if problems:
            for p in problems:
                fails[p] += 1
                examples.setdefault(p, (hid, contributed, h.total_pot, collected, h.rake))
        else:
            ok += 1

    print(f"RECONCILED: {ok} / {len(blocks)}")
    for reason, count in fails.most_common():
        print(f"  {reason:22s} {count:5d}   e.g. {examples[reason]}")
    return 0 if ok == len(blocks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
