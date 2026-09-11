import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import httpx
except ImportError as _exc:
    sys.exit(f"missing dependency '{_exc.name}'. run: pip install -r requirements.txt")

DB_PATH = Path(__file__).with_suffix(".db")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# reused across calls in the same process to avoid sqlite locking noise
_db_conn: Optional[sqlite3.Connection] = None

def _db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH, timeout=5)
    return _db_conn

def _init_db() -> None:
    conn = _db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            ts TEXT NOT NULL,
            close REAL NOT NULL,
            PRIMARY KEY (ticker, ts)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            ticker TEXT NOT NULL,
            kind TEXT NOT NULL,
            triggered_ts TEXT NOT NULL,
            PRIMARY KEY (ticker, kind)
        )
        """
    )
    conn.commit()

def _crumb_and_cookies(client: httpx.Client) -> Tuple[str, str]:
    r = client.get("https://finance.yahoo.com/quote/AAPL/history", headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    body = r.text
    crumb_match = re.search(r'"crumb":"([^"]+)"', body)
    if not crumb_match:
        raise RuntimeError("could not extract crumb from yahoo")
    crumb = crumb_match.group(1)
    cookies = client.cookies.jar._cookies  # type: ignore
    # Build a simple cookie string; yahoo only needs the session cookie really
    cookie_header = "; ".join(f"{k}={v.value}" for domain in cookies.values() for path in domain.values() for k, v in path.items())
    return crumb, cookie_header

def fetch_history(ticker: str, days: int = 90) -> List[Dict[str, float]]:
    """Pull daily closes from Yahoo Finance. Returns list of {date, close}."""
    _init_db()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 30)  # buffer for weekends/holidays
    period1 = int(start.timestamp())
    period2 = int(end.timestamp())

    with httpx.Client(http2=True, timeout=30.0, follow_redirects=True) as client:
        crumb, cookie_header = _crumb_and_cookies(client)
        url = (
            f"https://query1.finance.yahoo.com/v7/finance/download/{ticker}"
            f"?period1={period1}&period2={period2}&interval=1d&events=history&crumb={crumb}"
        )
        headers = {"User-Agent": USER_AGENT, "Cookie": cookie_header}
        r = client.get(url, headers=headers)
        if r.status_code == 401:
            # crumb probably expired, try once more after refresh
            crumb, cookie_header = _crumb_and_cookies(client)
            url = (
                f"https://query1.finance.yahoo.com/v7/finance/download/{ticker}"
                f"?period1={period1}&period2={period2}&interval=1d&events=history&crumb={crumb}"
            )
            r = client.get(url, headers={"User-Agent": USER_AGENT, "Cookie": cookie_header})
        # print(f"yahoo response {r.status_code}: {r.text[:200]}")  # debug
        r.raise_for_status()
        lines = r.text.strip().splitlines()
    
    if len(lines) < 2:
        return []
    
    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        date_str, _open, _high, _low, close_str = parts[:5]
        try:
            close = float(close_str)
        except ValueError:
            continue
        rows.append({"date": date_str, "close": close})
    return rows

def _save_prices(ticker: str, rows: List[Dict[str, float]]) -> None:
    conn = _db()
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO prices (ticker, ts, close) VALUES (?, ?, ?)",
            (ticker, row["date"], row["close"]),
        )
    conn.commit()

def load_prices(ticker: str, days: int = 90) -> List[Dict[str, float]]:
    """Load from DB, backfill from Yahoo if needed."""
    _init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    cur = _db().execute(
        "SELECT ts, close FROM prices WHERE ticker = ? AND ts >= ? ORDER BY ts",
        (ticker, cutoff),
    )
    rows = [{"date": ts, "close": close} for ts, close in cur.fetchall()]
    if len(rows) < days // 3:
        # stale or empty, fetch fresh
        fetched = fetch_history(ticker, days)
        if fetched:
            _save_prices(ticker, fetched)
            rows = fetched[-days:] if len(fetched) > days else fetched
    return rows

def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    # Wilder's smoothing
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def _ema(data: List[float], span: int) -> List[float]:
    mult = 2.0 / (span + 1)
    result = [data[0]]
    for val in data[1:]:
        result.append(result[-1] + mult * (val - result[-1]))
    return result

def macd_series(prices: List[float]) -> Tuple[List[float], List[float], List[float]]:
    """Returns (macd_line, signal_line, histogram) as full lists."""
    if len(prices) < 26:
        return [], [], []
    ema12 = _ema(prices, 12)
    ema26 = _ema(prices, 26)
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[i] - ema26[i] for i in range(min_len)]
    signal = _ema(macd_line, 9)
    histogram = [m - s for m, s in zip(macd_line, signal)]
    return macd_line, signal, histogram

def macd(prices: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (macd_line, signal_line, histogram) for the latest point."""
    m, s, h = macd_series(prices)
    if not m:
        return None, None, None
    return m[-1], s[-1], h[-1]

def check_alert(ticker: str, kind: str) -> bool:
    """Return True if we already fired this alert today."""
    _init_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur = _db().execute(
        "SELECT 1 FROM alerts WHERE ticker = ? AND kind = ? AND triggered_ts >= ?",
        (ticker, kind, today),
    )
    return cur.fetchone() is not None

def mark_alert(ticker: str, kind: str) -> None:
    _init_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _db().execute(
        "INSERT OR REPLACE INTO alerts (ticker, kind, triggered_ts) VALUES (?, ?, ?)",
        (ticker, kind, now),
    )
    _db().commit()

def macd_crossover_state(macd_vals: List[float], signal_vals: List[float]) -> Optional[str]:
    """Given aligned macd and signal series, return 'bullish' or 'bearish' if a crossover just happened."""
    if len(macd_vals) < 2 or len(signal_vals) < 2:
        return None
    prev_diff = macd_vals[-2] - signal_vals[-2]
    curr_diff = macd_vals[-1] - signal_vals[-1]
    if prev_diff < 0 and curr_diff > 0:
        return "bullish"
    if prev_diff > 0 and curr_diff < 0:
        return "bearish"
    return None

def evaluate(ticker: str, rsi_overbought: float = 70.0, rsi_oversold: float = 30.0) -> Dict[str, Optional[float]]:
    """Fetch prices, compute indicators, return dict with rsi, macd, signal, hist."""
    rows = load_prices(ticker)
    if not rows:
        return {"rsi": None, "macd": None, "signal": None, "hist": None, "last_close": None}
    closes = [r["close"] for r in rows]
    last_close = closes[-1]
    rsi_val = rsi(closes)
    macd_val, signal_val, hist_val = macd(closes)
    return {
        "rsi": rsi_val,
        "macd": macd_val,
        "signal": signal_val,
        "hist": hist_val,
        "last_close": last_close,
    }
