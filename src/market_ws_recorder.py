from __future__ import annotations

import asyncio
import csv
import datetime as dt
import gzip
import json
import math
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

HTTP_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
}

SUMMARY_COLUMNS = [
    "receive_ts_utc",
    "source_ts_ms",
    "receive_lag_ms",
    "slug",
    "window_start_ts",
    "elapsed_s",
    "event_type",
    "market",
    "asset_id",
    "outcome",
    "side",
    "price",
    "size",
    "hash",
    "yes_best_bid",
    "yes_best_bid_size",
    "yes_best_ask",
    "yes_best_ask_size",
    "yes_spread",
    "yes_mid",
    "yes_bid_depth_40_60",
    "yes_ask_depth_40_60",
    "yes_top_imbalance",
    "yes_depth_imbalance_40_60",
    "no_best_bid",
    "no_best_bid_size",
    "no_best_ask",
    "no_best_ask_size",
    "no_spread",
    "no_mid",
    "no_bid_depth_40_60",
    "no_ask_depth_40_60",
    "no_top_imbalance",
    "no_depth_imbalance_40_60",
    "directional_top_imbalance",
    "directional_depth_imbalance_40_60",
    "maker_pair_bid_sum",
    "maker_pair_edge",
    "taker_pair_ask_sum",
    "taker_pair_edge",
    "mid_pair_sum",
]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(timestamp: dt.datetime | None = None) -> str:
    value = timestamp or utc_now()
    return value.astimezone(dt.timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def updown_slug(prefix: str, window_seconds: int, timestamp: float | None = None) -> str:
    now_ts = time.time() if timestamp is None else float(timestamp)
    window = max(int(window_seconds), 1)
    window_start = int(now_ts // window) * window
    return f"{prefix}-{window_start}"


def btc_5m_slug(timestamp: float | None = None) -> str:
    return updown_slug("btc-updown-5m", 300, timestamp)


def slug_window_start(slug: str) -> int:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class MarketTokens:
    slug: str
    window_start_ts: int
    market_id: str
    yes_token_id: str
    no_token_id: str
    start_time: dt.datetime | None = None

    @property
    def token_ids(self) -> list[str]:
        return [self.yes_token_id, self.no_token_id]

    def outcome_for(self, asset_id: str) -> str:
        if str(asset_id) == self.yes_token_id:
            return "Yes"
        if str(asset_id) == self.no_token_id:
            return "No"
        return ""


def _market_tokens_from_event(slug: str, event: dict[str, Any]) -> MarketTokens | None:
    start_str = event.get("startTime") or event.get("startDate") or ""
    start_time = None
    if start_str:
        try:
            start_time = dt.datetime.fromisoformat(str(start_str).replace("Z", "+00:00"))
        except ValueError:
            start_time = None

    for market in event.get("markets") or []:
        token_ids = [str(item) for item in parse_jsonish_list(market.get("clobTokenIds"))]
        if len(token_ids) != 2:
            continue
        outcomes = [str(item).lower() for item in parse_jsonish_list(market.get("outcomes"))]
        yes_index = outcomes.index("yes") if "yes" in outcomes else 0
        no_index = outcomes.index("no") if "no" in outcomes else 1
        if yes_index >= len(token_ids) or no_index >= len(token_ids):
            continue
        return MarketTokens(
            slug=slug,
            window_start_ts=slug_window_start(slug),
            market_id=str(market.get("conditionId") or market.get("id") or ""),
            yes_token_id=token_ids[yes_index],
            no_token_id=token_ids[no_index],
            start_time=start_time,
        )
    return None


async def fetch_market_tokens(slug: str | None = None) -> MarketTokens:
    import aiohttp

    resolved_slug = slug or btc_5m_slug()
    url = f"{GAMMA_EVENTS_URL}?slug={resolved_slug}"
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout, headers=HTTP_HEADERS) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"No Gamma event found for slug={resolved_slug}")
    tokens = _market_tokens_from_event(resolved_slug, data[0])
    if tokens is None:
        raise RuntimeError(f"Gamma event for slug={resolved_slug} did not include two CLOB token ids")
    return tokens


