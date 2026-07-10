from __future__ import annotations

import asyncio
import csv
import datetime as dt
import gzip
import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .market_ws_recorder import (
    MARKET_WS_URL,
    MarketTokens,
    OrderBookState,
    fetch_market_tokens,
    safe_float,
    updown_slug,
    utc_now,
)


FEATURE_COLUMNS = [
    "sample_ts_utc",
    "slug",
    "window_start_ts",
    "elapsed_s",
    "market_id",
    "yes_token_id",
    "no_token_id",
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
    "yes_quote_age_ms",
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
    "no_quote_age_ms",
    "directional_top_imbalance",
    "directional_depth_imbalance_40_60",
    "maker_pair_bid_sum",
    "maker_pair_edge",
    "taker_pair_ask_sum",
    "taker_pair_edge",
    "mid_pair_sum",
    "raw_messages_since_sample",
    "book_events_since_sample",
    "price_changes_since_sample",
    "best_bid_ask_since_sample",
    "last_trade_events_since_sample",
    "yes_updates_since_sample",
    "no_updates_since_sample",
    "last_trade_outcome",
    "last_trade_price",
    "last_trade_side",
    "last_trade_size",
]


def _finite_or_blank(value: Any) -> Any:
    if value == "":
        return ""
    number = safe_float(value, math.nan)
    return round(number, 6) if math.isfinite(number) else ""


def _pair_sum(first: float, second: float) -> float:
    return first + second if first > 0 and second > 0 else 0.0


def _dir_diff(first: Any, second: Any) -> Any:
    a = safe_float(first, math.nan)
    b = safe_float(second, math.nan)
    if math.isfinite(a) and math.isfinite(b):
        return round(a - b, 6)
    return ""


def directory_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


class FeatureCsvWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = gzip.open(self.path, "at", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=FEATURE_COLUMNS, extrasaction="ignore")
        self._writer.writeheader()
        self.rows = 0

    def write(self, row: dict[str, Any]) -> None:
        self._writer.writerow(row)
        self.rows += 1
        if self.rows % 50 == 0:
            self._handle.flush()

    def close(self) -> None:
        self._handle.flush()
        self._handle.close()


