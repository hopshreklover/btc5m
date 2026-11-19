"""The one-time test on last week of the candidates chosen on the three earlier weeks."""
import numpy as np

import rlab

TAKERS = {
    "A  current bot rule (edge 3c, 8-294s, 0.08-0.92, 3/market)": dict(),
    "B  best honest taker rule (edge 2c, last 90s, 1/market)": dict(theta=0.02, t0=210, k=1, pmin=0.08, pmax=0.92),
    "B' late rule that needed the look-ahead (1.5c, last 90s, 2/mkt)": dict(theta=0.015, t0=210, k=2, pmin=0.20, pmax=0.92),
}


def longshot_maker(d, rule, lo=0.10, hi=0.20, t0=8, t1=240):
    won = {"up": d["up_won"], "dn": 1 - d["up_won"]}
    rets = []
    for s in ("up", "dn"):
        q = d["ask_" + s]
        m = (~np.isnan(q)) & (q >= lo) & (q < hi) & (d["t"] >= t0) & (d["t"] < t1)
        if rule == "strict":
            m &= d["max_" + s] > q + 1e-9
        idx = np.nonzero(m)[0]
        _, first = np.unique(d["start"][idx], return_index=True)
        i = idx[first]
        rets.append((q[i] - won[s][i]) / (1 - q[i]))
    return np.concatenate(rets)


for prefix in ("tr", "te"):
    label = "TUNING WEEKS (Oct 19 - Nov 9)" if prefix == "tr" else "LAST WEEK, held out (Nov 9 - 16)"
    print(f"\n################ {label}")
    for feed in ("oracle", "spot"):
        stamps = ("2.3",) if feed == "oracle" else ("1.3", "2.3", "3.3")
        for st in stamps:
            d = rlab.load(f"{prefix}_{feed}_s{st}.npy")
            hours = len(np.unique(d["start"])) * 5 / 60
            print(f"=== feed: {'oracle (what the bot uses now)' if feed == 'oracle' else 'direct exchange'}, "
                  f"timestamp correction {st}s, {hours:.0f} h")
            for fill in ("best", "vwap", "worst"):
                rlab.FILL = fill
                for name, kw in TAKERS.items():
                    if feed == "oracle" and name.startswith("B"):
                        continue
                    tr, wp = rlab.simulate(d, **kw)
                    rlab.show(f"  [{fill:5s}] {name[:40]}", rlab.stats(tr, wp, hours))
            rlab.FILL = "best"
            if feed == "spot":
                for rule in ("generous", "strict"):
                    r = longshot_maker(d, rule)
                    print(f"  [{rule:8s}] C  resting offers vs long-shot buyers (0.10-0.20, 8-240s): "
                          f"{len(r)} fills ({len(r) / hours:.1f}/h), return per $ {r.mean() * 100:+.2f}% "
                          f"+- {r.std() / np.sqrt(len(r)) * 100:.2f}")