class OrderBookState:
    def __init__(self) -> None:
        self._books: dict[str, dict[str, dict[float, float]]] = {}
        self._best_overrides: dict[str, dict[str, float]] = {}

    def ensure_asset(self, asset_id: str) -> dict[str, dict[float, float]]:
        return self._books.setdefault(str(asset_id), {"bids": {}, "asks": {}})

    def apply_book(self, event: dict[str, Any]) -> None:
        asset_id = str(event.get("asset_id") or "")
        if not asset_id:
            return
        book = self.ensure_asset(asset_id)
        book["bids"] = self._levels_to_map(event.get("bids") or [])
        book["asks"] = self._levels_to_map(event.get("asks") or [])
        self._set_best_override(asset_id, book)

    def apply_price_change(self, change: dict[str, Any]) -> None:
        asset_id = str(change.get("asset_id") or "")
        if not asset_id:
            return
        price = safe_float(change.get("price"), 0.0)
        size = safe_float(change.get("size"), 0.0)
        if price <= 0:
            return
        side = str(change.get("side") or "").upper()
        if side == "BUY":
            side_key = "bids"
        elif side == "SELL":
            side_key = "asks"
        else:
            return
        book = self.ensure_asset(asset_id)
        if size <= 0:
            book[side_key].pop(price, None)
        else:
            book[side_key][price] = size
        override = self._best_overrides.setdefault(asset_id, {})
        best_bid = safe_float(change.get("best_bid"), 0.0)
        best_ask = safe_float(change.get("best_ask"), 0.0)
        if best_bid > 0:
            override["best_bid"] = best_bid
        if best_ask > 0:
            override["best_ask"] = best_ask

    def apply_best_bid_ask(self, event: dict[str, Any]) -> None:
        asset_id = str(event.get("asset_id") or "")
        if not asset_id:
            return
        override = self._best_overrides.setdefault(asset_id, {})
        best_bid = safe_float(event.get("best_bid"), 0.0)
        best_ask = safe_float(event.get("best_ask"), 0.0)
        if best_bid > 0:
            override["best_bid"] = best_bid
        if best_ask > 0:
            override["best_ask"] = best_ask

    def metrics(self, asset_id: str) -> dict[str, float | str]:
        asset = str(asset_id)
        book = self.ensure_asset(asset)
        bids = sorted(book["bids"].items(), key=lambda item: item[0], reverse=True)
        asks = sorted(book["asks"].items(), key=lambda item: item[0])
        override = self._best_overrides.get(asset, {})
        best_bid, best_bid_size = bids[0] if bids else (safe_float(override.get("best_bid"), 0.0), 0.0)
        best_ask, best_ask_size = asks[0] if asks else (safe_float(override.get("best_ask"), 0.0), 0.0)
        spread = best_ask - best_bid if best_bid > 0 and best_ask > 0 else 0.0
        mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
        bid_depth, _ = self._depth(bids, 0.40, 0.60)
        ask_depth, _ = self._depth(asks, 0.40, 0.60)
        top_sum = best_bid_size + best_ask_size
        depth_sum = bid_depth + ask_depth
        return {
            "best_bid": round(best_bid, 6),
            "best_bid_size": round(best_bid_size, 6),
            "best_ask": round(best_ask, 6),
            "best_ask_size": round(best_ask_size, 6),
            "spread": round(spread, 6) if spread > 0 else "",
            "mid": round(mid, 6) if mid > 0 else "",
            "bid_depth_40_60": round(bid_depth, 6),
            "ask_depth_40_60": round(ask_depth, 6),
            "top_imbalance": round((best_bid_size - best_ask_size) / top_sum, 6) if top_sum > 0 else "",
            "depth_imbalance_40_60": round((bid_depth - ask_depth) / depth_sum, 6) if depth_sum > 0 else "",
        }

    def normalized_rows(
        self,
        event: dict[str, Any],
        tokens: MarketTokens,
        receive_time: dt.datetime | None = None,
    ) -> list[dict[str, Any]]:
        receive_dt = receive_time or utc_now()
        event_type = str(event.get("event_type") or "")
        if event_type == "book":
            self.apply_book(event)
            return [self._summary_row(event, event, tokens, receive_dt)]
        if event_type == "price_change":
            rows = []
            for change in event.get("price_changes") or []:
                if not isinstance(change, dict):
                    continue
                self.apply_price_change(change)
                rows.append(self._summary_row(event, change, tokens, receive_dt))
            return rows
        if event_type == "best_bid_ask":
            self.apply_best_bid_ask(event)
            return [self._summary_row(event, event, tokens, receive_dt)]
        if event_type == "last_trade_price":
            return [self._summary_row(event, event, tokens, receive_dt)]
        if event_type in {"tick_size_change", "new_market", "market_resolved"}:
            return [self._summary_row(event, event, tokens, receive_dt)]
        return []

    @staticmethod
    def _levels_to_map(levels: Iterable[Any]) -> dict[float, float]:
        result: dict[float, float] = {}
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = safe_float(level.get("price"), 0.0)
            size = safe_float(level.get("size"), 0.0)
            if price > 0 and size > 0:
                result[price] = size
        return result

    @staticmethod
    def _depth(levels: list[tuple[float, float]], lower: float, upper: float) -> tuple[float, float]:
        selected = [(price, size) for price, size in levels if lower <= price <= upper]
        return (
            sum(size for _, size in selected),
            sum(price * size for price, size in selected),
        )

    def _set_best_override(self, asset_id: str, book: dict[str, dict[float, float]]) -> None:
        bids = sorted(book["bids"].items(), key=lambda item: item[0], reverse=True)
        asks = sorted(book["asks"].items(), key=lambda item: item[0])
        override = self._best_overrides.setdefault(str(asset_id), {})
        if bids:
            override["best_bid"] = bids[0][0]
        if asks:
            override["best_ask"] = asks[0][0]

    def _summary_row(
        self,
        event: dict[str, Any],
        update: dict[str, Any],
        tokens: MarketTokens,
        receive_dt: dt.datetime,
    ) -> dict[str, Any]:
        source_ts = safe_int(event.get("timestamp") or update.get("timestamp"), 0)
        source_seconds = source_ts / 1000.0 if source_ts > 10_000_000_000 else float(source_ts or 0)
        lag_ms = ""
        if source_seconds > 0:
            lag_ms = round((receive_dt.timestamp() - source_seconds) * 1000.0, 3)
        asset_id = str(update.get("asset_id") or event.get("asset_id") or "")
        yes = self.metrics(tokens.yes_token_id)
        no = self.metrics(tokens.no_token_id)
        yes_bid = safe_float(yes.get("best_bid"), 0.0)
        yes_ask = safe_float(yes.get("best_ask"), 0.0)
        no_bid = safe_float(no.get("best_bid"), 0.0)
        no_ask = safe_float(no.get("best_ask"), 0.0)
        yes_mid = safe_float(yes.get("mid"), 0.0)
        no_mid = safe_float(no.get("mid"), 0.0)
        maker_pair_bid_sum = yes_bid + no_bid if yes_bid > 0 and no_bid > 0 else 0.0
        taker_pair_ask_sum = yes_ask + no_ask if yes_ask > 0 and no_ask > 0 else 0.0
        mid_pair_sum = yes_mid + no_mid if yes_mid > 0 and no_mid > 0 else 0.0
        receive_ts = receive_dt.timestamp()
        elapsed = receive_ts - float(tokens.window_start_ts) if tokens.window_start_ts else 0.0
        yes_top_imbalance = safe_float(yes.get("top_imbalance"), math.nan)
        no_top_imbalance = safe_float(no.get("top_imbalance"), math.nan)
        yes_depth_imbalance = safe_float(yes.get("depth_imbalance_40_60"), math.nan)
        no_depth_imbalance = safe_float(no.get("depth_imbalance_40_60"), math.nan)
        return {
            "receive_ts_utc": receive_dt.isoformat(),
            "source_ts_ms": source_ts or "",
            "receive_lag_ms": lag_ms,
            "slug": tokens.slug,
            "window_start_ts": tokens.window_start_ts or "",
            "elapsed_s": round(max(elapsed, 0.0), 3) if elapsed else "",
            "event_type": event.get("event_type", ""),
            "market": event.get("market", ""),
            "asset_id": asset_id,
            "outcome": tokens.outcome_for(asset_id),
            "side": update.get("side", ""),
            "price": update.get("price", ""),
            "size": update.get("size", ""),
            "hash": update.get("hash") or event.get("hash") or "",
            "yes_best_bid": yes["best_bid"],
            "yes_best_bid_size": yes["best_bid_size"],
            "yes_best_ask": yes["best_ask"],
            "yes_best_ask_size": yes["best_ask_size"],
            "yes_spread": yes["spread"],
            "yes_mid": yes["mid"],
            "yes_bid_depth_40_60": yes["bid_depth_40_60"],
            "yes_ask_depth_40_60": yes["ask_depth_40_60"],
            "yes_top_imbalance": yes["top_imbalance"],
            "yes_depth_imbalance_40_60": yes["depth_imbalance_40_60"],
            "no_best_bid": no["best_bid"],
            "no_best_bid_size": no["best_bid_size"],
            "no_best_ask": no["best_ask"],
            "no_best_ask_size": no["best_ask_size"],
            "no_spread": no["spread"],
            "no_mid": no["mid"],
            "no_bid_depth_40_60": no["bid_depth_40_60"],
            "no_ask_depth_40_60": no["ask_depth_40_60"],
            "no_top_imbalance": no["top_imbalance"],
            "no_depth_imbalance_40_60": no["depth_imbalance_40_60"],
            "directional_top_imbalance": (
                round(yes_top_imbalance - no_top_imbalance, 6)
                if math.isfinite(yes_top_imbalance) and math.isfinite(no_top_imbalance)
                else ""
            ),
            "directional_depth_imbalance_40_60": (
                round(yes_depth_imbalance - no_depth_imbalance, 6)
                if math.isfinite(yes_depth_imbalance) and math.isfinite(no_depth_imbalance)
                else ""
            ),
            "maker_pair_bid_sum": round(maker_pair_bid_sum, 6) if maker_pair_bid_sum > 0 else "",
            "maker_pair_edge": round(1.0 - maker_pair_bid_sum, 6) if maker_pair_bid_sum > 0 else "",
            "taker_pair_ask_sum": round(taker_pair_ask_sum, 6) if taker_pair_ask_sum > 0 else "",
            "taker_pair_edge": round(1.0 - taker_pair_ask_sum, 6) if taker_pair_ask_sum > 0 else "",
            "mid_pair_sum": round(mid_pair_sum, 6) if mid_pair_sum > 0 else "",
        }


