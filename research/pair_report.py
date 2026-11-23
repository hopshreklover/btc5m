"""What did the nested-pair strategy see? Joins real-book snapshots with official results.

  python research/pair_report.py [DIR]     (default data/pairs; scans DIR and DIR/<coin>/)

For each folder with results:
  - how close the bot's own starting prices (from the oracle) were to the official
    ones, and whether they picked the same pairing;
  - for each cost threshold: in how many 15m markets the safe pairing could be
    bought at or under it, what that paid, and the size available;
  - the settled paper trades.
"""
import csv
import math
import sys
from pathlib import Path


def rows(path):
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def report(d):
    settled = {int(r["start15"]): r for r in rows(d / "settled.csv")}
    if not settled:
        return
    snaps = {}
    for p in sorted(d.glob("books-*.csv")):
        for r in rows(p):
            snaps.setdefault(int(r["start15"]), []).append(r)
    print(f"== {d}: {len(settled)} markets settled, {len(snaps)} with logged snapshots")

    errs5, errs15, agree, judged = [], [], 0, 0
    for st in settled.values():
        k5e, k15e = num(st.get("k5_est")), num(st.get("k15_est"))
        k5, k15 = float(st["strike5"]), float(st["strike15"])
        if k5e is not None:
            errs5.append(abs(k5e - k5) / k5 * 1e4)
        if k15e is not None:
            errs15.append(abs(k15e - k15) / k15 * 1e4)
        if k5e is not None and k15e is not None:
            judged += 1
            agree += (k5e <= k15e) == (k5 <= k15)
    if errs5 or errs15:
        def med(v):
            return sorted(v)[len(v) // 2] if v else float("nan")
        print(f"  own starting prices vs official (basis points): 5m median {med(errs5):.2f} "
              f"max {max(errs5, default=float('nan')):.2f}; 15m median {med(errs15):.2f} "
              f"max {max(errs15, default=float('nan')):.2f}; same pairing in {agree} of {judged}")

    for thr in (0.98, 1.00, 1.02, 1.05):
        rets, sizes = [], []
        for s, st in settled.items():
            leg = "a" if float(st["strike5"]) <= float(st["strike15"]) else "b"
            pay = ((st["winner5"] == "UP") + (st["winner15"] == "DOWN") if leg == "a"
                   else (st["winner5"] == "DOWN") + (st["winner15"] == "UP"))
            for r in snaps.get(s, []):
                cost = num(r.get(f"cost_{leg}"))
                if cost is not None and cost <= thr:
                    rets.append((pay - cost) / cost)
                    sizes.append(num(r.get(f"size_{leg}")) or 0.0)
                    break
        if rets:
            m = sum(rets) / len(rets)
            se = math.sqrt(sum((x - m) ** 2 for x in rets) / max(len(rets) - 1, 1) / len(rets))
            print(f"  safe pair cost <= {thr:.2f}: {len(rets)} of {len(settled)} markets, return "
                  f"{m * 100:+.2f}% +-{se * 100:.2f}, median shares {sorted(sizes)[len(sizes) // 2]:.0f}")
        else:
            print(f"  safe pair cost <= {thr:.2f}: none")

    res = rows(d / "results.csv")
    if res:
        pnl = sum(float(r["pnl"]) for r in res)
        staked = sum(float(r["shares"]) * float(r["cost"]) for r in res)
        wrong = sum(r["pair"] != r["official_pair"] for r in res)
        print(f"  paper trades settled: {len(res)}, P&L {pnl:+.2f} on {staked:.2f} staked, "
              f"wrong pairing {wrong}")


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "data/pairs")
    for d in [root] + sorted(p for p in root.iterdir() if p.is_dir()):
        report(d)
