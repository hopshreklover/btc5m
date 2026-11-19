"""How much do long-shot buyers overpay in a series, and does a resting bid on the
favourite capture it? Pure Python (runs on the droplet without numpy).

  python research/maker_scan.py DIR [LENGTH_S] [LO HI]

For each market: per second, the real taker buys of each side (from the stored
prints). A resting offer on the long shot at q (= a bid on the favourite at 1-q)
is taken to sit at the cheapest price long-shot buyers paid that second.
  strict:   filled only if buyers also paid more than q that second (swept through)
  generous: filled whenever buyers paid q
One fill per market (the earliest, either side). Return per $ of our capital:
(q - long shot won) / (1 - q). Also: what buying the long shot as a taker earns.
Results are split into the earlier 2/3 and the later 1/3 of the period.
"""
import gzip
import json
import math
import sys
from pathlib import Path

FEE = 0.07


def markets(directory):
    for path in sorted(Path(directory).glob("windows-*.jsonl.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        pass
        except (EOFError, OSError):
            pass


def scan(directory, length=300, lo=0.10, hi=0.20):
    t0, t1 = 8, length - 60
    rows = []                                   # (start, rule, r) and taker
    seen = set()
    for w in markets(directory):
        if w["start"] in seen:
            continue
        seen.add(w["start"])
        up_won = 1.0 if w["winner"] == "UP" else 0.0
        by_sec = {}
        for sec, d, u, size in w["prints"]:
            if not t0 <= sec < t1:
                continue
            p = u / 1000.0
            side, price = ("up", p) if d else ("dn", round(1 - p, 4))
            by_sec.setdefault(sec, {"up": [], "dn": []})[side].append(price)
        first = {"strict": None, "generous": None}
        taker = {}
        for sec in sorted(by_sec):
            for side in ("up", "dn"):
                buys = by_sec[sec][side]
                if not buys:
                    continue
                q = min(buys)
                if not lo <= q < hi:
                    continue
                won = up_won if side == "up" else 1 - up_won
                if side not in taker:
                    cost = q + FEE * q * (1 - q)
                    taker[side] = (won - cost) / cost
                r = (q - won) / (1 - q)
                if first["generous"] is None:
                    first["generous"] = r
                if first["strict"] is None and max(buys) > q + 1e-9:
                    first["strict"] = r
        rows.append((w["start"], first["strict"], first["generous"], list(taker.values())))
    return sorted(rows)


def stats(vals):
    n = len(vals)
    if n < 2:
        return 0.0, 0.0, n
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    return m, sd / math.sqrt(n), n


if __name__ == "__main__":
    directory = sys.argv[1]
    length = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    lo, hi = (float(sys.argv[3]), float(sys.argv[4])) if len(sys.argv) > 4 else (0.10, 0.20)
    rows = scan(directory, length, lo, hi)
    if not rows:
        print("no markets")
        sys.exit()
    cut = rows[int(len(rows) * 2 / 3)][0]
    hours_all = len(rows) * length / 3600
    print(f"{directory}: {len(rows)} markets ({hours_all:.0f} h), long shots {lo:.2f}-{hi:.2f}")
    for label, part in (("earlier 2/3", [r for r in rows if r[0] < cut]),
                        ("later 1/3", [r for r in rows if r[0] >= cut])):
        hours = len(part) * length / 3600
        line = f"  {label:11s} "
        for k, name in ((1, "strict"), (2, "generous")):
            v = [r[k] for r in part if r[k] is not None]
            m, se, n = stats(v)
            line += (f"{name} {m * 100:+6.2f}% +-{se * 100:4.2f} ({n / hours:4.1f}/h, $ {m * 10 * n:+6.0f}) | ")
        t = [x for r in part for x in r[3]]
        m, se, n = stats(t)
        line += f"long-shot taker {m * 100:+6.1f}% +-{se * 100:3.1f}"
        print(line)