class CsvWriter:
    def __init__(self, path: Path, columns: list[str]) -> None:
        self.path = path
        self.columns = columns
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._header_written = self.path.exists() and self.path.stat().st_size > 0

    def write_rows(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        opener = gzip.open if self.path.suffix == ".gz" else open
        with opener(self.path, "at", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.columns, extrasaction="ignore")
            if not self._header_written:
                writer.writeheader()
                self._header_written = True
            for row in rows:
                writer.writerow(row)
        return len(rows)


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n")


class SQLiteMarketWriter:
    def __init__(self, path: Path, tokens: MarketTokens, store_raw_json: bool = False) -> None:
        self.path = path
        self.tokens = tokens
        self.store_raw_json = bool(store_raw_json)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._pending = 0
        self._setup()

    def close(self) -> None:
        self.flush()
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()

    def flush(self) -> None:
        if self._pending:
            self.conn.commit()
            self._pending = 0

    def write_event(self, event: dict[str, Any], receive_time: dt.datetime) -> int:
        event_type = str(event.get("event_type") or "")
        if event_type == "book":
            event_id = self._insert_event(event, event, receive_time)
            self._insert_book_levels(event_id, str(event.get("asset_id") or ""), "bid", event.get("bids") or [])
            self._insert_book_levels(event_id, str(event.get("asset_id") or ""), "ask", event.get("asks") or [])
            self._maybe_commit()
            return 1
        if event_type == "price_change":
            count = 0
            for change in event.get("price_changes") or []:
                if not isinstance(change, dict):
                    continue
                self._insert_event(event, change, receive_time)
                count += 1
            self._maybe_commit()
            return count
        self._insert_event(event, event, receive_time)
        self._maybe_commit()
        return 1

    def _setup(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receive_ts_utc TEXT NOT NULL,
                receive_ts_ms INTEGER NOT NULL,
                source_ts_ms INTEGER,
                slug TEXT NOT NULL,
                window_start_ts INTEGER,
                event_type TEXT NOT NULL,
                market TEXT,
                asset_id TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                best_bid REAL,
                best_ask REAL,
                hash TEXT,
                raw_json TEXT
            );
            CREATE TABLE IF NOT EXISTS book_levels (
                event_id INTEGER NOT NULL,
                asset_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_slug_ts ON events(slug, receive_ts_ms);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_levels_event ON book_levels(event_id);
            """
        )
        metadata = {
            "schema_version": "1",
            "slug": self.tokens.slug,
            "window_start_ts": str(self.tokens.window_start_ts),
            "market_id": self.tokens.market_id,
            "yes_token_id": self.tokens.yes_token_id,
            "no_token_id": self.tokens.no_token_id,
            "created_at_utc": iso_utc(),
        }
        self.conn.executemany(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        self.conn.commit()

    def _insert_event(self, event: dict[str, Any], update: dict[str, Any], receive_time: dt.datetime) -> int:
        source_ts = safe_int(event.get("timestamp") or update.get("timestamp"), 0)
        asset_id = str(update.get("asset_id") or event.get("asset_id") or "")
        price = safe_float(update.get("price"), math.nan)
        size = safe_float(update.get("size"), math.nan)
        best_bid = safe_float(update.get("best_bid"), math.nan)
        best_ask = safe_float(update.get("best_ask"), math.nan)
        raw_json = json.dumps(event, separators=(",", ":"), ensure_ascii=True) if self.store_raw_json else None
        cursor = self.conn.execute(
            """
            INSERT INTO events (
                receive_ts_utc, receive_ts_ms, source_ts_ms, slug, window_start_ts,
                event_type, market, asset_id, outcome, side, price, size,
                best_bid, best_ask, hash, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receive_time.isoformat(),
                int(receive_time.timestamp() * 1000),
                source_ts or None,
                self.tokens.slug,
                self.tokens.window_start_ts or None,
                str(event.get("event_type") or ""),
                str(event.get("market") or ""),
                asset_id,
                self.tokens.outcome_for(asset_id),
                str(update.get("side") or ""),
                price if math.isfinite(price) else None,
                size if math.isfinite(size) else None,
                best_bid if math.isfinite(best_bid) else None,
                best_ask if math.isfinite(best_ask) else None,
                str(update.get("hash") or event.get("hash") or ""),
                raw_json,
            ),
        )
        self._pending += 1
        return int(cursor.lastrowid)

    def _insert_book_levels(self, event_id: int, asset_id: str, side: str, levels: Iterable[Any]) -> None:
        rows = []
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = safe_float(level.get("price"), 0.0)
            size = safe_float(level.get("size"), 0.0)
            if price > 0 and size > 0:
                rows.append((event_id, asset_id, side, price, size))
        if rows:
            self.conn.executemany(
                "INSERT INTO book_levels(event_id, asset_id, side, price, size) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._pending += len(rows)

    def _maybe_commit(self) -> None:
        if self._pending >= 2000:
            self.flush()


@dataclass(frozen=True)
class RecorderConfig:
    tokens: MarketTokens
    output_dir: Path
    duration_seconds: float = 300.0
    raw_jsonl: Path | None = None
    summary_csv: Path | None = None
    write_raw: bool = True
    gzip_summary: bool = False
    summary_interval_ms: float = 0.0
    storage: str = "csv"
    sqlite_path: Path | None = None
    sqlite_store_raw_json: bool = False
    gzip_sqlite: bool = False


class MarketWsRecorder:
    def __init__(self, config: RecorderConfig) -> None:
        self.config = config
        self.state = OrderBookState()
        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        base = f"{config.tokens.slug}_{timestamp}"
        self.raw_path = config.raw_jsonl or config.output_dir / f"{base}.raw.jsonl"
        summary_suffix = ".summary.csv.gz" if config.gzip_summary else ".summary.csv"
        self.summary_path = config.summary_csv or config.output_dir / f"{base}{summary_suffix}"
        self.sqlite_path = config.sqlite_path or config.output_dir / f"{base}.market.sqlite"
        storage = str(config.storage or "csv").lower()
        if storage not in {"csv", "sqlite", "both"}:
            raise ValueError("Recorder storage must be csv, sqlite, or both")
        self.storage = storage
        self.raw_writer = JsonlWriter(self.raw_path) if config.write_raw else None
        self.csv_writer = CsvWriter(self.summary_path, SUMMARY_COLUMNS) if storage in {"csv", "both"} else None
        self.sqlite_writer = (
            SQLiteMarketWriter(self.sqlite_path, config.tokens, store_raw_json=config.sqlite_store_raw_json)
            if storage in {"sqlite", "both"}
            else None
        )
        self.sqlite_output_path = self.sqlite_path
        self.raw_messages = 0
        self.summary_rows = 0
        self.sqlite_rows = 0
        self._last_summary_write_ts = 0.0

    async def run(self) -> dict[str, Any]:
        import websockets

        subscription = json.dumps({
            "assets_ids": self.config.tokens.token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        })
        deadline = time.time() + max(float(self.config.duration_seconds), 1.0)
        try:
            async with websockets.connect(MARKET_WS_URL, ping_interval=None, close_timeout=2) as websocket:
                await websocket.send(subscription)
                while time.time() < deadline:
                    timeout = max(min(deadline - time.time(), 5.0), 0.1)
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        await websocket.send("PING")
                        continue
                    self._handle_message(message)
        finally:
            if self.sqlite_writer is not None:
                self.sqlite_writer.close()
                if self.config.gzip_sqlite:
                    self.sqlite_output_path = gzip_file(self.sqlite_path, remove_source=True)
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(self.sqlite_path) + suffix)
                        try:
                            sidecar.unlink()
                        except FileNotFoundError:
                            pass
        return {
            "slug": self.config.tokens.slug,
            "token_ids": self.config.tokens.token_ids,
            "storage": self.storage,
            "raw_messages": self.raw_messages,
            "summary_rows": self.summary_rows,
            "sqlite_rows": self.sqlite_rows,
            "raw_path": str(self.raw_path) if self.config.write_raw else "",
            "summary_path": str(self.summary_path) if self.csv_writer is not None else "",
            "sqlite_path": str(self.sqlite_output_path) if self.sqlite_writer is not None else "",
            "summary_interval_ms": float(self.config.summary_interval_ms),
            "gzip_summary": bool(self.config.gzip_summary),
        }

    def _handle_message(self, message: Any) -> None:
        receive_time = utc_now()
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if not isinstance(message, str) or message in {"PONG", "PING"}:
            return
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return
        events = parsed if isinstance(parsed, list) else [parsed]
        for event in events:
            if not isinstance(event, dict):
                continue
            self.raw_messages += 1
            if self.raw_writer is not None:
                self.raw_writer.write({
                    "receive_ts_utc": receive_time.isoformat(),
                    "slug": self.config.tokens.slug,
                    "event": event,
                })
            if self.sqlite_writer is not None:
                self.sqlite_rows += self.sqlite_writer.write_event(event, receive_time)
            if self.csv_writer is not None:
                rows = self.state.normalized_rows(event, self.config.tokens, receive_time)
                rows = self._filter_summary_rows(rows, event)
                self.summary_rows += self.csv_writer.write_rows(rows)

    def _filter_summary_rows(self, rows: list[dict[str, Any]], event: dict[str, Any]) -> list[dict[str, Any]]:
        interval_seconds = max(float(self.config.summary_interval_ms), 0.0) / 1000.0
        if interval_seconds <= 0.0 or not rows:
            return rows
        event_type = str(event.get("event_type") or "")
        if event_type in {"book", "last_trade_price", "new_market", "market_resolved", "tick_size_change"}:
            return rows
        now_ts = time.time()
        if now_ts - self._last_summary_write_ts < interval_seconds:
            return []
        self._last_summary_write_ts = now_ts
        return rows[:1]


def gzip_file(path: Path, remove_source: bool = False) -> Path:
    output = Path(str(path) + ".gz")
    with path.open("rb") as src, gzip.open(output, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    if remove_source and output.exists() and output.stat().st_size > 0:
        path.unlink()
    return output


def _read_sqlite_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return {str(key): str(value) for key, value in conn.execute("SELECT key, value FROM metadata")}


def _tokens_from_sqlite_metadata(metadata: dict[str, str]) -> MarketTokens:
    return MarketTokens(
        slug=metadata.get("slug", ""),
        window_start_ts=safe_int(metadata.get("window_start_ts"), 0),
        market_id=metadata.get("market_id", ""),
        yes_token_id=metadata.get("yes_token_id", ""),
        no_token_id=metadata.get("no_token_id", ""),
    )


def iter_summary_rows_from_sqlite(path: Path) -> Iterable[dict[str, Any]]:
    temp_path: Path | None = None
    sqlite_path = Path(path)
    if sqlite_path.name.endswith(".sqlite.gz"):
        temp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        temp_path = Path(temp.name)
        temp.close()
        with gzip.open(sqlite_path, "rb") as src, temp_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        sqlite_path = temp_path
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        tokens = _tokens_from_sqlite_metadata(_read_sqlite_metadata(conn))
        state = OrderBookState()
        for row in conn.execute("SELECT * FROM events ORDER BY id"):
            event_type = str(row["event_type"] or "")
            receive_time = dt.datetime.fromisoformat(str(row["receive_ts_utc"]))
            event = {
                "event_type": event_type,
                "timestamp": row["source_ts_ms"] or "",
                "market": row["market"] or "",
                "asset_id": row["asset_id"] or "",
                "hash": row["hash"] or "",
            }
            if event_type == "book":
                bids = []
                asks = []
                for level in conn.execute(
                    "SELECT side, price, size FROM book_levels WHERE event_id = ? ORDER BY side, price",
                    (row["id"],),
                ):
                    target = bids if str(level["side"]) == "bid" else asks
                    target.append({"price": level["price"], "size": level["size"]})
                event["bids"] = bids
                event["asks"] = asks
                yield from state.normalized_rows(event, tokens, receive_time)
                continue
            if event_type == "price_change":
                event["price_changes"] = [{
                    "asset_id": row["asset_id"] or "",
                    "side": row["side"] or "",
                    "price": row["price"] if row["price"] is not None else "",
                    "size": row["size"] if row["size"] is not None else "",
                    "best_bid": row["best_bid"] if row["best_bid"] is not None else "",
                    "best_ask": row["best_ask"] if row["best_ask"] is not None else "",
                    "hash": row["hash"] or "",
                }]
                yield from state.normalized_rows(event, tokens, receive_time)
                continue
            if event_type == "best_bid_ask":
                event["best_bid"] = row["best_bid"] if row["best_bid"] is not None else ""
                event["best_ask"] = row["best_ask"] if row["best_ask"] is not None else ""
            else:
                event["side"] = row["side"] or ""
                event["price"] = row["price"] if row["price"] is not None else ""
                event["size"] = row["size"] if row["size"] is not None else ""
            yield from state.normalized_rows(event, tokens, receive_time)
    finally:
        conn.close()
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
