# Research scripts

Run from the repository root with the project's Python. They read data that the
`history` and `check` commands download (kept out of git under `data/`):

- `data/research/old`, `last`, `new`: markets with real trades and official winners
  (copy `data/history*` folders from the droplet), plus `prices.csv` files.
- `data/research/npy/`: probe arrays made by `probe.py`.

| Script | What it does |
| --- | --- |
| `probe.py STORE PRICES LO HI OUT [feed_lag] [spot_lag or none] [twap] [stamp]` | Replay without trading; save, per second with real trades, what the model thought and the prices that traded. |
| `rlab.py` | Helpers for fast rule tests on probe arrays (`simulate`, `maker_sim`, `grid_table`). |
| `calib.py feed...` | Does the model add anything to the market's own price? (log loss, tuning vs test). |
| `rest2.py`, `rest_filter.py`, `holdout.py` | Resting-bid variants and filters on probe arrays. |
| `grid_rest.py SET`, `grid_offset.py SET` | Grids of the bot's own engine with the `rest` strategy (`SET` = tuning, last or fresh). |
| `volstudy.py`, `limits.py` | P&L by volatility / hour; effect of the daily loss limit. |

Method used so far: tune on the older weeks, then test once on later data the
tuning never saw; require at least 3 trades an hour; report the standard error.
