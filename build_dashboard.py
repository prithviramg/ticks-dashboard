#!/usr/bin/env python3
"""Build data for the straddle dashboard (GitHub Pages) from the daily tick archives.

For each trading day and each index in UNDERLYINGS (NIFTY, SENSEX) the engine archived, writes
docs/data/<index>/<date>.json with:
- The index spot, 09:00-15:30 IST.
- The CE and PE LTP (from 09:15) of ATM +/- N strikes for up to four expiry slots: this week, next
  week, this month and next month. The page plots either leg or their sum (the straddle premium).
- The day's filled orders in that index's options from the engine's trading.db.

The strike set is fixed from the 09:15 spot. Only the newest --keep trading days are kept (FIFO).
docs/index.html + docs/app.js render it with Plotly.js. Ticks are decoded with the engine's own
`tickdump` tool, which streams CSV, so memory stays small; each day is decoded once for all indexes.

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
# Indexes whose options the engine archives (kUnderlyings in options-cpp's include/trading/kite/Underlying.hpp),
# in the page's dropdown order. spot: tradingsymbol of the index; step: strike spacing in points.
UNDERLYINGS = {
    "NIFTY": dict(spot="NIFTY 50", step=50),
    "SENSEX": dict(spot="SENSEX", step=100),
}
# Kite option symbols after the index name: weekly 26O0622600CE (YY, month 1-9/O/N/D, DD) or monthly 26OCT22600CE.
# Requiring one right after the name excludes NIFTYNXT50, SENSEX50... (and str.match excludes BANKNIFTY).
WEEKLY_SUFFIX_RE = r"\d{2}[1-9OND][0-3]\d\d"
MONTHLY_SUFFIX_RE = r"\d{2}[A-Z]{3}\d"
ARCHIVE_RE = re.compile(r"^ticks-(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.ticks\.zst$")
DATA_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
# Days not retried by --missing: "DATE" had no market data at all (holiday), "DATE NAME" none for that index
# (not archived yet, or its contract list failed to load).
SKIPPED_FILE = "skipped-dates.txt"
CSV_COLUMNS = ["ts_ms", "tradingsymbol", "expiry", "strike", "option_type", "ltp"]
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_SQL = """
    SELECT o.timestamp, o.symbol, i.expiry, i.strike, i.option_type, o.side, o.price, o.quantity, o.mode,
           o.strategy_id
    FROM orders o JOIN instruments i ON i.tradingsymbol = o.symbol
    WHERE o.timestamp >= ? AND o.timestamp < ? AND o.status = 'FILLED'
      AND o.strategy_id <> 'expiry'  -- synthetic expiry-settlement rows
    ORDER BY o.timestamp"""


class NoMarketData(Exception):
    """The archive has no spot or option ticks of any index inside market hours (holiday, engine down)."""


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def ist_ms(date, hhmm):
    """Epoch milliseconds of `date` `hhmm` in IST."""
    return pd.Timestamp(f"{date} {hhmm}", tz=IST).value // 1_000_000


def ist_time(ts_ms):
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_convert(IST).strftime("%H:%M:%S")


def option_re(name):
    """Kite symbols of `name`'s weekly and monthly options."""
    return f"{name}(?:{WEEKLY_SUFFIX_RE}|{MONTHLY_SUFFIX_RE})"


