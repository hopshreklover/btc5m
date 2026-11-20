"""Nested markets: the last 5-minute market inside each 15-minute market ends at
the same moment. If both settle on the same final price, then

  strike5 <= strike15:  5m Up + 15m Down pays >= $1 (and $2 if final in between)
  strike5 >  strike15:  5m Down + 15m Up pays >= $1 (same)

so the pair is worth buying whenever it costs less than $1 plus the chance of
the $2 corridor. Pure Python (runs on the droplet).

  python research/nested_arb.py DIR15 DIR5 [DIR5 ...]
"""
import gzip
import json
import math
import sys
from pathlib import Path

FEE = 0.07


def load(dirs):
    out = {}
    for d in dirs:
        for path in sorted(Path(d).glob("windows-*.jsonl.gz")):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            w = json.loads(line)
                        except ValueError:
                            continue
                        out.setdefault(w["start"], w)
            except (EOFError, OSError):
                pass
    return out


def per_second(w):
    """sec -> {"up": [prices bought], "dn": [...]}"""
    out = {}
    for sec, d, u, size in w["prints"]:
        p = u / 1000.0
        side, price = ("up", p) if d else ("dn", round(1 - p, 4))
        out.setdefault(sec, {"up": [], "dn": []})[side].append(price)
    return out


def fee(p):
    return FEE * p * (1 - p)


def stats(v):
    n = len(v)
    if n < 2:
        return (sum(v) / n if n else 0.0), 0.0, n
    m = sum(v) / n
    return m, math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1) / n), n


if __name__ == "__main__":
    m15 = load([sys.argv[1]])
    m5 = load(sys.argv[2:])
    pairs, diffs = [], []
    for s15, w15 in sorted(m15.items()):
        w5 = m5.get(s15 + 600)
        if w5 is None or w15.get("final") is None or w5.get("final") is None:
            continue
        diffs.append(abs(float(w15["final"]) - float(w5["final"])))
        pairs.append((w15, w5))
    if not pairs:
        print("no overlapping markets")
        sys.exit()
    diffs.sort()
    print(f"{len(pairs)} nested pairs; |final15 - final5|: median {diffs[len(diffs) // 2]:.4f}, "
          f"max {diffs[-1]:.4f}, identical in {sum(d < 1e-6 for d in diffs)}")
    # pick the dominant pairing per market and collect each second's cost
    rows = []          # (start, sec, cost_best, cost_worst, payoff, gap)
    for w15, w5 in pairs:
        k15, k5 = float(w15["strike"]), float(w5["strike"])
        if k5 <= k15:
            leg5, leg15 = "up", "dn"
        else:
            leg5, leg15 = "dn", "up"
        won5 = (w5["winner"] == "UP") == (leg5 == "up")
        won15 = (w15["winner"] == "UP") == (leg15 == "up")
        payoff = int(won5) + int(won15)
        a, b = per_second(w5), per_second(w15)
        for sec in range(5, 295):
            x, y = a.get(sec, {}).get(leg5), b.get(sec + 600, {}).get(leg15)
            if not x or not y:
                continue
            best = min(x) + min(y) + fee(min(x)) + fee(min(y))
            worst = max(x) + max(y) + fee(max(x)) + fee(max(y))
            rows.append((w5["start"], sec, best, worst, payoff, abs(k5 - k15)))
    print(f"{len(rows)} seconds where both legs traded; payoff $2 in "
          f"{sum(1 for p in pairs if True) and 0} (see below)")
    n2 = sum(1 for w15, w5 in pairs if
             ((float(w5['strike']) <= float(w15['strike'])) and w5['winner'] == 'UP' and w15['winner'] == 'DOWN')
             or ((float(w5['strike']) > float(w15['strike'])) and w5['winner'] == 'DOWN' and w15['winner'] == 'UP'))
    n0 = sum(1 for w15, w5 in pairs if
             ((float(w5['strike']) <= float(w15['strike'])) and w5['winner'] == 'DOWN' and w15['winner'] == 'UP')
             or ((float(w5['strike']) > float(w15['strike'])) and w5['winner'] == 'UP' and w15['winner'] == 'DOWN'))
    print(f"pair paid $2 in {n2} of {len(pairs)} markets, $0 in {n0} (should be ~0 if finals match)")
    cut = sorted({r[0] for r in rows})
    cut = cut[int(len(cut) * 2 / 3)] if cut else 0
    hours = len(pairs) * 900 / 3600
    for fillname, k in (("best", 2), ("worst", 3)):
        for thr in (0.97, 1.00, 1.02, 1.05, 1.10):
            first = {}
            for r in rows:
                if r[k] <= thr and r[0] not in first:
                    first[r[0]] = r
            for part, sel in (("earlier", lambda s: s < cut), ("later", lambda s: s >= cut)):
                v = [(r[4] - r[k]) / r[k] for s, r in first.items() if sel(s)]
                m, se, n = stats(v)
                usd = sum((r[4] - r[k]) / r[k] * 10 for s, r in first.items() if sel(s))
                print(f"  [{fillname:5s}] cost <= {thr:.2f} {part:7s}: {n:4d} pairs ({n / hours * (3 if part == 'earlier' else 1.5):.2f}/h) "
                      f"return {m * 100:+6.2f}% +-{se * 100:4.2f}  $ {usd:+7.1f} at $10 per pair")
