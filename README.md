# Straddle dashboard (NIFTY, SENSEX)

Interactive daily charts of an index's spot plus the straddle premium (CE + PE LTP), or either leg alone,
of the strikes around the opening ATM. Built from the trading engine's tick archive and orders DB.

Live: https://prithviramg.github.io/ticks-dashboard/

## The page
- **Index:** dropdown in the top-left corner: NIFTY (strikes 50 apart) or SENSEX (100 apart). Switching keeps
  the day if the other index has it, else shows its newest day. An index with no days yet is greyed out.
- **Day:** dropdown grouped by month, with ◀ ▶ to step through days. The URL keeps the index and day
  (`#SENSEX/2026-10-27`; older `#2026-10-06` links open NIFTY). The site keeps each index's newest
  **44 trading days** (FIFO); the oldest day is dropped when a new one arrives.
- **Expiry:** *This week* (nearest expiry, same-day on expiry days), *Next week*, *This month* and
  *Next month*: the expiries the engine subscribes. Monthly contracts are recognised by Kite's symbol
  (`NIFTY26OCT…`). In a month's last week, *This week* and *This month* are the same contract.
  SENSEX has only the two nearest weeklies, so *This month* is there only when one of them is the monthly,
  and *Next month* never is.
- **Axes:** index spot on the left (integer ticks), option premium on the right.
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
- **Default visibility:** only the index spot and the strikes traded that day (★, both legs) are shown.
- **Trades:** ▼ SELL / ▲ BUY markers sit on the traded strike's line; hover for leg, price, time and
  strategy. With one leg shown, only that leg's trades are marked.
  Trades are FILLED engine orders (paper and live) from `/opt/options-cpp/data/trading.db`.
- **Time range:** 09:00–15:30. Spot includes pre-open (09:00–09:08); straddles start at 09:15, since
  options don't trade before that. Each point is the last LTP in a 30 s bucket.

## Layout
- `build_dashboard.py` writes `docs/data/<INDEX>/YYYY-MM-DD.json` (about 1.2 MB for NIFTY's 4 expiries, with
  CE and PE stored separately; Pages serves it gzipped, ≤ ~53 MB for 44 days) and `docs/data/days.json`
  (each index's days).
  - The indexes are the `UNDERLYINGS` table at the top (spot symbol, strike step), mirroring `kUnderlyings`
    in options-cpp's `Underlying.hpp`. Add a line there to chart another index the engine archives.
  - `--missing` only looks at the newest `--keep` archive dates, so pruned days are never rebuilt.
  - It decodes `/opt/options-cpp/data/archive/ticks-YYYY-MM-DD*.ticks.zst` once per day with the engine's
    `tickdump` tool, splits the ticks by index, and reads orders read-only from `trading.db`.
  - Days with no market data (holidays) are recorded in `skipped-dates.txt` as `DATE` and not retried;
    an index with no ticks that day (not archived yet) as `DATE INDEX`.
- `docs/index.html` + `docs/app.js` are a static page that renders the JSON with Plotly.js.
  `buildFigure()` and `strikeRows()` in `app.js` are pure and can be tested with node.
- `publish.sh` builds any missing days, amends the single commit and force-pushes, so the repo always
  has exactly one commit. GitHub Pages serves `main:/docs`.

## Usage
```bash
/home/prithvi/venv/bin/python3 build_dashboard.py --missing           # build all new days
/home/prithvi/venv/bin/python3 build_dashboard.py --date 2026-10-06   # rebuild one day
    # options: --keep 44 (days kept) --strikes 15 (stored per side) --interval 30s --db PATH
./publish.sh                                                          # build + amend + force push
```

## Schedule (crontab)
```
10,40 16 * * 1-5 /home/prithvi/git/ticks-dashboard/publish.sh >> /home/prithvi/claude_workarea/ticks-dashboard/logs/publish.log 2>&1
```
The engine archives the day's ticks at 16:00 IST, usually done within a minute but up to ~8 minutes on slow
days. It writes a `.part` file and renames it when complete, so a run never reads a half-written archive.
The 16:40 run picks up a day whose archive wasn't ready at 16:10; otherwise it finds nothing to do and
doesn't push. Any day still missed is picked up on the next run.