def load_day(date, archive_dir, tickdump, start_ms, open_ms, close_ms, step_ms):
    """Stream one day's archives through tickdump once, splitting the ticks by index.

    Returns {name: (spot, options, monthly, open_tick)} for each index in UNDERLYINGS with spot and option
    ticks in market hours (empty on a holiday):
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

    spot_parts = {name: [] for name in UNDERLYINGS}
    opt_parts = {name: {} for name in UNDERLYINGS}
    monthly = {name: set() for name in UNDERLYINGS}
    open_tick = dict.fromkeys(UNDERLYINGS)
    proc = subprocess.Popen([tickdump, *files], stdout=subprocess.PIPE)
    try:
        reader = pd.read_csv(proc.stdout, usecols=CSV_COLUMNS, keep_default_na=False,
                             dtype={"expiry": str, "option_type": str, "tradingsymbol": str},
                             chunksize=200_000)
        for chunk in reader:
            chunk = chunk[(chunk["ts_ms"] >= start_ms) & (chunk["ts_ms"] <= close_ms)]
            chunk = chunk.assign(bucket=(chunk["ts_ms"] - start_ms) // step_ms)
            all_opts = chunk[(chunk["ts_ms"] >= open_ms) & chunk["option_type"].isin(["CE", "PE"])
                             & (chunk["expiry"] >= date) & (chunk["ltp"] > 0)]
            for name, underlying in UNDERLYINGS.items():
                spot = chunk[chunk["tradingsymbol"] == underlying["spot"]]
                opened = spot[spot["ts_ms"] >= open_ms]
                if not opened.empty and (open_tick[name] is None or opened["ts_ms"].min() < open_tick[name][0]):
                    row = opened.loc[opened["ts_ms"].idxmin()]
                    open_tick[name] = (row["ts_ms"], float(row["ltp"]))
                spot_parts[name].append(spot.drop_duplicates("bucket", keep="last")[["ts_ms", "ltp"]])

                opts = all_opts[all_opts["tradingsymbol"].str.match(option_re(name))]
                opts = opts.drop_duplicates(["expiry", "strike", "option_type", "bucket"], keep="last")
                is_monthly = opts["tradingsymbol"].str.match(name + MONTHLY_SUFFIX_RE)
                monthly[name].update(opts.loc[is_monthly, "expiry"].unique())
                for expiry, part in opts.groupby("expiry"):
                    opt_parts[name].setdefault(expiry, []).append(part[["ts_ms", "strike", "option_type", "ltp"]])
    finally:
        proc.stdout.close()
        if proc.wait() != 0:
            raise RuntimeError(f"tickdump exited with {proc.returncode}")

    days = {}
    for name in UNDERLYINGS:
        spot = pd.concat(spot_parts[name]).sort_values("ts_ms", kind="stable")
        if spot.empty or open_tick[name] is None or not opt_parts[name]:
            log(f"{date} {name}: no spot/option ticks between {MARKET_OPEN} and {MARKET_CLOSE}")
            continue
        options = {e: pd.concat(p).sort_values("ts_ms", kind="stable") for e, p in sorted(opt_parts[name].items())}
        log(f"{date} {name}: {len(spot):,} spot samples; expiries "
            + ", ".join(f"{e}{' monthly' if e in monthly[name] else ''} ({len(f):,} samples)"
                        for e, f in options.items()))
        days[name] = (spot, options, monthly[name], open_tick[name])
    return days


def load_trades(db_path, date, name, start_ms, step_ms, n_points):
    """Filled engine orders (paper and live) in `name`'s options placed on `date` (IST), with their contract.

    Each trade's `i` is its sample bucket, clamped to the chart.
    """
    if not os.path.exists(db_path):
        log(f"{date} {name}: WARNING no trading db at {db_path}; no trades shown")
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # engine is live: never write
    try:
        rows = con.execute(TRADES_SQL, (ist_ms(date, "00:00"), ist_ms(date, "00:00") + 86_400_000)).fetchall()
    finally:
        con.close()
    symbol_re = re.compile(option_re(name))
    trades = [dict(time=ist_time(ts), i=int(min(max((ts - start_ms) // step_ms, 0), n_points - 1)),
                   expiry=expiry, strike=f"{strike:g}", type=kind, side=side,
                   price=round(price, 2), qty=qty, mode=mode, strategy=strategy)
              for ts, symbol, expiry, strike, kind, side, price, qty, mode, strategy in rows
              if symbol_re.match(symbol)]
    log(f"{date} {name}: {len(trades)} filled orders"
        + (": " + ", ".join(sorted({f"{t['strike']} ({t['expiry']})" for t in trades})) if trades else ""))
    return trades


def sample(ts_ms, values, start_ms, step_ms, n):
    """Last value in each `step_ms` bucket from `start_ms`, forward-filled; NaN before the first tick."""
    s = pd.Series(np.asarray(values, dtype=float), index=(np.asarray(ts_ms) - start_ms) // step_ms)
    return s.groupby(level=0).last().reindex(range(n)).ffill().to_numpy()


def to_json_list(values):
    return [None if math.isnan(v) else round(float(v), 2) for v in values]


def build_day(date, args):
    """Write `<out>/data/<name>/<date>.json` for each index with data that day.

    Returns the indexes without data. Raises NoMarketData if none has any (holiday).
    """
    start_ms, open_ms, close_ms = (ist_ms(date, t) for t in (DAY_START, MARKET_OPEN, MARKET_CLOSE))
    step_ms = int(pd.Timedelta(args.interval) / pd.Timedelta(milliseconds=1))
    n_points = (close_ms - start_ms) // step_ms + 1
    days = load_day(date, args.archive_dir, args.tickdump, start_ms, open_ms, close_ms, step_ms)
    if not days:
        raise NoMarketData(f"{date}: no spot/option ticks of any index between {MARKET_OPEN} and {MARKET_CLOSE}")
    for name, ticks in days.items():
        write_day(date, name, ticks, args, start_ms, step_ms, n_points)
    return [name for name in UNDERLYINGS if name not in days]


def write_day(date, name, ticks, args, start_ms, step_ms, n_points):
    """Write `<out>/data/<name>/<date>.json` from `name`'s ticks (one entry of load_day) and orders."""
    spot, options, monthly, (open_ts, open_spot) = ticks
    trades = load_trades(args.db, date, name, start_ms, step_ms, n_points)
    step = UNDERLYINGS[name]["step"]
    atm = math.floor(open_spot / step + 0.5) * step

    # Expiry slots shown on the page. In a month's last week, this week and this month are one contract.
    weeklies, monthlies = sorted(options), sorted(monthly)
    slots = dict(cw=weeklies[0], nw=nth(weeklies, 1), cm=nth(monthlies, 0), nm=nth(monthlies, 1))
    expiries = []
    for expiry in sorted({e for e in slots.values() if e}):
        opts = options[expiry]
        traded = {float(t["strike"]) for t in trades if t["expiry"] == expiry}
        wanted = sorted({atm + k * step for k in range(-args.strikes, args.strikes + 1)} | traded)
        opts = opts.assign(strike=opts["strike"].round(2))
        strikes = {}
        for strike in wanted:
            legs = {}
            for kind in ("CE", "PE"):
                leg = opts[(opts["strike"] == strike) & (opts["option_type"] == kind)]
                if not leg.empty:
                    legs[kind] = sample(leg["ts_ms"], leg["ltp"], start_ms, step_ms, n_points)
            if len(legs) < 2:
                log(f"{date} {name}: WARNING {expiry} {strike:g} missing "
                    f"{'/'.join({'CE', 'PE'} - legs.keys())}, skipped")
                continue
            strikes[f"{strike:g}"] = {kind: to_json_list(legs[kind]) for kind in ("CE", "PE")}
        expiries.append(dict(expiry=expiry, monthly=expiry in monthly, legs=strikes))
    charted = {e["expiry"] for e in expiries}
    for t in trades:
        if t["expiry"] not in charted:
            log(f"{date} {name}: WARNING trade in {t['expiry']} {t['strike']} {t['type']} is not in any "
                f"week/month slot; not charted")

    day = dict(underlying=name, date=date, start=f"{date} {DAY_START}:00", step_ms=step_ms, n=int(n_points),
               open_spot=round(open_spot, 2), open_time=ist_time(open_ts), atm=atm, step=step,
               spot=to_json_list(sample(spot["ts_ms"], spot["ltp"], start_ms, step_ms, n_points)),
               slots=slots, expiries=expiries, trades=trades)
    path = day_path(args.out, name, date)
    with open(path, "w") as f:
        json.dump(day, f, separators=(",", ":"))
    log(f"{date} {name}: wrote {path} ({os.path.getsize(path) / 1e3:.0f} kB, open {open_spot:.2f}, ATM {atm:g}, "
        + ", ".join(f"{s}={e}" for s, e in slots.items()) + "; "
        + ", ".join(f"{e['expiry']}: {len(e['legs'])} strikes" for e in expiries) + ")")