class FeatureState:
    def __init__(self, tokens: MarketTokens) -> None:
        self.tokens = tokens
        self.book = OrderBookState()
        self.counts = Counter()
        self.total_counts = Counter()
        self.last_update_ts: dict[str, float] = {}
        self.last_trade: dict[str, Any] = {}

    def ingest(self, message: Any, receive_time: dt.datetime) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if not isinstance(message, str) or message in {"PING", "PONG"}:
            return
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return
        events = parsed if isinstance(parsed, list) else [parsed]
        for event in events:
            if not isinstance(event, dict):
                continue
            self._ingest_event(event, receive_time)

    def row(self, sample_time: dt.datetime) -> dict[str, Any]:
        yes = self.book.metrics(self.tokens.yes_token_id)
        no = self.book.metrics(self.tokens.no_token_id)
        yes_bid = safe_float(yes.get("best_bid"), 0.0)
        yes_ask = safe_float(yes.get("best_ask"), 0.0)
        no_bid = safe_float(no.get("best_bid"), 0.0)
        no_ask = safe_float(no.get("best_ask"), 0.0)
        yes_mid = safe_float(yes.get("mid"), 0.0)
        no_mid = safe_float(no.get("mid"), 0.0)
        maker_sum = _pair_sum(yes_bid, no_bid)
        taker_sum = _pair_sum(yes_ask, no_ask)
        mid_sum = _pair_sum(yes_mid, no_mid)
        sample_ts = sample_time.timestamp()
        counts = self.counts
        self.counts = Counter()
        return {
            "sample_ts_utc": sample_time.isoformat(),
            "slug": self.tokens.slug,
            "window_start_ts": self.tokens.window_start_ts or "",
            "elapsed_s": round(max(sample_ts - float(self.tokens.window_start_ts or sample_ts), 0.0), 3),
            "market_id": self.tokens.market_id,
            "yes_token_id": self.tokens.yes_token_id,
            "no_token_id": self.tokens.no_token_id,
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
            "yes_quote_age_ms": self._quote_age_ms(self.tokens.yes_token_id, sample_ts),
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
            "no_quote_age_ms": self._quote_age_ms(self.tokens.no_token_id, sample_ts),
            "directional_top_imbalance": _dir_diff(yes.get("top_imbalance"), no.get("top_imbalance")),
            "directional_depth_imbalance_40_60": _dir_diff(
                yes.get("depth_imbalance_40_60"),
                no.get("depth_imbalance_40_60"),
            ),
            "maker_pair_bid_sum": round(maker_sum, 6) if maker_sum > 0 else "",
            "maker_pair_edge": round(1.0 - maker_sum, 6) if maker_sum > 0 else "",
            "taker_pair_ask_sum": round(taker_sum, 6) if taker_sum > 0 else "",
            "taker_pair_edge": round(1.0 - taker_sum, 6) if taker_sum > 0 else "",
            "mid_pair_sum": round(mid_sum, 6) if mid_sum > 0 else "",
            "raw_messages_since_sample": int(counts["raw_messages"]),
            "book_events_since_sample": int(counts["book"]),
            "price_changes_since_sample": int(counts["price_changes"]),
            "best_bid_ask_since_sample": int(counts["best_bid_ask"]),
            "last_trade_events_since_sample": int(counts["last_trade_price"]),
            "yes_updates_since_sample": int(counts["yes_updates"]),
            "no_updates_since_sample": int(counts["no_updates"]),
            "last_trade_outcome": self.last_trade.get("outcome", ""),
            "last_trade_price": self.last_trade.get("price", ""),
            "last_trade_side": self.last_trade.get("side", ""),
            "last_trade_size": self.last_trade.get("size", ""),
        }

    def _ingest_event(self, event: dict[str, Any], receive_time: dt.datetime) -> None:
        event_type = str(event.get("event_type") or "")
        self.counts["raw_messages"] += 1
        self.total_counts["raw_messages"] += 1
        receive_ts = receive_time.timestamp()
        if event_type == "price_change":
            changes = [change for change in event.get("price_changes") or [] if isinstance(change, dict)]
            self.counts["price_changes"] += len(changes)
            self.total_counts["price_changes"] += len(changes)
            for change in changes:
                self._mark_asset_update(str(change.get("asset_id") or ""), receive_ts)
            self.book.normalized_rows(event, self.tokens, receive_time)
            return
        if event_type == "book":
            self.counts["book"] += 1
            self.total_counts["book"] += 1
            self._mark_asset_update(str(event.get("asset_id") or ""), receive_ts)
            self.book.normalized_rows(event, self.tokens, receive_time)
            return
        if event_type == "best_bid_ask":
            self.counts["best_bid_ask"] += 1
            self.total_counts["best_bid_ask"] += 1
            self._mark_asset_update(str(event.get("asset_id") or ""), receive_ts)
            self.book.normalized_rows(event, self.tokens, receive_time)
            return
        if event_type == "last_trade_price":
            self.counts["last_trade_price"] += 1
            self.total_counts["last_trade_price"] += 1
            asset_id = str(event.get("asset_id") or "")
            self.last_trade = {
                "outcome": self.tokens.outcome_for(asset_id),
                "price": _finite_or_blank(event.get("price")),
                "side": event.get("side") or "",
                "size": _finite_or_blank(event.get("size")),
            }

    def _mark_asset_update(self, asset_id: str, receive_ts: float) -> None:
        if not asset_id:
            return
        self.last_update_ts[asset_id] = receive_ts
        outcome = self.tokens.outcome_for(asset_id)
        if outcome == "Yes":
            self.counts["yes_updates"] += 1
            self.total_counts["yes_updates"] += 1
        elif outcome == "No":
            self.counts["no_updates"] += 1
            self.total_counts["no_updates"] += 1

    def _quote_age_ms(self, asset_id: str, sample_ts: float) -> Any:
        last = self.last_update_ts.get(asset_id)
        if last is None:
            return ""
        return round(max(sample_ts - last, 0.0) * 1000.0, 3)


