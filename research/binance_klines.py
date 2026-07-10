from __future__ import annotations

import argparse
import io
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "research" / "binance"
BINANCE_SPOT_BASE_URL = "https://api.binance.com"
BINANCE_PUBLIC_DATA_BASE_URL = "https://data.binance.vision"

KLINE_COLUMNS = [
    "open_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time_ms",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]

INTERVAL_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
}


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if len(text) == 10:
        text = f"{text}T00:00:00+00:00"
    elif text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def default_output_path(symbol: str, interval: str, start: str, end: str) -> Path:
    safe_start = start[:10]
    safe_end = end[:10] if len(end) >= 10 else end
    return DEFAULT_OUTPUT_DIR / f"{symbol}_{interval}_{safe_start}_{safe_end}.csv"


def request_klines(
    session: requests.Session,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    timeout: float,
    base_url: str,
) -> list[list[Any]]:
    response = session.get(
        f"{base_url.rstrip('/')}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms - 1,
            "limit": 1000,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Binance response: {str(payload)[:160]}")
    return payload


def fetch_klines(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    sleep_seconds: float,
    timeout: float,
    max_rows: int,
    base_url: str,
) -> pd.DataFrame:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval {interval!r}; supported: {sorted(INTERVAL_MS)}")
    if end <= start:
        raise ValueError("--end must be after --start")

    interval_ms = INTERVAL_MS[interval]
    cursor_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows: list[list[Any]] = []
    session = requests.Session()

    while cursor_ms < end_ms:
        batch = request_klines(
            session=session,
            symbol=symbol,
            interval=interval,
            start_ms=cursor_ms,
            end_ms=end_ms,
            timeout=timeout,
            base_url=base_url,
        )
        if not batch:
            break
        rows.extend(item for item in batch if int(item[0]) < end_ms)
        last_open_ms = int(batch[-1][0])
        next_cursor_ms = last_open_ms + interval_ms
        if next_cursor_ms <= cursor_ms:
            break
        cursor_ms = next_cursor_ms
        if max_rows and len(rows) >= max_rows:
            rows = rows[:max_rows]
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    df = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    if df.empty:
        return df
    numeric_columns = [column for column in KLINE_COLUMNS if column != "ignore"]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.drop(columns=["ignore"], errors="ignore")
    df = df.drop_duplicates("open_time_ms").sort_values("open_time_ms")
    return df


def iter_utc_days(start: datetime, end: datetime) -> list[datetime]:
    first = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    days: list[datetime] = []
    cursor = first
    while cursor < end:
        days.append(cursor)
        cursor = cursor + pd.Timedelta(days=1).to_pytimedelta()
    return days


def public_data_daily_url(symbol: str, interval: str, day: datetime, base_url: str) -> str:
    date_text = day.strftime("%Y-%m-%d")
    return (
        f"{base_url.rstrip('/')}/data/spot/daily/klines/"
        f"{symbol}/{interval}/{symbol}-{interval}-{date_text}.zip"
    )


def read_public_data_zip(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise RuntimeError("Binance public-data zip did not contain a CSV file.")
        with archive.open(names[0]) as handle:
            df = pd.read_csv(handle, header=None, names=KLINE_COLUMNS)
    return df


def fetch_public_data_klines(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    sleep_seconds: float,
    timeout: float,
    max_rows: int,
    base_url: str,
) -> pd.DataFrame:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    session = requests.Session()
    frames: list[pd.DataFrame] = []
    days = iter_utc_days(start, end)

    for index, day in enumerate(days, start=1):
        url = public_data_daily_url(symbol=symbol, interval=interval, day=day, base_url=base_url)
        response = session.get(url, timeout=timeout)
        if response.status_code == 404:
            print(f"[binance] missing public-data day {day.date()} ({url})")
            continue
        response.raise_for_status()
        day_df = read_public_data_zip(response.content)
        frames.append(day_df)
        if max_rows and sum(len(frame) for frame in frames) >= max_rows:
            break
        if sleep_seconds > 0 and index < len(days):
            time.sleep(sleep_seconds)

    if not frames:
        return pd.DataFrame(columns=[column for column in KLINE_COLUMNS if column != "ignore"])
    df = pd.concat(frames, ignore_index=True)
    numeric_columns = [column for column in KLINE_COLUMNS if column != "ignore"]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.drop(columns=["ignore"], errors="ignore")
    df = df.dropna(subset=["open_time_ms"])
    df = df[(df["open_time_ms"] >= start_ms) & (df["open_time_ms"] < end_ms)].copy()
    if max_rows and len(df) > max_rows:
        df = df.head(max_rows).copy()
    return df.drop_duplicates("open_time_ms").sort_values("open_time_ms")


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Binance spot klines into data/research cache.")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1s", choices=sorted(INTERVAL_MS))
    parser.add_argument("--start", required=True, help="UTC start, e.g. 2024-01-01 or 2024-01-01T00:00:00Z.")
    parser.add_argument("--end", required=True, help="UTC end, exclusive.")
    parser.add_argument("--output", default="", help="CSV output path. Defaults under data/research/binance/.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Pause between API requests.")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--max-rows", type=int, default=0, help="Optional cap for smoke tests.")
    parser.add_argument("--base-url", default=BINANCE_SPOT_BASE_URL)
    parser.add_argument(
        "--source",
        choices=["api", "public-data"],
        default="api",
        help="REST API or Binance public data daily zip files.",
    )
    args = parser.parse_args()

    start = parse_utc(args.start)
    end = parse_utc(args.end)
    output_path = Path(args.output) if args.output else default_output_path(args.symbol, args.interval, args.start, args.end)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.source == "public-data":
        base_url = args.base_url if args.base_url != BINANCE_SPOT_BASE_URL else BINANCE_PUBLIC_DATA_BASE_URL
        df = fetch_public_data_klines(
            symbol=args.symbol,
            interval=args.interval,
            start=start,
            end=end,
            sleep_seconds=max(args.sleep, 0.0),
            timeout=max(args.timeout, 1.0),
            max_rows=max(args.max_rows, 0),
            base_url=base_url,
        )
    else:
        df = fetch_klines(
            symbol=args.symbol,
            interval=args.interval,
            start=start,
            end=end,
            sleep_seconds=max(args.sleep, 0.0),
            timeout=max(args.timeout, 1.0),
            max_rows=max(args.max_rows, 0),
            base_url=args.base_url,
        )
    df.to_csv(output_path, index=False)

    if df.empty:
        print(f"[binance] no rows -> {output_path}")
        return 1
    start_ts = pd.to_datetime(df["open_time_ms"].iloc[0], unit="ms", utc=True)
    end_ts = pd.to_datetime(df["open_time_ms"].iloc[-1], unit="ms", utc=True)
    print(f"[binance] rows={len(df)} interval={args.interval} symbol={args.symbol}")
    print(f"[binance] range={start_ts} -> {end_ts}")
    print(f"[binance] wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
