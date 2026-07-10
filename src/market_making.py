from __future__ import annotations

import csv
import datetime as dt
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from .market_ws_recorder import HTTP_HEADERS, safe_float
from .structural_arbitrage import fetch_order_books


CLOB_BASE_URL = "https://clob.polymarket.com"
LAST_CURSOR = "LTE="


@dataclass(frozen=True)
class MarketMakingScanConfig:
    output_dir: Path
    clob_base_url: str = CLOB_BASE_URL
    max_markets: int = 250
    page_size: int = 100
    book_chunk_size: int = 50
    request_timeout_s: float = 12.0
    min_daily_rate: float = 10.0
    min_volume_24hr: float = 1000.0
    min_avg_spread: float = 0.002
    max_mid_pair_deviation: float = 0.04
    max_one_day_price_change: float = 0.20
    min_hours_to_end: float = 12.0
    require_end_date: bool = True
    min_scoreable_sides: int = 2
    default_quote_size: float = 50.0
    max_quote_size: float = 500.0
    max_history_rows_per_scan: int = 50


@dataclass(frozen=True)
class MakerPaperSimConfig:
    history_csv: Path
    output_dir: Path
    horizon_scans: int = 1
    min_quote_score: float = 0.0
    quote_size_override: float = 0.0
    max_events: int = 100_000