@dataclass(frozen=True)
class FeatureRecorderConfig:
    output_dir: Path
    slug_prefix: str = "btc-updown-5m"
    window_seconds: int = 300
    sample_interval_ms: float = 500.0
    max_runtime_seconds: float = 9 * 60 * 60
    max_output_mb: float = 500.0
    window_grace_seconds: float = 3.0
    retry_seconds: float = 5.0
    max_windows: int = 0


class FeatureRecorder:
    def __init__(self, config: FeatureRecorderConfig) -> None:
        self.config = config
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.completed_windows = 0

    async def run(self) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        while self._can_continue():
            if self._output_over_budget():
                print(json.dumps({
                    "event": "disk_guard_stop",
                    "output_dir": str(self.config.output_dir),
                    "size_mb": round(directory_size_bytes(self.config.output_dir) / 1024 / 1024, 3),
                    "max_output_mb": self.config.max_output_mb,
                }), flush=True)
                break
            try:
                slug = updown_slug(self.config.slug_prefix, int(self.config.window_seconds))
                tokens = await fetch_market_tokens(slug)
                report = await self._record_window(tokens)
                reports.append(report)
                self.completed_windows += 1
                print(json.dumps({"event": "feature_window_complete", **report}), flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(json.dumps({
                    "event": "feature_recorder_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }), flush=True)
                await asyncio.sleep(max(float(self.config.retry_seconds), 1.0))
        return reports

    async def _record_window(self, tokens: MarketTokens) -> dict[str, Any]:
        import websockets

        now_ts = time.time()
        window_end_ts = (
            float(tokens.window_start_ts or now_ts)
            + max(float(self.config.window_seconds), 1.0)
            + max(float(self.config.window_grace_seconds), 0.0)
        )
        deadline = min(
            window_end_ts,
            self.started_at + max(float(self.config.max_runtime_seconds), 1.0),
        )
        duration = max(deadline - now_ts, 1.0)
        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        path = self.config.output_dir / f"{tokens.slug}_{timestamp}.features.csv.gz"
        writer = FeatureCsvWriter(path)
        state = FeatureState(tokens)
        subscription = json.dumps({
            "assets_ids": tokens.token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        })
        sample_interval = max(float(self.config.sample_interval_ms), 100.0) / 1000.0
        next_sample = time.time()
        print(json.dumps({
            "event": "feature_window_start",
            "slug": tokens.slug,
            "duration_seconds": round(duration, 3),
            "window_seconds": int(self.config.window_seconds),
            "sample_interval_ms": round(sample_interval * 1000.0, 3),
            "path": str(path),
        }), flush=True)
        try:
            async with websockets.connect(MARKET_WS_URL, ping_interval=None, close_timeout=2, open_timeout=8) as websocket:
                await websocket.send(subscription)
                while time.time() < deadline:
                    now = time.time()
                    if now >= next_sample:
                        writer.write(state.row(utc_now()))
                        next_sample = now + sample_interval
                    timeout = max(min(next_sample - time.time(), deadline - time.time(), 1.0), 0.01)
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        continue
                    state.ingest(message, utc_now())
        finally:
            writer.close()
        return {
            "slug": tokens.slug,
            "path": str(path),
            "rows": writer.rows,
            "raw_messages": int(state.total_counts["raw_messages"]),
            "price_changes": int(state.total_counts["price_changes"]),
            "book_events": int(state.total_counts["book"]),
            "best_bid_ask_events": int(state.total_counts["best_bid_ask"]),
            "file_mb": round(path.stat().st_size / 1024 / 1024, 4) if path.exists() else 0.0,
        }

    def _can_continue(self) -> bool:
        if self.config.max_windows > 0 and self.completed_windows >= self.config.max_windows:
            return False
        return time.time() < self.started_at + max(float(self.config.max_runtime_seconds), 1.0)

    def _output_over_budget(self) -> bool:
        if self.config.max_output_mb <= 0:
            return False
        return directory_size_bytes(self.config.output_dir) > float(self.config.max_output_mb) * 1024 * 1024
