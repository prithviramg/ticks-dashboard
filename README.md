# NIFTY straddle dashboard

Interactive daily charts of NIFTY spot plus the straddle premium (CE + PE LTP), or either leg alone, of
the strikes around the opening ATM. Built from the trading engine's tick archive and orders DB.

Live: https://prithviramg.github.io/ticks-dashboard/

## The page
- **Day:** dropdown grouped by month, with ◀ ▶ to step through days. The URL keeps the day (`#2026-10-06`).
  The site keeps the newest **44 trading days** (FIFO); the oldest day is dropped when a new one arrives.
- **Expiry:** *This week* (nearest expiry, same-day on expiry days), *Next week*, *This month* and
  *Next month*. These are the four sets the engine subscribes. Monthly contracts are recognised by
  Kite's symbol (`NIFTY26OCT…`). In a month's last week, *This week* and *This month* are the same contract.
- **Axes:** NIFTY spot on the left (integer ticks), option premium on the right.
- **Straddles:** *Raw* or an EMA over 3 / 5 / 10 minutes of whatever premium is plotted. It's computed in the browser
  (`alpha = 2 / (span + 1)`, span = minutes ÷ 30 s, matching pandas `ewm(adjust=False)`). Spot always stays raw.
- **Memory:** about 4 MB of browser memory per day viewed. The page keeps only the 8 most recently viewed
  days in memory and re-downloads older ones (~240 kB gzipped).
- **Strikes:** ±5 / ±10 / ±15 around ATM. The strike set is fixed for the day from the first spot tick at
  or after 09:15.
- **Strike panel** (right of the chart; under it on narrow screens): one row per strike, highest first,
  with **CE** and **PE** checkboxes.
  - Both checked: the straddle (CE + PE), solid line.
  - One checked: only that leg, dashed (CE) or dotted (PE), in the strike's colour.
  - Click the strike itself to turn both legs on (or off, if both are already on).
  - Picks are kept per day and expiry while the page is open, and zoom survives toggling strikes.
- **Default visibility:** only NIFTY spot and the strikes traded that day (★, both legs) are shown.
- **Trades:** ▼ SELL / ▲ BUY markers sit on the traded strike's line; hover for leg, price, time and
  strategy. With one leg shown, only that leg's trades are marked.
  Trades are FILLED engine orders (paper and live) from `/opt/options-cpp/data/trading.db`.
- **Time range:** 09:00–15:30. Spot includes pre-open (09:00–09:08); straddles start at 09:15, since
  options don't trade before that. Each point is the last LTP in a 30 s bucket.

## Layout
- `build_dashboard.py` writes `docs/data/YYYY-MM-DD.json` (about 1.2 MB for 4 expiries, with CE and PE
  stored separately; Pages serves it gzipped, ≤ ~53 MB for 44 days) and `docs/data/days.json`.
  - `--missing` only looks at the newest `--keep` archive dates, so pruned days are never rebuilt.
  - It decodes `/opt/options-cpp/data/archive/ticks-YYYY-MM-DD*.ticks.zst` with the engine's `tickdump`
    tool and reads orders read-only from `trading.db`.
  - Days with no market data (holidays) are recorded in `skipped-dates.txt` and not retried.
- `docs/index.html` + `docs/app.js` are a static page that renders the JSON with Plotly.js.
  `buildFigure()` and `strikeRows()` in `app.js` are pure and can be tested with node.
- `publish.sh` builds any missing days, amends the single commit and force-pushes, so the repo always
  has exactly one commit. GitHub Pages serves `main:/docs`.

## Usage
```bash
/home/prithvi/venv/bin/python3 build_dashboard.py --missing           # build all new days
/home/prithvi/venv/bin/python3 build_dashboard.py --date 2026-10-06   # rebuild one day
    # options: --keep 44 (days kept) --strikes 15 (stored per side) --step 50 --interval 30s --db PATH
./publish.sh                                                          # build + amend + force push
```

## Schedule (crontab)
```
35 16 * * 1-5 /home/prithvi/git/ticks-dashboard/publish.sh >> /home/prithvi/claude_workarea/ticks-dashboard/logs/publish.log 2>&1
```
The engine archives the day's ticks at 16:00 IST. Any day it misses is picked up on the next run.
