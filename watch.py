import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from indicators import compute_macd, compute_rsi

WATCHLIST_PATH = Path.home() / ".config" / "watch" / "watchlist.json"
CACHE_DIR = Path.home() / ".cache" / "watch"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_RSI_OVERBOUGHT = 70
DEFAULT_RSI_OVERSOLD = 30
DEFAULT_MACD_SIGNAL_GAP = 0.0
POLL_INTERVAL = 60

ALERT_COOLDOWN_MINUTES = 15

def _load_watchlist():
    if not WATCHLIST_PATH.exists():
        return []
    try:
        with open(WATCHLIST_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

def _save_watchlist(entries):
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(entries, f, indent=2)

def _fetch_yahoo(ticker):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?"
        f"interval=1d&range=3mo&includeAdjustedClose=true"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    r = httpx.get(url, headers=headers, timeout=15.0)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    prices = result["indicators"]["adjclose"][0]["adjclose"]
    timestamps = result["timestamp"]
    return timestamps, prices

def _check_alerts(entry, timestamps, prices):
    alerts = []
    ticker = entry["ticker"]
    rsi_over = entry.get("rsi_overbought", DEFAULT_RSI_OVERBOUGHT)
    rsi_under = entry.get("rsi_oversold", DEFAULT_RSI_OVERSOLD)
    macd_gap = entry.get("macd_signal_gap", DEFAULT_MACD_SIGNAL_GAP)

    rsi_values = compute_rsi(prices)
    if rsi_values:
        rsi = rsi_values[-1]
        if rsi > rsi_over:
            alerts.append(f"RSI overbought {rsi:.1f} > {rsi_over}")
        elif rsi < rsi_under:
            alerts.append(f"RSI oversold {rsi:.1f} < {rsi_under}")

    macd_line, signal_line = compute_macd(prices)
    if macd_line and signal_line:
        macd_val = macd_line[-1]
        sig_val = signal_line[-1]
        diff = macd_val - sig_val
        if diff > macd_gap:
            alerts.append(f"MACD above signal by {diff:.4f}")
        elif diff < -macd_gap:
            alerts.append(f"MACD below signal by {abs(diff):.4f}")

    return alerts

def _should_alert(ticker, alert_type):
    cache_file = CACHE_DIR / f"{ticker}_{alert_type}.last"
    now = datetime.utcnow()
    if cache_file.exists():
        try:
            ts = datetime.fromisoformat(cache_file.read_text().strip())
            if now - ts < timedelta(minutes=ALERT_COOLDOWN_MINUTES):
                return False
        except ValueError:
            pass
    return True

def _record_alert(ticker, alert_type):
    cache_file = CACHE_DIR / f"{ticker}_{alert_type}.last"
    cache_file.write_text(datetime.utcnow().isoformat())

def _print_alert(ticker, alerts):
    now = datetime.utcnow().strftime("%H:%M:%S")
    for a in alerts:
        print(f"[{now}] {ticker}: {a}")
        sys.stdout.flush()

def cmd_add(args):
    entries = _load_watchlist()
    ticker = args.ticker.upper()
    if any(e["ticker"] == ticker for e in entries):
        print(f"{ticker} already on watchlist")
        return 0
    entry = {"ticker": ticker}
    if args.rsi_overbought is not None:
        entry["rsi_overbought"] = args.rsi_overbought
    if args.rsi_oversold is not None:
        entry["rsi_oversold"] = args.rsi_oversold
    if args.macd_gap is not None:
        entry["macd_signal_gap"] = args.macd_gap
    entries.append(entry)
    _save_watchlist(entries)
    print(f"added {ticker}")
    return 0

def cmd_remove(args):
    entries = _load_watchlist()
    ticker = args.ticker.upper()
    new_entries = [e for e in entries if e["ticker"] != ticker]
    if len(new_entries) == len(entries):
        print(f"{ticker} not found")
        return 1
    _save_watchlist(new_entries)
    print(f"removed {ticker}")
    return 0

def cmd_list(args):
    entries = _load_watchlist()
    if not entries:
        print("no entries, add one with --add-ticker SYM")
        return 0
    for e in entries:
        line = e["ticker"]
        if "rsi_overbought" in e:
            line += f" rsi_over={e['rsi_overbought']}"
        if "rsi_oversold" in e:
            line += f" rsi_under={e['rsi_oversold']}"
        if "macd_signal_gap" in e:
            line += f" macd_gap={e['macd_signal_gap']}"
        print(line)
    return 0

def cmd_run(args):
    entries = _load_watchlist()
    if not entries:
        print("no entries, add one with --add-ticker SYM")
        return 0

    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))

    while True:
        for entry in entries:
            ticker = entry["ticker"]
            try:
                timestamps, prices = _fetch_yahoo(ticker)
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                print(f"{ticker}: fetch failed ({exc})")
                continue

            alerts = _check_alerts(entry, timestamps, prices)
            for a in alerts:
                alert_type = a.split()[0].lower()
                if _should_alert(ticker, alert_type):
                    _print_alert(ticker, [a])
                    _record_alert(ticker, alert_type)

        time.sleep(POLL_INTERVAL)

def main():
    parser = argparse.ArgumentParser(
        prog="watch",
        usage="python watch.py --add-ticker AAPL [--rsi-overbought 75]",
        description="minimal stock watcher with rsi/macd alerts"
    )
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="add ticker to watchlist")
    p_add.add_argument("ticker")
    p_add.add_argument("--rsi-overbought", type=float)
    p_add.add_argument("--rsi-oversold", type=float)
    p_add.add_argument("--macd-gap", type=float)

    p_rm = sub.add_parser("remove", help="remove ticker from watchlist")
    p_rm.add_argument("ticker")

    sub.add_parser("list", help="show watchlist")
    sub.add_parser("run", help="start polling loop")

    args = parser.parse_args()

    if args.command == "add":
        return cmd_add(args)
    elif args.command == "remove":
        return cmd_remove(args)
    elif args.command == "list":
        return cmd_list(args)
    elif args.command == "run":
        return cmd_run(args)
    else:
        parser.print_usage()
        return 2

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
