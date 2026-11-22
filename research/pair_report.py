"""What did the nested-pair monitor see? Joins real-book snapshots with official results.

  python research/pair_report.py [DIR]     (default data/pairs)

For each 15m market: the valid pairing (by the official strikes), the first
second its real ask cost was at or under each threshold, the size available,
and what it paid. Return per $ assumes both legs were bought at those asks.
"""
import csv
import math
import sys
from pathlib import Path


def rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


if __name__ == "__main__":
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "data/pairs")
    settled = {int(r["start15"]): r for r in rows(d / "settled.csv")} if (d / "settled.csv").exists() else {}
    snaps = {}
    for p in sorted(d.glob("books-*.csv")):
        for r in rows(p):
            snaps.setdefault(int(r["start15"]), []).append(r)
    print(f"{len(settled)} pairs settled, {len(snaps)} with logged snapshots")
    for thr in (0.98, 1.00, 1.02, 1.05):
        rets, sizes, n_pairs = [], [], 0
        for s, st in settled.items():
            k5, k15 = float(st["strike5"]), float(st["strike15"])
            leg = "a" if k5 <= k15 else "b"
            pay = ((st["winner5"] == "UP") + (st["winner15"] == "DOWN") if leg == "a"
                   else (st["winner5"] == "DOWN") + (st["winner15"] == "UP"))
            for r in snaps.get(s, []):
                cost = float(r[f"cost_{leg}"])
                if cost <= thr:
                    rets.append((pay - cost) / cost)
                    sizes.append(float(r[f"size_{leg}"]))
                    n_pairs += 1
                    break
        if rets:
            m = sum(rets) / len(rets)
            se = math.sqrt(sum((x - m) ** 2 for x in rets) / max(len(rets) - 1, 1) / len(rets))
            print(f"  cost <= {thr:.2f}: {n_pairs} of {len(settled)} pairs, return {m * 100:+.2f}% +-{se * 100:.2f}, "
                  f"median shares available {sorted(sizes)[len(sizes) // 2]:.0f}")
        else:
            print(f"  cost <= {thr:.2f}: none")