def scan_market_making_rewards(config: MarketMakingScanConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    scan_ts = utc_iso()
    reward_markets = fetch_reward_markets(config)
    token_ids = _dedupe(
        token_id
        for market in reward_markets
        for token_id in reward_market_token_ids(market).values()
    )
    books = fetch_order_books(config, token_ids)

    rows = [
        evaluate_reward_market(scan_ts, market, books, config)
        for market in reward_markets
    ]
    rows = [row for row in rows if row]
    rows.sort(key=lambda row: safe_float(row.get("research_score"), -999.0), reverse=True)
    candidates = [row for row in rows if bool(row.get("candidate_ok"))]
    candidates.sort(key=lambda row: safe_float(row.get("research_score"), -999.0), reverse=True)

    latest_snapshot = config.output_dir / "latest_snapshot.csv"
    latest_candidates = config.output_dir / "latest_candidates.csv"
    candidate_history = config.output_dir / "candidate_history.csv"
    _write_csv(latest_snapshot, rows, append=False)
    _write_csv(latest_candidates, candidates, append=False)
    _write_csv(candidate_history, candidates[: max(int(config.max_history_rows_per_scan), 0)], append=True)

    report = {
        "scan_ts_utc": scan_ts,
        "markets_loaded": len(reward_markets),
        "token_ids_requested": len(token_ids),
        "books_loaded": len(books),
        "snapshot_rows": len(rows),
        "candidate_rows": len(candidates),
        "config": {
            "min_daily_rate": config.min_daily_rate,
            "min_volume_24hr": config.min_volume_24hr,
            "min_avg_spread": config.min_avg_spread,
            "max_mid_pair_deviation": config.max_mid_pair_deviation,
            "max_one_day_price_change": config.max_one_day_price_change,
            "min_hours_to_end": config.min_hours_to_end,
            "require_end_date": config.require_end_date,
            "min_scoreable_sides": config.min_scoreable_sides,
            "default_quote_size": config.default_quote_size,
            "max_quote_size": config.max_quote_size,
        },
        "top_candidates": candidates[:20],
        "top_non_candidates": rows[:20],
        "outputs": {
            "latest_snapshot": str(latest_snapshot),
            "latest_candidates": str(latest_candidates),
            "candidate_history": str(candidate_history),
            "scan_log": str(config.output_dir / "scan_log.jsonl"),
            "report": str(config.output_dir / "report.json"),
        },
    }
    (config.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (config.output_dir / "scan_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, separators=(",", ":"), ensure_ascii=True) + "\n")
    return report


def fetch_reward_markets(config: MarketMakingScanConfig) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    seen_market_keys: set[str] = set()
    next_cursor = ""
    page_size = max(1, min(int(config.page_size), 500))
    while len(markets) < int(config.max_markets):
        params: dict[str, Any] = {
            "page_size": page_size,
            "order_by": "rate_per_day",
            "position": "DESC",
        }
        if next_cursor:
            params["next_cursor"] = next_cursor
        response = requests.get(
            f"{config.clob_base_url.rstrip('/')}/rewards/markets/multi",
            params=params,
            headers=HTTP_HEADERS,
            timeout=float(config.request_timeout_s),
        )
        response.raise_for_status()
        payload = response.json()
        page = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(page, list) or not page:
            break
        for market in page:
            if not isinstance(market, dict) or not reward_market_token_ids(market):
                continue
            key = str(market.get("condition_id") or market.get("market_id") or market.get("market_slug") or "")
            if not key or key in seen_market_keys:
                continue
            seen_market_keys.add(key)
            markets.append(market)
            if len(markets) >= int(config.max_markets):
                break
        next_cursor = str(payload.get("next_cursor") or "") if isinstance(payload, dict) else ""
        if not next_cursor or next_cursor == LAST_CURSOR:
            break
    return markets


def evaluate_reward_market(
    scan_ts: str,
    market: dict[str, Any],
    books: dict[str, dict[str, Any]],
    config: MarketMakingScanConfig,
) -> dict[str, Any]:
    tokens = reward_market_token_ids(market)
    yes_token = tokens.get("yes", "")
    no_token = tokens.get("no", "")
    yes_book = books.get(yes_token, {})
    no_book = books.get(no_token, {})
    yes = book_metrics(yes_book)
    no = book_metrics(no_book)

    min_size = max(safe_float(market.get("rewards_min_size"), 0.0), 0.0)
    quote_size = max(float(config.default_quote_size), min_size)
    quote_size = min(quote_size, float(config.max_quote_size))
    max_spread = normalize_reward_spread(market.get("rewards_max_spread"))
    daily_rate = market_daily_rate(market)
    volume_24hr = safe_float(market.get("volume_24hr"), 0.0)
    one_day_change = safe_float(market.get("one_day_price_change"), 0.0)
    competitiveness = safe_float(market.get("market_competitiveness"), 0.0)
    hours_to_end = market_hours_to_end(market.get("end_date"), scan_ts)

    yes_plan = quote_plan(yes, quote_size, min_size, max_spread)
    no_plan = quote_plan(no, quote_size, min_size, max_spread)
    scoreable_sides = int(yes_plan["scoreable_sides"]) + int(no_plan["scoreable_sides"])
    avg_spread = _avg_positive([safe_float(yes.get("spread"), 0.0), safe_float(no.get("spread"), 0.0)])
    mid_pair_sum = _pair_sum(safe_float(yes.get("mid"), 0.0), safe_float(no.get("mid"), 0.0))
    mid_pair_deviation = abs(mid_pair_sum - 1.0) if mid_pair_sum > 0 else math.inf
    books_healthy = bool(yes.get("healthy")) and bool(no.get("healthy"))

    flags = candidate_flags(
        books_healthy=books_healthy,
        daily_rate=daily_rate,
        volume_24hr=volume_24hr,
        min_size=min_size,
        quote_size=quote_size,
        scoreable_sides=scoreable_sides,
        avg_spread=avg_spread,
        mid_pair_deviation=mid_pair_deviation,
        one_day_change=one_day_change,
        hours_to_end=hours_to_end,
        config=config,
    )
    candidate_ok = not flags
    score = research_score(
        daily_rate=daily_rate,
        volume_24hr=volume_24hr,
        scoreable_sides=scoreable_sides,
        avg_spread=avg_spread,
        mid_pair_deviation=mid_pair_deviation,
        one_day_change=one_day_change,
        competitiveness=competitiveness,
        candidate_ok=candidate_ok,
    )

    return {
        "scan_ts_utc": scan_ts,
        "candidate_ok": candidate_ok,
        "candidate_flags": ";".join(flags),
        "research_score": round(score, 6),
        "condition_id": str(market.get("condition_id") or ""),
        "event_id": str(market.get("event_id") or ""),
        "event_slug": str(market.get("event_slug") or ""),
        "market_id": str(market.get("market_id") or ""),
        "market_slug": str(market.get("market_slug") or ""),
        "question": str(market.get("question") or ""),
        "group_item_title": str(market.get("group_item_title") or ""),
        "end_date": str(market.get("end_date") or ""),
        "daily_rate": round(daily_rate, 6),
        "rewards_max_spread": safe_float(market.get("rewards_max_spread"), 0.0),
        "rewards_max_spread_price": round(max_spread, 6),
        "rewards_min_size": round(min_size, 6),
        "quote_size": round(quote_size, 6),
        "scoreable_sides": scoreable_sides,
        "volume_24hr": round(volume_24hr, 6),
        "one_day_price_change": round(one_day_change, 6),
        "hours_to_end": round(hours_to_end, 6) if math.isfinite(hours_to_end) else "",
        "market_competitiveness": round(competitiveness, 6),
        "avg_spread": round(avg_spread, 6) if math.isfinite(avg_spread) else "",
        "mid_pair_sum": round(mid_pair_sum, 6) if mid_pair_sum > 0 else "",
        "mid_pair_deviation": round(mid_pair_deviation, 6) if math.isfinite(mid_pair_deviation) else "",
        "yes_token_id": yes_token,
        "yes_best_bid": yes["best_bid"],
        "yes_best_bid_size": yes["best_bid_size"],
        "yes_best_ask": yes["best_ask"],
        "yes_best_ask_size": yes["best_ask_size"],
        "yes_spread": yes["spread"],
        "yes_mid": yes["mid"],
        "yes_depth_at_bid": yes["depth_at_bid"],
        "yes_depth_at_ask": yes["depth_at_ask"],
        "yes_bid_quote": yes_plan["bid_quote"],
        "yes_ask_quote": yes_plan["ask_quote"],
        "yes_bid_reward_score": yes_plan["bid_reward_score"],
        "yes_ask_reward_score": yes_plan["ask_reward_score"],
        "no_token_id": no_token,
        "no_best_bid": no["best_bid"],
        "no_best_bid_size": no["best_bid_size"],
        "no_best_ask": no["best_ask"],
        "no_best_ask_size": no["best_ask_size"],
        "no_spread": no["spread"],
        "no_mid": no["mid"],
        "no_depth_at_bid": no["depth_at_bid"],
        "no_depth_at_ask": no["depth_at_ask"],
        "no_bid_quote": no_plan["bid_quote"],
        "no_ask_quote": no_plan["ask_quote"],
        "no_bid_reward_score": no_plan["bid_reward_score"],
        "no_ask_reward_score": no_plan["ask_reward_score"],
    }


def reward_market_token_ids(market: dict[str, Any]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for token in market.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome") or "").strip().lower()
        token_id = str(token.get("token_id") or "")
        if outcome in {"yes", "no"} and token_id:
            tokens[outcome] = token_id
    return tokens if tokens.get("yes") and tokens.get("no") else {}


def book_metrics(book: dict[str, Any]) -> dict[str, Any]:
    bid, bid_size = best_level_from_book(book, "bid")
    ask, ask_size = best_level_from_book(book, "ask")
    spread = ask - bid if bid > 0 and ask > 0 else 0.0
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
    tick_size = safe_float(book.get("tick_size"), 0.01)
    return {
        "healthy": bool(bid > 0 and ask > 0 and bid <= ask),
        "best_bid": round(bid, 6) if bid > 0 else "",
        "best_bid_size": round(bid_size, 6) if bid_size > 0 else "",
        "best_ask": round(ask, 6) if ask > 0 else "",
        "best_ask_size": round(ask_size, 6) if ask_size > 0 else "",
        "spread": round(spread, 6) if spread > 0 else "",
        "mid": round(mid, 6) if mid > 0 else "",
        "tick_size": tick_size if tick_size > 0 else 0.01,
        "depth_at_bid": round(depth_at_price(book.get("bids") or [], bid), 6) if bid > 0 else "",
        "depth_at_ask": round(depth_at_price(book.get("asks") or [], ask), 6) if ask > 0 else "",
    }


def best_level_from_book(book: dict[str, Any], side: str) -> tuple[float, float]:
    levels = book.get("bids") if side == "bid" else book.get("asks")
    parsed = [
        (safe_float(level.get("price"), math.nan), safe_float(level.get("size"), math.nan))
        for level in levels or []
        if isinstance(level, dict)
    ]
    parsed = [(price, size) for price, size in parsed if math.isfinite(price) and math.isfinite(size) and size > 0]
    if not parsed:
        return 0.0, 0.0
    price = max(price for price, _size in parsed) if side == "bid" else min(price for price, _size in parsed)
    size = sum(size for level_price, size in parsed if abs(level_price - price) < 1e-12)
    return price, size


def depth_at_price(levels: Iterable[Any], price: float) -> float:
    if price <= 0:
        return 0.0
    depth = 0.0
    for level in levels:
        if not isinstance(level, dict):
            continue
        level_price = safe_float(level.get("price"), math.nan)
        if math.isfinite(level_price) and abs(level_price - price) < 1e-12:
            depth += safe_float(level.get("size"), 0.0)
    return depth


def quote_plan(metrics: dict[str, Any], quote_size: float, min_size: float, max_reward_spread: float) -> dict[str, Any]:
    bid = safe_float(metrics.get("best_bid"), 0.0)
    ask = safe_float(metrics.get("best_ask"), 0.0)
    mid = safe_float(metrics.get("mid"), 0.0)
    tick = max(safe_float(metrics.get("tick_size"), 0.01), 0.0001)
    if bid <= 0 or ask <= 0 or mid <= 0 or bid > ask:
        return {
            "bid_quote": "",
            "ask_quote": "",
            "bid_reward_score": "",
            "ask_reward_score": "",
            "scoreable_sides": 0,
        }
    if ask - bid > (2.0 * tick) + 1e-12:
        bid_quote = bid + tick
        ask_quote = ask - tick
    else:
        bid_quote = bid
        ask_quote = ask
    bid_distance = abs(mid - bid_quote)
    ask_distance = abs(ask_quote - mid)
    bid_score = reward_position_score(max_reward_spread, bid_distance, quote_size, min_size)
    ask_score = reward_position_score(max_reward_spread, ask_distance, quote_size, min_size)
    return {
        "bid_quote": round(bid_quote, 6),
        "ask_quote": round(ask_quote, 6),
        "bid_reward_score": round(bid_score, 6) if bid_score > 0 else "",
        "ask_reward_score": round(ask_score, 6) if ask_score > 0 else "",
        "scoreable_sides": int(bid_score > 0) + int(ask_score > 0),
    }


def reward_position_score(max_spread: float, distance_from_mid: float, quote_size: float, min_size: float) -> float:
    if max_spread <= 0 or quote_size < min_size or distance_from_mid > max_spread:
        return 0.0
    return ((max_spread - max(distance_from_mid, 0.0)) / max_spread) ** 2 * quote_size


def normalize_reward_spread(value: Any) -> float:
    spread = safe_float(value, 0.0)
    if spread <= 0:
        return 0.0
    return spread / 100.0 if spread > 1.0 else spread


def market_daily_rate(market: dict[str, Any]) -> float:
    total = safe_float(market.get("total_daily_rate"), math.nan)
    if math.isfinite(total) and total > 0:
        return total
    return sum(
        safe_float(config.get("rate_per_day"), 0.0)
        for config in market.get("rewards_config") or []
        if isinstance(config, dict)
    )


def market_hours_to_end(value: Any, scan_ts: str) -> float:
    if value in (None, ""):
        return math.inf
    try:
        end_dt = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return math.inf
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=dt.timezone.utc)
    try:
        now_dt = dt.datetime.fromisoformat(str(scan_ts).replace("Z", "+00:00"))
    except ValueError:
        now_dt = dt.datetime.now(dt.timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=dt.timezone.utc)
    return (end_dt.astimezone(dt.timezone.utc) - now_dt.astimezone(dt.timezone.utc)).total_seconds() / 3600.0


def candidate_flags(
    *,
    books_healthy: bool,
    daily_rate: float,
    volume_24hr: float,
    min_size: float,
    quote_size: float,
    scoreable_sides: int,
    avg_spread: float,
    mid_pair_deviation: float,
    one_day_change: float,
    hours_to_end: float,
    config: MarketMakingScanConfig,
) -> list[str]:
    flags: list[str] = []
    if not books_healthy:
        flags.append("unhealthy_book")
    if daily_rate < float(config.min_daily_rate):
        flags.append("low_reward_rate")
    if volume_24hr < float(config.min_volume_24hr):
        flags.append("low_volume")
    if min_size > float(config.max_quote_size) or quote_size < min_size:
        flags.append("min_size_too_large")
    if scoreable_sides < int(config.min_scoreable_sides):
        flags.append("not_enough_scoreable_sides")
    if not math.isfinite(avg_spread) or avg_spread < float(config.min_avg_spread):
        flags.append("spread_too_tight")
    if not math.isfinite(mid_pair_deviation) or mid_pair_deviation > float(config.max_mid_pair_deviation):
        flags.append("mid_pair_inconsistent")
    if abs(one_day_change) > float(config.max_one_day_price_change):
        flags.append("large_recent_move")
    if bool(config.require_end_date) and not math.isfinite(hours_to_end):
        flags.append("missing_end_date")
    if math.isfinite(hours_to_end) and hours_to_end < float(config.min_hours_to_end):
        flags.append("near_or_past_end")
    return flags


def research_score(
    *,
    daily_rate: float,
    volume_24hr: float,
    scoreable_sides: int,
    avg_spread: float,
    mid_pair_deviation: float,
    one_day_change: float,
    competitiveness: float,
    candidate_ok: bool,
) -> float:
    reward_component = math.log1p(max(daily_rate, 0.0))
    volume_component = math.log1p(max(volume_24hr, 0.0)) / 10.0
    scoreable_component = max(scoreable_sides, 0) / 4.0
    spread_component = min(max(avg_spread, 0.0) / 0.02, 1.0) if math.isfinite(avg_spread) else 0.0
    consistency_penalty = max(0.0, 1.0 - min(max(mid_pair_deviation, 0.0), 0.20) * 3.0)
    move_penalty = 1.0 / (1.0 + abs(one_day_change) * 6.0)
    competition_penalty = 1.0 / (1.0 + max(competitiveness, 0.0) / 500.0)
    candidate_multiplier = 1.0 if candidate_ok else 0.25
    return (
        reward_component
        * (0.35 + scoreable_component)
        * (0.50 + volume_component)
        * (0.50 + spread_component)
        * consistency_penalty
        * move_penalty
        * competition_penalty
        * candidate_multiplier
    )


def run_periodic_market_making_scan(
    config: MarketMakingScanConfig,
    iterations: int,
    interval_seconds: float,
) -> None:
    for iteration in range(max(int(iterations), 1)):
        try:
            report = scan_market_making_rewards(config)
            print(json.dumps(report, indent=2), flush=True)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "market_making_scan_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                ),
                flush=True,
            )
        if iteration + 1 >= max(int(iterations), 1):
            break
        time.sleep(max(float(interval_seconds), 1.0))


def simulate_market_making_history(config: MakerPaperSimConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    rows = _read_history_rows(config.history_csv)
    rows_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _market_key(row)
        if not key:
            continue
        row_ts = _parse_utc(row.get("scan_ts_utc"))
        if row_ts is None:
            continue
        normalized = dict(row)
        normalized["_scan_dt"] = row_ts
        rows_by_market[key].append(normalized)

    events: list[dict[str, Any]] = []
    horizon = max(int(config.horizon_scans), 1)
    for market_rows in rows_by_market.values():
        market_rows.sort(key=lambda row: row["_scan_dt"])
        for index, entry in enumerate(market_rows):
            next_index = index + horizon
            if next_index >= len(market_rows):
                continue
            next_row = market_rows[next_index]
            seconds = (next_row["_scan_dt"] - entry["_scan_dt"]).total_seconds()
            if seconds <= 0:
                continue
            for outcome in ("yes", "no"):
                for quote_side in ("bid", "ask"):
                    event = _simulate_quote_event(entry, next_row, outcome, quote_side, seconds, config)
                    if event:
                        events.append(event)
                        if len(events) >= int(config.max_events):
                            break
                if len(events) >= int(config.max_events):
                    break
            if len(events) >= int(config.max_events):
                break
        if len(events) >= int(config.max_events):
            break

    side_summary = _summarize_events(
        events,
        key_fields=["outcome", "quote_side"],
    )
    market_summary = _summarize_events(
        events,
        key_fields=["market_slug", "question"],
    )
    overall = _summarize_events(events, key_fields=[])[0] if events else _empty_summary_row({})

    events_path = config.output_dir / "maker_paper_events.csv"
    side_summary_path = config.output_dir / "maker_paper_side_summary.csv"
    market_summary_path = config.output_dir / "maker_paper_market_summary.csv"
    report_path = config.output_dir / "report.json"
    _write_csv(events_path, events, append=False)
    _write_csv(side_summary_path, side_summary, append=False)
    _write_csv(market_summary_path, market_summary, append=False)

    report = {
        "run_ts_utc": utc_iso(),
        "mode": "dry_research_only",
        "history_csv": str(config.history_csv),
        "rows_read": len(rows),
        "markets": len(rows_by_market),
        "horizon_scans": horizon,
        "quote_events": len(events),
        "overall": overall,
        "top_markets_conservative_reward": sorted(
            market_summary,
            key=lambda row: safe_float(row.get("net_pnl_conservative_reward_usd"), -math.inf),
            reverse=True,
        )[:20],
        "worst_markets_mark_to_mid": sorted(
            market_summary,
            key=lambda row: safe_float(row.get("mark_to_mid_pnl_usd"), math.inf),
        )[:20],
        "side_summary": side_summary,
        "caveats": [
            "Snapshot-only fill model: a quote is counted as filled only when the next sampled book crosses or touches it.",
            "Rewards are proxies using daily_rate, quote score, and market_competitiveness; they are not official reward accounting.",
            "Ask-side quotes assume inventory or a separate inventory plan; this simulator does not model wallet constraints.",
        ],
        "outputs": {
            "events": str(events_path),
            "side_summary": str(side_summary_path),
            "market_summary": str(market_summary_path),
            "report": str(report_path),
        },
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _avg_positive(values: list[float]) -> float:
    positives = [value for value in values if value > 0 and math.isfinite(value)]
    if not positives:
        return math.inf
    return sum(positives) / len(positives)


def _read_history_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _market_key(row: dict[str, Any]) -> str:
    return str(row.get("condition_id") or row.get("market_slug") or row.get("market_id") or "")


def _parse_utc(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _simulate_quote_event(
    entry: dict[str, Any],
    next_row: dict[str, Any],
    outcome: str,
    quote_side: str,
    seconds: float,
    config: MakerPaperSimConfig,
) -> dict[str, Any]:
    quote = safe_float(entry.get(f"{outcome}_{quote_side}_quote"), math.nan)
    reward_score = safe_float(entry.get(f"{outcome}_{quote_side}_reward_score"), 0.0)
    entry_mid = safe_float(entry.get(f"{outcome}_mid"), math.nan)
    next_mid = safe_float(next_row.get(f"{outcome}_mid"), math.nan)
    if (
        not math.isfinite(quote)
        or quote <= 0
        or not math.isfinite(entry_mid)
        or entry_mid <= 0
        or not math.isfinite(next_mid)
        or next_mid <= 0
        or reward_score <= float(config.min_quote_score)
    ):
        return {}

    quote_size = safe_float(entry.get("quote_size"), 0.0)
    if float(config.quote_size_override) > 0:
        quote_size = float(config.quote_size_override)
    if quote_size <= 0:
        return {}

    if quote_side == "bid":
        touch_price = safe_float(next_row.get(f"{outcome}_best_ask"), math.inf)
        filled = bool(math.isfinite(touch_price) and touch_price <= quote + 1e-12)
        pnl_per_share = next_mid - quote
        entry_edge_to_mid = entry_mid - quote
        next_move_for_quote = next_mid - entry_mid
    else:
        touch_price = safe_float(next_row.get(f"{outcome}_best_bid"), -math.inf)
        filled = bool(math.isfinite(touch_price) and touch_price >= quote - 1e-12)
        pnl_per_share = quote - next_mid
        entry_edge_to_mid = quote - entry_mid
        next_move_for_quote = entry_mid - next_mid

    mark_to_mid_pnl_usd = pnl_per_share * quote_size if filled else 0.0
    reward_proxy_usd = _reward_proxy_usd(entry, reward_score, seconds)
    reward_proxy_conservative_usd = 0.0 if filled else reward_proxy_usd

    return {
        "entry_ts_utc": str(entry.get("scan_ts_utc") or ""),
        "next_ts_utc": str(next_row.get("scan_ts_utc") or ""),
        "seconds": round(seconds, 3),
        "market_slug": str(entry.get("market_slug") or ""),
        "question": str(entry.get("question") or ""),
        "outcome": outcome,
        "quote_side": quote_side,
        "quote": round(quote, 6),
        "quote_size": round(quote_size, 6),
        "entry_mid": round(entry_mid, 6),
        "next_mid": round(next_mid, 6),
        "touch_price_next": round(touch_price, 6) if math.isfinite(touch_price) else "",
        "filled_by_next_snapshot": filled,
        "entry_edge_to_mid": round(entry_edge_to_mid, 6),
        "next_move_for_quote": round(next_move_for_quote, 6),
        "pnl_per_share_if_filled": round(pnl_per_share, 6),
        "mark_to_mid_pnl_usd": round(mark_to_mid_pnl_usd, 6),
        "daily_rate": round(safe_float(entry.get("daily_rate"), 0.0), 6),
        "market_competitiveness": round(safe_float(entry.get("market_competitiveness"), 0.0), 6),
        "reward_score": round(reward_score, 6),
        "reward_proxy_aggressive_usd": round(reward_proxy_usd, 6),
        "reward_proxy_conservative_usd": round(reward_proxy_conservative_usd, 6),
        "net_pnl_aggressive_reward_usd": round(mark_to_mid_pnl_usd + reward_proxy_usd, 6),
        "net_pnl_conservative_reward_usd": round(mark_to_mid_pnl_usd + reward_proxy_conservative_usd, 6),
    }


def _reward_proxy_usd(entry: dict[str, Any], quote_score: float, seconds: float) -> float:
    daily_rate = safe_float(entry.get("daily_rate"), 0.0)
    competitiveness = max(safe_float(entry.get("market_competitiveness"), 0.0), 0.0)
    if daily_rate <= 0 or quote_score <= 0 or seconds <= 0:
        return 0.0
    reward_pool = daily_rate * seconds / 86400.0
    share = quote_score / (competitiveness + quote_score) if competitiveness + quote_score > 0 else 0.0
    return reward_pool * min(max(share, 0.0), 1.0)


def _summarize_events(events: list[dict[str, Any]], key_fields: list[str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    if not key_fields:
        grouped[()].extend(events)
    else:
        for event in events:
            grouped[tuple(event.get(field, "") for field in key_fields)].append(event)

    summaries: list[dict[str, Any]] = []
    for key, group in grouped.items():
        prefix = {field: key[index] for index, field in enumerate(key_fields)}
        summaries.append(_summary_row(prefix, group))
    summaries.sort(
        key=lambda row: safe_float(row.get("net_pnl_conservative_reward_usd"), -math.inf),
        reverse=True,
    )
    return summaries


def _summary_row(prefix: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    row = _empty_summary_row(prefix)
    row["quote_events"] = len(events)
    if not events:
        return row
    fills = [event for event in events if str(event.get("filled_by_next_snapshot")).lower() == "true"]
    row["fills_by_next_snapshot"] = len(fills)
    row["fill_rate"] = round(len(fills) / len(events), 6)
    for field in (
        "mark_to_mid_pnl_usd",
        "reward_proxy_aggressive_usd",
        "reward_proxy_conservative_usd",
        "net_pnl_aggressive_reward_usd",
        "net_pnl_conservative_reward_usd",
    ):
        row[field] = round(sum(safe_float(event.get(field), 0.0) for event in events), 6)
        row[f"avg_{field}"] = round(row[field] / len(events), 6)
    if fills:
        row["avg_pnl_per_filled_quote_usd"] = round(
            sum(safe_float(event.get("mark_to_mid_pnl_usd"), 0.0) for event in fills) / len(fills),
            6,
        )
    mark_pnl = safe_float(row.get("mark_to_mid_pnl_usd"), 0.0)
    conservative_reward = safe_float(row.get("reward_proxy_conservative_usd"), 0.0)
    aggressive_reward = safe_float(row.get("reward_proxy_aggressive_usd"), 0.0)
    if mark_pnl < 0 and conservative_reward > 0:
        row["conservative_reward_fraction_needed_to_break_even"] = round((-mark_pnl) / conservative_reward, 6)
    if mark_pnl < 0 and aggressive_reward > 0:
        row["aggressive_reward_fraction_needed_to_break_even"] = round((-mark_pnl) / aggressive_reward, 6)
    return row


def _empty_summary_row(prefix: dict[str, Any]) -> dict[str, Any]:
    return {
        **prefix,
        "quote_events": 0,
        "fills_by_next_snapshot": 0,
        "fill_rate": 0.0,
        "mark_to_mid_pnl_usd": 0.0,
        "avg_mark_to_mid_pnl_usd": 0.0,
        "avg_pnl_per_filled_quote_usd": 0.0,
        "reward_proxy_aggressive_usd": 0.0,
        "avg_reward_proxy_aggressive_usd": 0.0,
        "reward_proxy_conservative_usd": 0.0,
        "avg_reward_proxy_conservative_usd": 0.0,
        "net_pnl_aggressive_reward_usd": 0.0,
        "avg_net_pnl_aggressive_reward_usd": 0.0,
        "net_pnl_conservative_reward_usd": 0.0,
        "avg_net_pnl_conservative_reward_usd": 0.0,
        "aggressive_reward_fraction_needed_to_break_even": "",
        "conservative_reward_fraction_needed_to_break_even": "",
    }


def _pair_sum(first: float, second: float) -> float:
    return first + second if first > 0 and second > 0 else 0.0


def _write_csv(path: Path, rows: list[dict[str, Any]], append: bool) -> None:
    if not rows and not append:
        path.write_text("", encoding="utf-8")
        return
    if not rows:
        return
    fieldnames: list[str] = []
    if append and path.exists() and path.stat().st_size > 0:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            try:
                fieldnames = next(reader)
            except StopIteration:
                fieldnames = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not append or not path.exists() or path.stat().st_size == 0
    with path.open("a" if append else "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