def nth(seq, i):
    return seq[i] if i < len(seq) else None


def day_path(out_dir, name, date):
    return os.path.join(out_dir, "data", name, f"{date}.json")


def data_days(out_dir, name):
    """Days with built data for index `name`, newest first."""
    return sorted((m.group(1) for f in os.listdir(os.path.join(out_dir, "data", name))
                   if (m := DATA_RE.match(f))), reverse=True)


def prune(out_dir, name, keep):
    """FIFO: delete `name`'s day data beyond the newest `keep` trading days."""
    for date in data_days(out_dir, name)[keep:]:
        os.remove(day_path(out_dir, name, date))
        log(f"pruned {name} {date} (keeping the newest {keep} days)")


def write_manifest(out_dir):
    """Rewrite data/days.json: each index's days (newest first), which drive the page's dropdowns."""
    days = {name: data_days(out_dir, name) for name in UNDERLYINGS}
    with open(os.path.join(out_dir, "data", "days.json"), "w") as f:
        json.dump({"underlyings": days}, f)
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    log("manifest: " + ", ".join(f"{name} {len(d)} day(s)" for name, d in days.items()))


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
    p.add_argument("--interval", default="30s", help="sampling interval, pandas syntax (default 30s)")
    p.add_argument("--archive-dir", default="/opt/options-cpp/data/archive")
    p.add_argument("--tickdump", default="/opt/options-cpp/current/tickdump")
    p.add_argument("--db", default="/opt/options-cpp/data/trading.db", help="engine DB with orders")
    p.add_argument("--out", default=os.path.join(REPO_DIR, "docs"), help="site directory")
    args = p.parse_args()
    for name in UNDERLYINGS:
        os.makedirs(os.path.join(args.out, "data", name), exist_ok=True)

    skipped_path = os.path.join(REPO_DIR, SKIPPED_FILE)
    skipped = set(open(skipped_path).read().splitlines()) - {""} if os.path.exists(skipped_path) else set()

    def missing(date):
        """Indexes with neither data nor a skip record for `date`."""
        return [name for name in UNDERLYINGS
                if not os.path.exists(day_path(args.out, name, date)) and f"{date} {name}" not in skipped]

    if args.date:
        todo = [args.date]
    else:  # only the FIFO window, so days already pruned are never rebuilt
        recent = [d for d in archive_dates(args.archive_dir) if d not in skipped][-args.keep:]
        todo = [d for d in recent if missing(d)]
        log("missing: " + (", ".join(f"{d} ({' '.join(missing(d))})" for d in todo) or "none"))

    failed = False
    for date in todo:
        try:
            no_data = build_day(date, args)
            if no_data:
                log(f"{date}: no data for {', '.join(no_data)}; recorded in {SKIPPED_FILE}")
            skipped.update(f"{date} {name}" for name in no_data)
        except NoMarketData as e:
            log(f"{e}; recorded in {SKIPPED_FILE}")
            skipped.add(date)
        except Exception as e:  # keep building the other days; report failure at the end
            log(f"{date}: FAILED: {e!r}")
            failed = True
    with open(skipped_path, "w") as f:
        f.write("\n".join(sorted(skipped)) + "\n")
    for name in UNDERLYINGS:
        prune(args.out, name, args.keep)
    write_manifest(args.out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
