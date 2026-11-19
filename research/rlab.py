"""Rule lab: evaluate entry rules on probe arrays (see probe.py).

A row is one second in which real trades happened: what the model thought
(p, p_lo, p_hi for Up), the cheapest price that really traded for each side
and how much, and who officially won.
"""
import numpy as np

COLS = ["start", "t", "p", "p_lo", "p_hi", "sigma", "ask_up", "size_up", "ask_dn", "size_dn",
        "spot", "strike", "up_won", "crossed", "vwap_up", "max_up", "vwap_dn", "max_dn"]
FEE = 0.07


def load(path):
    """A probe array, from the current folder or data/research/npy/."""
    import os
    if not os.path.exists(path):
        path = os.path.join("data", "research", "npy", path)
    a = np.load(path)
    d = {c: a[:, i] for i, c in enumerate(COLS)}
    d["n"] = len(a)
    return d


def fee(price):
    return FEE * price * (1.0 - price)


SKIP_CROSSED = False
FILL = "best"     # which traded price the bot pays: best (cheapest), vwap or worst


def sides(d, shrink=0.0, pess=True):
    """Per row and side: price, size, model probability used, edge after fee, won.
    shrink pulls the model probability toward the traded price (0 = pure model)."""
    out = {}
    for side in ("up", "dn"):
        ask = d["ask_" + side]
        if FILL == "vwap" and "vwap_" + side in d:
            ask = d["vwap_" + side]
        elif FILL == "worst" and "max_" + side in d:
            ask = d["max_" + side]
        if side == "up":
            p = d["p_lo"] if pess else d["p"]
            won = d["up_won"]
        else:
            p = 1.0 - (d["p_hi"] if pess else d["p"])
            won = 1.0 - d["up_won"]
        p = (1.0 - shrink) * p + shrink * ask
        out[side] = dict(ask=ask, size=d["size_" + side], p=p, edge=p - ask - fee(ask), won=won)
    return out


def simulate(d, theta=0.03, t0=8.0, t1=294.0, pmin=0.08, pmax=0.92, k=3, stake=10.0,
             shrink=0.0, pess=True, cooldown=2.0, p_cap=0.97, extra=None, min_shares=5.0):
    """Greedy per-window replay of a threshold rule. Returns per-window P&L etc."""
    s = sides(d, shrink, pess)
    ok = {}
    for side in ("up", "dn"):
        x = s[side]
        e = np.minimum(x["p"], p_cap) - x["ask"] - fee(x["ask"])
        m = (~np.isnan(x["ask"])) & (e >= theta) & (d["t"] >= t0) & (d["t"] <= t1)
        m &= (x["ask"] >= pmin) & (x["ask"] <= pmax)
        if SKIP_CROSSED:          # look-ahead in trade-print replays: leave off
            m &= d["crossed"] == 0
        if extra is not None:
            m &= extra(d, side, x)
        ok[side] = (m, e)
    cand = ok["up"][0] | ok["dn"][0]
    idx = np.nonzero(cand)[0]
    pnl_by_win, trades = {}, []
    cur = None
    for i in idx:
        w = d["start"][i]
        if cur is None or cur[0] != w:
            cur = [w, 0, None, -1e9]          # window, n trades, side, last t
        if cur[1] >= k or d["t"][i] - cur[3] < cooldown:
            continue
        eu = ok["up"][1][i] if ok["up"][0][i] else -9
        ed = ok["dn"][1][i] if ok["dn"][0][i] else -9
        side = "up" if eu >= ed else "dn"
        if cur[2] is not None and side != cur[2]:
            other = cur[2]
            if not ok[other][0][i]:
                continue
            side = other
        x = s[side]
        a = x["ask"][i]
        cost = a + fee(a)
        shares = min(stake / cost, x["size"][i])
        if shares < min_shares:
            continue
        pnl = shares * (x["won"][i] - cost)
        cur[1] += 1; cur[2] = side; cur[3] = d["t"][i]
        pnl_by_win[w] = pnl_by_win.get(w, 0.0) + pnl
        trades.append((w, d["t"][i], a, x["p"][i], shares * cost, pnl, x["won"][i]))
    tr = np.array(trades) if trades else np.zeros((0, 7))
    wp = np.array(list(pnl_by_win.values()))
    return tr, wp


def stats(tr, wp, hours):
    if not len(tr):
        return dict(trades=0, markets=0, per_hour=0, staked=0, pnl=0, ret=0, se=0, t=0, win=0)
    staked = tr[:, 4].sum()
    pnl = tr[:, 5].sum()
    se = wp.std(ddof=1) * np.sqrt(len(wp)) if len(wp) > 1 else 0.0
    return dict(trades=len(tr), markets=len(wp), per_hour=round(len(tr) / hours, 2),
                mkts_per_hour=round(len(wp) / hours, 2), staked=round(staked),
                pnl=round(pnl, 1), ret=round(pnl / staked * 100, 2),
                se_ret=round(se / staked * 100, 2), t=round(pnl / se, 2) if se else 0,
                win=round((wp > 0).mean() * 100, 1), avg_price=round(tr[:, 2].mean(), 3))


