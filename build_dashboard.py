#!/usr/bin/env python3
"""Build data for the NIFTY straddle dashboard (GitHub Pages) from the daily tick archives.

For each trading day, writes docs/data/<date>.json with:
- NIFTY spot, 09:00-15:30 IST.
- The CE and PE LTP (from 09:15) of ATM +/- N strikes for four expiry slots: this week, next week,
  this month and next month. The page plots either leg or their sum (the straddle premium).
- The day's filled orders from the engine's trading.db.

The strike set is fixed from the 09:15 spot. Only the newest --keep trading days are kept (FIFO).
docs/index.html + docs/app.js render it with Plotly.js. Ticks are decoded with the engine's own
`tickdump` tool, which streams CSV, so memory stays small.

Usage:
  build_dashboard.py --missing                 build every archived day without data yet
  build_dashboard.py --date 2026-10-06         (re)build one day
"""
import argparse
import datetime as dt
import glob
import json
import math
import os
import re
import sqlite3
import subprocess
import sys

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
DAY_START, MARKET_OPEN, MARKET_CLOSE = "09:00", "09:15", "15:30"  # spot from 09:00, options from 09:15
SPOT_SYMBOL = "NIFTY 50"
OPTION_SYMBOL_RE = r"NIFTY\d{2}"  # NIFTY weekly/monthly options; excludes NIFTYNXT50, BANKNIFTY...
MONTHLY_SYMBOL_RE = r"NIFTY\d{2}[A-Z]{3}\d"  # Kite monthly: NIFTY26OCT22600CE (weekly: NIFTY26O0622600CE)
ARCHIVE_RE = re.compile(r"^ticks-(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.ticks\.zst$")
DATA_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
SKIPPED_FILE = "skipped-dates.txt"  # days with no market data (holidays); not retried by --missing
CSV_COLUMNS = ["ts_ms", "tradingsymbol", "expiry", "strike", "option_type", "ltp"]
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_SQL = """
    SELECT o.timestamp, i.expiry, i.strike, i.option_type, o.side, o.price, o.quantity, o.mode,
           o.strategy_id
    FROM orders o JOIN instruments i ON i.tradingsymbol = o.symbol
    WHERE o.timestamp >= ? AND o.timestamp < ? AND o.status = 'FILLED'
      AND o.strategy_id <> 'expiry'  -- synthetic expiry-settlement rows
    ORDER BY o.timestamp"""


class NoMarketData(Exception):
    """The archive has no NIFTY spot or option ticks inside market hours (holiday, engine down)."""


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def ist_ms(date, hhmm):
    """Epoch milliseconds of `date` `hhmm` in IST."""
    return pd.Timestamp(f"{date} {hhmm}", tz=IST).value // 1_000_000


def ist_time(ts_ms):
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_convert(IST).strftime("%H:%M:%S")


