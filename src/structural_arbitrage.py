from __future__ import annotations

import csv
import datetime as dt
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from .market_ws_recorder import HTTP_HEADERS, parse_jsonish_list, safe_float


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"


@dataclass(frozen=True)
class StructuralArbitrageConfig:
    output_dir: Path
    gamma_base_url: str = GAMMA_BASE_URL
    clob_base_url: str = CLOB_BASE_URL
    market_limit: int = 180
    market_page_size: int = 100
    event_limit: int = 20
    book_chunk_size: int = 50
    max_book_tokens: int = 800
    min_gross_edge: float = 0.001
    min_net_edge: float = 0.0
    min_top_shares: float = 5.0
    request_timeout_s: float = 12.0


def scan_structural_arbitrage(config: StructuralArbitrageConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    scan_ts = utc_iso()
    markets = fetch_gamma_markets(config)
    events = fetch_gamma_events(config)
    event_markets = _markets_from_events(events)
    markets_by_key = {_market_key(market): market for market in [*markets, *event_markets] if _market_key(market)}
    markets = list(markets_by_key.values())

    token_ids = _token_ids_for_markets(markets)
    basket_specs = _neg_risk_basket_specs(events)
    for basket in basket_specs:
        token_ids.extend(basket["yes_token_ids"])
    token_ids = _dedupe(token_ids)[: max(int(config.max_book_tokens), 0)]

    books = fetch_order_books(config, token_ids)
    binary_rows, binary_opps = _binary_complete_set_rows(scan_ts, markets, books, config)
    basket_rows, basket_opps = _basket_rows(scan_ts, basket_specs, books, config)
    snapshot_rows = [*binary_rows, *basket_rows]
    opportunities = [*binary_opps, *basket_opps]
    near_misses = _near_miss_rows(snapshot_rows, limit=100)

    _write_csv(config.output_dir / "latest_snapshot.csv", snapshot_rows, append=False)
    _write_csv(config.output_dir / "latest_near_misses.csv", near_misses, append=False)
    if opportunities:
        _write_csv(config.output_dir / "opportunities.csv", opportunities, append=True)

    report = {
        "scan_ts_utc": scan_ts,
        "markets_requested": int(config.market_limit),
        "markets_discovered": len(markets),
        "events_discovered": len(events),
        "basket_specs": len(basket_specs),
        "token_ids_requested": len(token_ids),
        "books_loaded": len(books),
        "snapshot_rows": len(snapshot_rows),
        "opportunities": len(opportunities),
        "min_gross_edge": config.min_gross_edge,
        "min_net_edge": config.min_net_edge,
        "min_top_shares": config.min_top_shares,
        "top_opportunities": sorted(
            opportunities,
            key=lambda row: safe_float(row.get("net_edge"), safe_float(row.get("gross_edge"), 0.0)),
            reverse=True,
        )[:20],
        "top_near_misses": near_misses[:20],
        "outputs": {
            "latest_snapshot": str(config.output_dir / "latest_snapshot.csv"),
            "latest_near_misses": str(config.output_dir / "latest_near_misses.csv"),
            "opportunities": str(config.output_dir / "opportunities.csv"),
            "scan_log": str(config.output_dir / "scan_log.jsonl"),
            "report": str(config.output_dir / "report.json"),
        },
    }
    (config.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (config.output_dir / "scan_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, separators=(",", ":"), ensure_ascii=True) + "\n")
    return report


def fetch_gamma_markets(config: StructuralArbitrageConfig) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    limit = max(1, min(int(config.market_page_size), 500))
    offset = 0
    while len(markets) < int(config.market_limit):
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        }
        response = requests.get(
            f"{config.gamma_base_url.rstrip('/')}/markets",
            params=params,
            headers=HTTP_HEADERS,
            timeout=float(config.request_timeout_s),
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list) or not page:
            break
        for market in page:
            if _is_tradable_market(market):
                markets.append(market)
            if len(markets) >= int(config.market_limit):
                break
        offset += limit
        if len(page) < limit:
            break
    return markets


def fetch_gamma_events(config: StructuralArbitrageConfig) -> list[dict[str, Any]]:
    params = {
        "limit": max(1, int(config.event_limit)),
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    response = requests.get(
        f"{config.gamma_base_url.rstrip('/')}/events",
        params=params,
        headers=HTTP_HEADERS,
        timeout=float(config.request_timeout_s),
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def fetch_order_books(config: StructuralArbitrageConfig, token_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    books: dict[str, dict[str, Any]] = {}
    unique_tokens = _dedupe(str(token_id) for token_id in token_ids if token_id)
    chunk_size = max(1, min(int(config.book_chunk_size), 100))
    for start in range(0, len(unique_tokens), chunk_size):
        chunk = unique_tokens[start : start + chunk_size]
        payload = [{"token_id": token_id} for token_id in chunk]
        try:
            response = requests.post(
                f"{config.clob_base_url.rstrip('/')}/books",
                json=payload,
                headers={**HTTP_HEADERS, "Content-Type": "application/json"},
                timeout=float(config.request_timeout_s),
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException:
            data = []
            for token_id in chunk:
                try:
                    single = requests.get(
                        f"{config.clob_base_url.rstrip('/')}/book",
                        params={"token_id": token_id},
                        headers=HTTP_HEADERS,
                        timeout=float(config.request_timeout_s),
                    )
                    single.raise_for_status()
                    data.append(single.json())
                except requests.RequestException:
                    continue
        for book in data if isinstance(data, list) else []:
            asset_id = str(book.get("asset_id") or "")
            if asset_id:
                books[asset_id] = book
    return books


def _binary_complete_set_rows(
    scan_ts: str,
    markets: list[dict[str, Any]],
    books: dict[str, dict[str, Any]],
    config: StructuralArbitrageConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    snapshot_rows: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []
    for market in markets:
        token_ids = market_token_ids(market)
        if len(token_ids) != 2 or any(token_id not in books for token_id in token_ids):
            continue
        token_books = [books[token_id] for token_id in token_ids]
        fee_rate = market_fee_rate(market)
        buy = complete_set_metrics(token_books, "buy", fee_rate)
        sell = complete_set_metrics(token_books, "sell", fee_rate)
        base = _market_base_row(scan_ts, market, len(token_ids))
        row = {
            **base,
            "scope": "binary",
            "buy_cost": buy["sum_price"],
            "buy_gross_edge": buy["gross_edge"],
            "buy_est_fee": buy["estimated_fee"],
            "buy_net_edge": buy["net_edge"],
            "buy_top_shares": buy["top_shares"],
            "sell_proceeds": sell["sum_price"],
            "sell_gross_edge": sell["gross_edge"],
            "sell_est_fee": sell["estimated_fee"],
            "sell_net_edge": sell["net_edge"],
            "sell_top_shares": sell["top_shares"],
            "book_hashes": ";".join(str(book.get("hash") or "") for book in token_books),
        }
        snapshot_rows.append(row)
        for side, metrics in (("buy_complete_set", buy), ("split_sell_complete_set", sell)):
            if _is_opportunity(metrics, config):
                opportunities.append({
                    **base,
                    "scope": "binary",
                    "opportunity_type": side,
                    "sum_price": metrics["sum_price"],
                    "gross_edge": metrics["gross_edge"],
                    "estimated_fee": metrics["estimated_fee"],
                    "net_edge": metrics["net_edge"],
                    "top_shares": metrics["top_shares"],
                    "prices": metrics["prices"],
                    "sizes": metrics["sizes"],
                    "book_hashes": row["book_hashes"],
                })
    return snapshot_rows, opportunities


def _basket_rows(
    scan_ts: str,
    baskets: list[dict[str, Any]],
    books: dict[str, dict[str, Any]],
    config: StructuralArbitrageConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    snapshot_rows: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []
    for basket in baskets:
        token_ids = [token_id for token_id in basket["yes_token_ids"] if token_id in books]
        if len(token_ids) != len(basket["yes_token_ids"]) or len(token_ids) < 2:
            continue
        token_books = [books[token_id] for token_id in token_ids]
        fee_rate = safe_float(basket.get("taker_fee_rate"), 0.0)
        buy = complete_set_metrics(token_books, "buy", fee_rate)
        sell = complete_set_metrics(token_books, "sell", fee_rate)
        base = {
            "scan_ts_utc": scan_ts,
            "event_slug": basket["event_slug"],
            "event_title": basket["event_title"],
            "market_slug": "",
            "question": basket["event_title"],
            "condition_id": "",
            "neg_risk": True,
            "neg_risk_market_id": basket["neg_risk_market_id"],
            "token_count": len(token_ids),
            "outcomes": ";".join(basket["outcomes"]),
            "volume24hr": basket.get("volume24hr", ""),
            "liquidity": basket.get("liquidity", ""),
            "taker_fee_rate": fee_rate,
        }
        row = {
            **base,
            "scope": "neg_risk_yes_basket",
            "buy_cost": buy["sum_price"],
            "buy_gross_edge": buy["gross_edge"],
            "buy_est_fee": buy["estimated_fee"],
            "buy_net_edge": buy["net_edge"],
            "buy_top_shares": buy["top_shares"],
            "sell_proceeds": sell["sum_price"],
            "sell_gross_edge": sell["gross_edge"],
            "sell_est_fee": sell["estimated_fee"],
            "sell_net_edge": sell["net_edge"],
            "sell_top_shares": sell["top_shares"],
            "book_hashes": ";".join(str(book.get("hash") or "") for book in token_books),
        }
        snapshot_rows.append(row)
        for side, metrics in (("buy_yes_basket", buy), ("split_sell_yes_basket", sell)):
            if _is_opportunity(metrics, config):
                opportunities.append({
                    **base,
                    "scope": "neg_risk_yes_basket",
                    "opportunity_type": side,
                    "sum_price": metrics["sum_price"],
                    "gross_edge": metrics["gross_edge"],
                    "estimated_fee": metrics["estimated_fee"],
                    "net_edge": metrics["net_edge"],
                    "top_shares": metrics["top_shares"],
                    "prices": metrics["prices"],
                    "sizes": metrics["sizes"],
                    "book_hashes": row["book_hashes"],
                })
    return snapshot_rows, opportunities


def complete_set_metrics(
    books: list[dict[str, Any]],
    side: str,
    fee_rate: float,
) -> dict[str, Any]:
    prices: list[float] = []
    sizes: list[float] = []
    estimated_fee = 0.0
    for book in books:
        price, size = best_level(book, side)
        if price is None or size is None:
            return {
                "sum_price": "",
                "gross_edge": "",
                "estimated_fee": "",
                "net_edge": "",
                "top_shares": 0.0,
                "prices": "",
                "sizes": "",
            }
        prices.append(price)
        sizes.append(size)
        estimated_fee += fee_rate * min(max(price, 0.0), max(1.0 - price, 0.0))
    sum_price = sum(prices)
    gross_edge = (1.0 - sum_price) if side == "buy" else (sum_price - 1.0)
    net_edge = gross_edge - estimated_fee
    return {
        "sum_price": round(sum_price, 6),
        "gross_edge": round(gross_edge, 6),
        "estimated_fee": round(estimated_fee, 6),
        "net_edge": round(net_edge, 6),
        "top_shares": round(min(sizes), 6) if sizes else 0.0,
        "prices": ";".join(f"{price:.6f}".rstrip("0").rstrip(".") for price in prices),
        "sizes": ";".join(f"{size:.6f}".rstrip("0").rstrip(".") for size in sizes),
    }


def best_level(book: dict[str, Any], side: str) -> tuple[float | None, float | None]:
    levels = book.get("asks") if side == "buy" else book.get("bids")
    parsed = [
        (safe_float(level.get("price"), math.nan), safe_float(level.get("size"), math.nan))
        for level in levels or []
        if isinstance(level, dict)
    ]
    parsed = [(price, size) for price, size in parsed if math.isfinite(price) and math.isfinite(size) and size > 0]
    if not parsed:
        return None, None
    price = min(price for price, _size in parsed) if side == "buy" else max(price for price, _size in parsed)
    size = sum(size for level_price, size in parsed if abs(level_price - price) < 1e-12)
    return price, size


def market_token_ids(market: dict[str, Any]) -> list[str]:
    token_ids = [str(token_id) for token_id in parse_jsonish_list(market.get("clobTokenIds"))]
    if token_ids:
        return token_ids
    tokens = market.get("tokens") or []
    return [str(token.get("token_id") or "") for token in tokens if isinstance(token, dict) and token.get("token_id")]


def market_outcomes(market: dict[str, Any]) -> list[str]:
    outcomes = [str(outcome) for outcome in parse_jsonish_list(market.get("outcomes"))]
    if outcomes:
        return outcomes
    tokens = market.get("tokens") or []
    return [str(token.get("outcome") or "") for token in tokens if isinstance(token, dict)]


def market_fee_rate(market: dict[str, Any]) -> float:
    bps = safe_float(market.get("takerBaseFee") or market.get("taker_base_fee"), 0.0)
    return max(bps, 0.0) / 10000.0


def _is_tradable_market(market: Any) -> bool:
    if not isinstance(market, dict):
        return False
    return (
        bool(market.get("active"))
        and not bool(market.get("closed"))
        and bool(market.get("enableOrderBook") or market.get("enable_order_book"))
        and bool(market.get("acceptingOrders") if "acceptingOrders" in market else market.get("accepting_orders", True))
        and len(market_token_ids(market)) == 2
    )


def _markets_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            if isinstance(market, dict) and _is_tradable_market(market):
                enriched = dict(market)
                enriched.setdefault("events", [{"slug": event.get("slug", ""), "title": event.get("title", "")}])
                markets.append(enriched)
    return markets


def _neg_risk_basket_specs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baskets: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or not bool(event.get("negRisk")):
            continue
        markets = [market for market in event.get("markets") or [] if _is_tradable_market(market)]
        yes_token_ids: list[str] = []
        outcomes: list[str] = []
        fee_rates: list[float] = []
        neg_risk_market_ids: set[str] = set()
        for market in markets:
            token_id = _yes_token_id(market)
            if not token_id:
                continue
            yes_token_ids.append(token_id)
            outcomes.append(str(market.get("groupItemTitle") or market.get("question") or market.get("slug") or ""))
            fee_rates.append(market_fee_rate(market))
            neg_id = str(market.get("negRiskMarketID") or "")
            if neg_id:
                neg_risk_market_ids.add(neg_id)
        if len(yes_token_ids) < 2:
            continue
        baskets.append({
            "event_slug": str(event.get("slug") or ""),
            "event_title": str(event.get("title") or ""),
            "neg_risk_market_id": ";".join(sorted(neg_risk_market_ids)),
            "yes_token_ids": yes_token_ids,
            "outcomes": outcomes,
            "volume24hr": event.get("volume24hr", ""),
            "liquidity": event.get("liquidity", ""),
            "taker_fee_rate": max(fee_rates) if fee_rates else 0.0,
        })
    return baskets


def _yes_token_id(market: dict[str, Any]) -> str:
    token_ids = market_token_ids(market)
    outcomes = [outcome.lower() for outcome in market_outcomes(market)]
    if "yes" not in outcomes:
        return ""
    index = outcomes.index("yes")
    return token_ids[index] if index < len(token_ids) else ""


def _market_base_row(scan_ts: str, market: dict[str, Any], token_count: int) -> dict[str, Any]:
    event = _first_event(market)
    return {
        "scan_ts_utc": scan_ts,
        "event_slug": str(event.get("slug") or ""),
        "event_title": str(event.get("title") or ""),
        "market_slug": str(market.get("slug") or market.get("market_slug") or ""),
        "question": str(market.get("question") or ""),
        "condition_id": str(market.get("conditionId") or market.get("condition_id") or ""),
        "neg_risk": bool(market.get("negRisk") or market.get("neg_risk")),
        "neg_risk_market_id": str(market.get("negRiskMarketID") or market.get("neg_risk_market_id") or ""),
        "token_count": token_count,
        "outcomes": ";".join(market_outcomes(market)),
        "volume24hr": market.get("volume24hr", market.get("volume24hrClob", "")),
        "liquidity": market.get("liquidity", market.get("liquidityClob", "")),
        "taker_fee_rate": market_fee_rate(market),
    }


def _first_event(market: dict[str, Any]) -> dict[str, Any]:
    events = market.get("events") or []
    return events[0] if events and isinstance(events[0], dict) else {}


def _market_key(market: dict[str, Any]) -> str:
    return str(market.get("conditionId") or market.get("condition_id") or market.get("slug") or "")


def _token_ids_for_markets(markets: Iterable[dict[str, Any]]) -> list[str]:
    token_ids: list[str] = []
    for market in markets:
        token_ids.extend(market_token_ids(market))
    return token_ids


def _is_opportunity(metrics: dict[str, Any], config: StructuralArbitrageConfig) -> bool:
    gross_edge = safe_float(metrics.get("gross_edge"), -999.0)
    net_edge = safe_float(metrics.get("net_edge"), -999.0)
    top_shares = safe_float(metrics.get("top_shares"), 0.0)
    return (
        top_shares >= float(config.min_top_shares)
        and gross_edge >= float(config.min_gross_edge)
        and net_edge >= float(config.min_net_edge)
    )


def _near_miss_rows(snapshot_rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in snapshot_rows:
        for prefix, opportunity_type in (
            ("buy", "buy_complete_set"),
            ("sell", "split_sell_complete_set"),
        ):
            gross_edge = safe_float(row.get(f"{prefix}_gross_edge"), -999.0)
            net_edge = safe_float(row.get(f"{prefix}_net_edge"), -999.0)
            top_shares = safe_float(row.get(f"{prefix}_top_shares"), 0.0)
            if gross_edge <= -900:
                continue
            rows.append({
                "scan_ts_utc": row.get("scan_ts_utc", ""),
                "scope": row.get("scope", ""),
                "opportunity_type": opportunity_type,
                "event_slug": row.get("event_slug", ""),
                "market_slug": row.get("market_slug", ""),
                "question": row.get("question", ""),
                "neg_risk": row.get("neg_risk", ""),
                "token_count": row.get("token_count", ""),
                "sum_price": row.get("buy_cost" if prefix == "buy" else "sell_proceeds", ""),
                "gross_edge": row.get(f"{prefix}_gross_edge", ""),
                "estimated_fee": row.get(f"{prefix}_est_fee", ""),
                "net_edge": row.get(f"{prefix}_net_edge", ""),
                "top_shares": top_shares,
                "outcomes": row.get("outcomes", ""),
            })
    rows.sort(
        key=lambda item: (
            safe_float(item.get("net_edge"), -999.0),
            safe_float(item.get("gross_edge"), -999.0),
            safe_float(item.get("top_shares"), 0.0),
        ),
        reverse=True,
    )
    return rows[: max(int(limit), 0)]


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


def run_periodic_structural_arbitrage(
    config: StructuralArbitrageConfig,
    iterations: int,
    interval_seconds: float,
) -> None:
    for iteration in range(max(int(iterations), 1)):
        report = scan_structural_arbitrage(config)
        print(json.dumps(report, indent=2), flush=True)
        if iteration + 1 >= max(int(iterations), 1):
            break
        time.sleep(max(float(interval_seconds), 1.0))