def show(label, st):
    print(f"{label:<44} trades {st.get('trades', 0):5d} ({st.get('per_hour', 0):5.2f}/h, "
          f"{st.get('mkts_per_hour', 0):4.2f} mkts/h) staked {st.get('staked', 0):7.0f} "
          f"pnl {st.get('pnl', 0):8.1f} ret {st.get('ret', 0):6.2f}% +-{st.get('se_ret', 0):5.2f} "
          f"t {st.get('t', 0):5.2f} win {st.get('win', 0):5.1f}% px {st.get('avg_price', 0)}")


def grid_table(d, value="edge_real", tb=(8, 60, 120, 180, 240, 270, 294), pb=(0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95),
               mask=None, pess=True):
    """If you bought every real print in a (time, price) cell: realised edge per
    share after fees (won - price - fee), in cents, with the count of windows."""
    s = sides(d, 0.0, pess)
    print("rows: seconds into the market; columns: price paid. cell: realised cents/share after fee (n windows)")
    print("          " + "".join(f"{pb[j]:.2f}-{pb[j + 1]:.2f}      " for j in range(len(pb) - 1)))
    for i in range(len(tb) - 1):
        line = f"{tb[i]:3d}-{tb[i + 1]:3d}  "
        for j in range(len(pb) - 1):
            vals, wins = [], set()
            for side in ("up", "dn"):
                x = s[side]
                m = (~np.isnan(x["ask"])) & (d["t"] >= tb[i]) & (d["t"] < tb[i + 1]) & (x["ask"] >= pb[j]) & (x["ask"] < pb[j + 1])
                if mask is not None:
                    m &= mask(d, side, x)
                # one observation per window and side: the mean over its rows
                st = d["start"][m]
                v = (x["won"][m] - x["ask"][m] - fee(x["ask"][m]))
                if len(st):
                    u, inv = np.unique(st, return_inverse=True)
                    vals.append(np.bincount(inv, v) / np.bincount(inv))
            if vals:
                v = np.concatenate(vals)
                line += f"{v.mean() * 100:+6.2f} ({len(v):5d}) "
            else:
                line += "      -        "
        print(line)


def maker_sim(d, margin=0.02, t0=8.0, t1=290.0, pmin=0.10, pmax=0.90, k=3, stake=10.0,
              use_bounds=True, cooldown=2.0, side_ok=("up", "dn")):
    """Post an offer on side S at q = (upper estimate of S's value) + margin, rounded up to
    a cent; i.e. a bid on the other side at 1-q. Filled only if, in that second, real
    takers bought S at a price strictly above q (they swept through our level, so our
    order at q was used up whatever its place in the queue). No maker fee.
    P&L per share = q - won_S. Size capped by the volume printed at the best level."""
    pnl_by_win, trades, cur = {}, [], None
    p_up_hi = d["p_hi"] if use_bounds else d["p"]
    p_up_lo = d["p_lo"] if use_bounds else d["p"]
    fair_hi = {"up": p_up_hi, "dn": 1.0 - p_up_lo}
    won = {"up": d["up_won"], "dn": 1.0 - d["up_won"]}
    q = {s: np.ceil((np.minimum(fair_hi[s], 0.99) + margin) * 100 - 1e-9) / 100 for s in side_ok}
    hit = {}
    for s in side_ok:
        worst = d["max_" + s]
        m = (~np.isnan(worst)) & (worst > q[s] + 1e-9) & (q[s] >= pmin) & (q[s] <= pmax)
        m &= (d["t"] >= t0) & (d["t"] <= t1)
        hit[s] = m
    any_hit = np.zeros(d["n"], bool)
    for s in side_ok:
        any_hit |= hit[s]
    for i in np.nonzero(any_hit)[0]:
        w = d["start"][i]
        if cur is None or cur[0] != w:
            cur = [w, 0, None, -1e9]
        if cur[1] >= k or d["t"][i] - cur[3] < cooldown:
            continue
        cands = [s for s in side_ok if hit[s][i] and (cur[2] is None or cur[2] == s)]
        if not cands:
            continue
        s = max(cands, key=lambda s: q[s][i] - fair_hi[s][i])
        qq = q[s][i]
        cost = 1.0 - qq                      # we are long the other side at 1-q
        shares = min(stake / cost, d["size_" + s][i])
        if shares < 5.0:
            continue
        pnl = shares * (qq - won[s][i])
        cur[1] += 1; cur[2] = s; cur[3] = d["t"][i]
        pnl_by_win[w] = pnl_by_win.get(w, 0.0) + pnl
        trades.append((w, d["t"][i], 1.0 - qq, 1.0 - fair_hi[s][i], shares * cost, pnl, 1.0 - won[s][i]))
    tr = np.array(trades) if trades else np.zeros((0, 7))
    return tr, np.array(list(pnl_by_win.values()))