def load_day(date, archive_dir, tickdump, start_ms, open_ms, close_ms, step_ms):
    """Stream one day's archives through tickdump.

    Returns (spot, options, monthly, open_tick):
    - spot: ts_ms/ltp from `start_ms`.
    - options: {expiry: frame of ts_ms/strike/option_type/ltp} from `open_ms`, for every expiry on or
      after `date`. The engine subscribes only the weekly/monthly expiries it trades.
    - monthly: the set of those expiries that are monthly contracts.
    - open_tick: (ts_ms, ltp) of the first spot tick at or after `open_ms`.

    Each chunk is reduced to the last tick per instrument and `step_ms` bucket, which is all `sample`
    needs, so memory is bounded by instruments x buckets.
    """
    files = sorted(glob.glob(os.path.join(archive_dir, f"ticks-{date}*.ticks.zst")))
    if not files:
        raise FileNotFoundError(f"no archive for {date} in {archive_dir}")
    log(f"{date}: decoding {', '.join(os.path.basename(f) for f in files)}")

    spot_parts, opt_parts, monthly, open_tick = [], {}, set(), None
    proc = subprocess.Popen([tickdump, *files], stdout=subprocess.PIPE)
    try:
        reader = pd.read_csv(proc.stdout, usecols=CSV_COLUMNS, keep_default_na=False,
                             dtype={"expiry": str, "option_type": str, "tradingsymbol": str},
                             chunksize=200_000)
        for chunk in reader:
            chunk = chunk[(chunk["ts_ms"] >= start_ms) & (chunk["ts_ms"] <= close_ms)]
            chunk = chunk.assign(bucket=(chunk["ts_ms"] - start_ms) // step_ms)
            spot = chunk[chunk["tradingsymbol"] == SPOT_SYMBOL]
            opened = spot[spot["ts_ms"] >= open_ms]
            if not opened.empty and (open_tick is None or opened["ts_ms"].min() < open_tick[0]):
                row = opened.loc[opened["ts_ms"].idxmin()]
                open_tick = (row["ts_ms"], float(row["ltp"]))
            spot_parts.append(spot.drop_duplicates("bucket", keep="last")[["ts_ms", "ltp"]])

            opts = chunk[(chunk["ts_ms"] >= open_ms) & chunk["option_type"].isin(["CE", "PE"])
                         & (chunk["expiry"] >= date)]
            opts = opts[opts["tradingsymbol"].str.match(OPTION_SYMBOL_RE) & (opts["ltp"] > 0)]
            opts = opts.drop_duplicates(["expiry", "strike", "option_type", "bucket"], keep="last")
            monthly.update(opts.loc[opts["tradingsymbol"].str.match(MONTHLY_SYMBOL_RE), "expiry"].unique())
            for expiry, part in opts.groupby("expiry"):
                opt_parts.setdefault(expiry, []).append(part[["ts_ms", "strike", "option_type", "ltp"]])
    finally:
        proc.stdout.close()
        if proc.wait() != 0:
            raise RuntimeError(f"tickdump exited with {proc.returncode}")

    spot = pd.concat(spot_parts).sort_values("ts_ms", kind="stable")
    if spot.empty or open_tick is None or not opt_parts:
        raise NoMarketData(f"{date}: no NIFTY spot/option ticks between {MARKET_OPEN} and {MARKET_CLOSE}")
    options = {e: pd.concat(p).sort_values("ts_ms", kind="stable") for e, p in sorted(opt_parts.items())}
    log(f"{date}: {len(spot):,} spot samples; expiries "
        + ", ".join(f"{e}{' monthly' if e in monthly else ''} ({len(f):,} samples)" for e, f in options.items()))
    return spot, options, monthly, open_tick


def load_trades(db_path, date, start_ms, step_ms, n_points):
    """Filled engine orders (paper and live) placed on `date` (IST), with their option contract.

    Each trade's `i` is its sample bucket, clamped to the chart.
    """
    if not os.path.exists(db_path):
        log(f"{date}: WARNING no trading db at {db_path}; no trades shown")
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # engine is live: never write
    try:
        rows = con.execute(TRADES_SQL, (ist_ms(date, "00:00"), ist_ms(date, "00:00") + 86_400_000)).fetchall()
    finally:
        con.close()
    trades = [dict(time=ist_time(ts), i=int(min(max((ts - start_ms) // step_ms, 0), n_points - 1)),
                   expiry=expiry, strike=f"{strike:g}", type=kind, side=side,
                   price=round(price, 2), qty=qty, mode=mode, strategy=strategy)
              for ts, expiry, strike, kind, side, price, qty, mode, strategy in rows]
    log(f"{date}: {len(trades)} filled orders"
        + (": " + ", ".join(sorted({f"{t['strike']} ({t['expiry']})" for t in trades})) if trades else ""))
    return trades


def sample(ts_ms, values, start_ms, step_ms, n):
    """Last value in each `step_ms` bucket from `start_ms`, forward-filled; NaN before the first tick."""
    s = pd.Series(np.asarray(values, dtype=float), index=(np.asarray(ts_ms) - start_ms) // step_ms)
    return s.groupby(level=0).last().reindex(range(n)).ffill().to_numpy()


def to_json_list(values):
    return [None if math.isnan(v) else round(float(v), 2) for v in values]


def build_day(date, args):
    """Write `<out>/data/<date>.json`. Raises NoMarketData on days without trading."""
    start_ms, open_ms, close_ms = (ist_ms(date, t) for t in (DAY_START, MARKET_OPEN, MARKET_CLOSE))
    step_ms = int(pd.Timedelta(args.interval) / pd.Timedelta(milliseconds=1))
    n_points = (close_ms - start_ms) // step_ms + 1
    trades = load_trades(args.db, date, start_ms, step_ms, n_points)
    spot, options, monthly, (open_ts, open_spot) = load_day(date, args.archive_dir, args.tickdump,
                                                            start_ms, open_ms, close_ms, step_ms)
    atm = math.floor(open_spot / args.step + 0.5) * args.step

    # Expiry slots shown on the page. In a month's last week, this week and this month are one contract.
    weeklies, monthlies = sorted(options), sorted(monthly)
    slots = dict(cw=weeklies[0], nw=nth(weeklies, 1), cm=nth(monthlies, 0), nm=nth(monthlies, 1))
    expiries = []
    for expiry in sorted({e for e in slots.values() if e}):
        opts = options[expiry]
        traded = {float(t["strike"]) for t in trades if t["expiry"] == expiry}
        wanted = sorted({atm + k * args.step for k in range(-args.strikes, args.strikes + 1)} | traded)
        opts = opts.assign(strike=opts["strike"].round(2))
        strikes = {}
        for strike in wanted:
            legs = {}
            for kind in ("CE", "PE"):
                leg = opts[(opts["strike"] == strike) & (opts["option_type"] == kind)]
                if not leg.empty:
                    legs[kind] = sample(leg["ts_ms"], leg["ltp"], start_ms, step_ms, n_points)
            if len(legs) < 2:
                log(f"{date}: WARNING {expiry} {strike:g} missing {'/'.join({'CE', 'PE'} - legs.keys())}, skipped")
                continue
            strikes[f"{strike:g}"] = {kind: to_json_list(legs[kind]) for kind in ("CE", "PE")}
        expiries.append(dict(expiry=expiry, monthly=expiry in monthly, legs=strikes))
    charted = {e["expiry"] for e in expiries}
    for t in trades:
        if t["expiry"] not in charted:
            log(f"{date}: WARNING trade in {t['expiry']} {t['strike']} {t['type']} is not in any "
                f"week/month slot; not charted")

    day = dict(date=date, start=f"{date} {DAY_START}:00", step_ms=step_ms, n=int(n_points),
               open_spot=round(open_spot, 2), open_time=ist_time(open_ts), atm=atm, step=args.step,
               spot=to_json_list(sample(spot["ts_ms"], spot["ltp"], start_ms, step_ms, n_points)),
               slots=slots, expiries=expiries, trades=trades)
    path = os.path.join(args.out, "data", f"{date}.json")
    with open(path, "w") as f:
        json.dump(day, f, separators=(",", ":"))
    log(f"{date}: wrote {path} ({os.path.getsize(path) / 1e3:.0f} kB, open {open_spot:.2f}, ATM {atm:g}, "
        + ", ".join(f"{s}={e}" for s, e in slots.items()) + "; "
        + ", ".join(f"{e['expiry']}: {len(e['legs'])} strikes" for e in expiries) + ")")


def nth(seq, i):
    return seq[i] if i < len(seq) else None


def data_days(out_dir):
    """Days with built data, newest first."""
    return sorted((m.group(1) for f in os.listdir(os.path.join(out_dir, "data"))
                   if (m := DATA_RE.match(f))), reverse=True)


def prune(out_dir, keep):
    """FIFO: delete day data beyond the newest `keep` trading days."""
    for date in data_days(out_dir)[keep:]:
        os.remove(os.path.join(out_dir, "data", f"{date}.json"))
        log(f"pruned {date} (keeping the newest {keep} days)")


def write_manifest(out_dir):
    """Rewrite data/days.json (newest first), which drives the page's date dropdown."""
    days = data_days(out_dir)
    with open(os.path.join(out_dir, "data", "days.json"), "w") as f:
        json.dump({"days": days}, f)
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    log(f"manifest: {len(days)} day(s)")


def archive_dates(archive_dir):
    return sorted({m.group(1) for f in os.listdir(archive_dir) if (m := ARCHIVE_RE.match(f))})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--date", help="build this day (YYYY-MM-DD), overwriting its data")
    which.add_argument("--missing", action="store_true",
                       help="build the newest --keep archived days that have no data yet")
    p.add_argument("--keep", type=int, default=44,
                   help="trading days kept on the site; the oldest is dropped first (default 44)")
    p.add_argument("--strikes", type=int, default=15,
                   help="strikes stored on each side of ATM (default 15; the page shows 5/10/15)")
    p.add_argument("--step", type=int, default=50, help="strike spacing in points (default 50)")
    p.add_argument("--interval", default="30s", help="sampling interval, pandas syntax (default 30s)")
    p.add_argument("--archive-dir", default="/opt/options-cpp/data/archive")
    p.add_argument("--tickdump", default="/opt/options-cpp/current/tickdump")
    p.add_argument("--db", default="/opt/options-cpp/data/trading.db", help="engine DB with orders")
    p.add_argument("--out", default=os.path.join(REPO_DIR, "docs"), help="site directory")
    args = p.parse_args()
    os.makedirs(os.path.join(args.out, "data"), exist_ok=True)

    skipped_path = os.path.join(REPO_DIR, SKIPPED_FILE)
    skipped = set(open(skipped_path).read().split()) if os.path.exists(skipped_path) else set()
    if args.date:
        todo = [args.date]
    else:  # only the FIFO window, so days already pruned are never rebuilt
        recent = [d for d in archive_dates(args.archive_dir) if d not in skipped][-args.keep:]
        todo = [d for d in recent if not os.path.exists(os.path.join(args.out, "data", f"{d}.json"))]
        log(f"missing: {', '.join(todo) or 'none'}")

    failed = False
    for date in todo:
        try:
            build_day(date, args)
        except NoMarketData as e:
            log(f"{e}; recorded in {SKIPPED_FILE}")
            skipped.add(date)
            with open(skipped_path, "w") as f:
                f.write("\n".join(sorted(skipped)) + "\n")
        except Exception as e:  # keep building the other days; report failure at the end
            log(f"{date}: FAILED: {e!r}")
            failed = True
    prune(args.out, args.keep)
    write_manifest(args.out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
