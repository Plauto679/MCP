from __future__ import annotations

import csv
import datetime as dt
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from .market_making import book_metrics, market_hours_to_end
from .market_ws_recorder import HTTP_HEADERS, safe_float
from .structural_arbitrage import (
    CLOB_BASE_URL,
    GAMMA_BASE_URL,
    StructuralArbitrageConfig,
    fetch_gamma_markets,
    fetch_order_books,
    market_outcomes,
    market_token_ids,
)


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


@dataclass(frozen=True)
class ExternalFairValueConfig:
    output_dir: Path
    gamma_base_url: str = GAMMA_BASE_URL
    clob_base_url: str = CLOB_BASE_URL
    max_markets: int = 500
    market_page_size: int = 100
    book_chunk_size: int = 50
    max_book_tokens: int = 900
    request_timeout_s: float = 12.0
    min_modelability_score: int = 3
    min_volume_24hr: float = 250.0
    max_yes_spread: float = 0.12
    min_hours_to_end: float = 0.5
    include_crypto: bool = False
    max_history_rows_per_scan: int = 80
    fetch_external_prices: bool = True


@dataclass(frozen=True)
class MarketFamily:
    family: str
    modelability_score: int
    rationale: str


@dataclass(frozen=True)
class ThresholdSpec:
    direction: str
    threshold: float


def scan_external_fair_value(config: ExternalFairValueConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    scan_ts = utc_iso()
    markets = fetch_gamma_markets(_gamma_config(config))
    contexts = external_context(config)

    preliminary = [
        _preliminary_row(scan_ts, market, config, contexts)
        for market in markets
    ]
    preliminary = [
        row for row in preliminary
        if row["family"] != "crypto" or bool(config.include_crypto)
    ]
    modelable_prelim = [
        row for row in preliminary
        if int(row["modelability_score"]) >= int(config.min_modelability_score)
    ]

    token_ids = _dedupe(
        token_id
        for row in modelable_prelim
        for token_id in (row.get("yes_token_id", ""), row.get("no_token_id", ""))
        if token_id
    )[: max(int(config.max_book_tokens), 0)]
    books = fetch_order_books(config, token_ids)

    rows = [
        evaluate_external_market(row, books, config)
        for row in preliminary
    ]
    rows.sort(key=lambda row: safe_float(row.get("research_score"), -999.0), reverse=True)
    modelable_rows = [
        row for row in rows
        if int(row["modelability_score"]) >= int(config.min_modelability_score)
    ]
    candidates = [row for row in modelable_rows if bool(row.get("candidate_ok"))]

    latest_snapshot = config.output_dir / "latest_snapshot.csv"
    latest_modelable = config.output_dir / "latest_modelable.csv"
    latest_candidates = config.output_dir / "latest_candidates.csv"
    candidate_history = config.output_dir / "candidate_history.csv"
    _write_csv(latest_snapshot, rows, append=False)
    _write_csv(latest_modelable, modelable_rows, append=False)
    _write_csv(latest_candidates, candidates, append=False)
    _write_csv(candidate_history, modelable_rows[: max(int(config.max_history_rows_per_scan), 0)], append=True)

    family_counts: dict[str, int] = {}
    family_candidate_counts: dict[str, int] = {}
    for row in rows:
        family_counts[str(row.get("family") or "other")] = family_counts.get(str(row.get("family") or "other"), 0) + 1
    for row in candidates:
        family_candidate_counts[str(row.get("family") or "other")] = (
            family_candidate_counts.get(str(row.get("family") or "other"), 0) + 1
        )

    report = {
        "scan_ts_utc": scan_ts,
        "mode": "read_only_research",
        "markets_loaded": len(markets),
        "snapshot_rows": len(rows),
        "modelable_rows": len(modelable_rows),
        "candidate_rows": len(candidates),
        "token_ids_requested": len(token_ids),
        "books_loaded": len(books),
        "families": family_counts,
        "candidate_families": family_candidate_counts,
        "external_context": contexts,
        "config": {
            "max_markets": config.max_markets,
            "min_modelability_score": config.min_modelability_score,
            "min_volume_24hr": config.min_volume_24hr,
            "max_yes_spread": config.max_yes_spread,
            "min_hours_to_end": config.min_hours_to_end,
            "include_crypto": config.include_crypto,
            "fetch_external_prices": config.fetch_external_prices,
        },
        "top_modelable": modelable_rows[:25],
        "top_candidates": candidates[:25],
        "outputs": {
            "latest_snapshot": str(latest_snapshot),
            "latest_modelable": str(latest_modelable),
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


def evaluate_external_market(
    preliminary_row: dict[str, Any],
    books: dict[str, dict[str, Any]],
    config: ExternalFairValueConfig,
) -> dict[str, Any]:
    yes_token = str(preliminary_row.get("yes_token_id") or "")
    yes = book_metrics(books.get(yes_token, {}))
    yes_mid = safe_float(yes.get("mid"), 0.0)
    yes_spread = safe_float(yes.get("spread"), math.inf)
    books_healthy = bool(yes.get("healthy"))
    volume_24hr = safe_float(preliminary_row.get("volume24hr"), 0.0)
    hours_to_end = safe_float(preliminary_row.get("hours_to_end"), math.inf)
    family = str(preliminary_row.get("family") or "other")
    modelability_score = int(preliminary_row.get("modelability_score") or 0)

    flags = candidate_flags(
        family=family,
        modelability_score=modelability_score,
        books_healthy=books_healthy,
        yes_spread=yes_spread,
        volume_24hr=volume_24hr,
        hours_to_end=hours_to_end,
        threshold_direction=str(preliminary_row.get("threshold_direction") or ""),
        external_value=safe_float(preliminary_row.get("external_value"), math.nan),
        config=config,
    )
    candidate_ok = not flags
    score = research_score(
        family=family,
        modelability_score=modelability_score,
        volume_24hr=volume_24hr,
        yes_mid=yes_mid,
        yes_spread=yes_spread,
        hours_to_end=hours_to_end,
        candidate_ok=candidate_ok,
        threshold_distance=safe_float(preliminary_row.get("external_distance_to_threshold"), math.nan),
    )

    row = {
        **preliminary_row,
        "candidate_ok": candidate_ok,
        "candidate_flags": ";".join(flags),
        "research_score": round(score, 6),
        "yes_best_bid": yes["best_bid"],
        "yes_best_bid_size": yes["best_bid_size"],
        "yes_best_ask": yes["best_ask"],
        "yes_best_ask_size": yes["best_ask_size"],
        "yes_spread": yes["spread"],
        "yes_mid": yes["mid"],
        "yes_depth_at_bid": yes["depth_at_bid"],
        "yes_depth_at_ask": yes["depth_at_ask"],
    }
    return row


def classify_market(question: str, slug: str = "", event_title: str = "") -> MarketFamily:
    text = " ".join([question, slug, event_title]).lower()
    if _contains_any_word(text, ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto")):
        return MarketFamily("crypto", 2, "crypto_external_price_available_but_already_covered")
    if "wti" in text or "crude oil" in text or "oil closes" in text:
        return MarketFamily("wti_daily", 5, "liquid_external_reference_and_clear_thresholds")
    if _contains_any_word(text, ("fed", "fomc", "bps")) or any(
        token in text for token in ("interest rate", "rate cut", "rate hike")
    ):
        return MarketFamily("fed_rates", 4, "external_implied_probabilities_available_later")
    if _contains_any_word(text, ("cpi", "inflation", "ppi", "nonfarm", "unemployment")) or "jobs report" in text:
        return MarketFamily("macro_release", 4, "scheduled_macro_release_with_external_consensus_data")
    if _contains_any_word(text, ("weather", "temperature", "rain", "snow", "hurricane", "fahrenheit", "degrees", "degree")):
        return MarketFamily("weather", 4, "external_forecasts_available_later")
    if _contains_any_word(text, ("election", "poll", "president", "senate", "congress", "mayor", "governor")):
        return MarketFamily("polling_politics", 3, "external_polls_and_base_rates_available_later")
    if _contains_any_word(text, ("nfl", "nba", "mlb", "nhl", "ufc", "soccer", "football", "tennis", "f1")) or any(
        token in text for token in ("world cup", "formula 1")
    ):
        return MarketFamily("sports", 3, "sports_books_and_stats_possible_later")
    return MarketFamily("other", 0, "no_obvious_external_fair_value_source")


def parse_threshold(question: str) -> ThresholdSpec | None:
    text = str(question or "").lower().replace(",", "")
    pattern = re.compile(
        r"\b(above|over|greater than|at or above|below|under|less than|at or below)\b\s*\$?\s*([0-9]+(?:\.[0-9]+)?)"
    )
    match = pattern.search(text)
    if match:
        direction_text = match.group(1)
        direction = "below" if direction_text in {"below", "under", "less than", "at or below"} else "above"
        return ThresholdSpec(direction=direction, threshold=float(match.group(2)))
    hit_match = re.search(
        r"\b(?:hit|reach|reaches|reaching)\s*(?:\((high|low)\))?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
        text,
    )
    if hit_match:
        direction = "below" if hit_match.group(1) == "low" else "above"
        return ThresholdSpec(direction=direction, threshold=float(hit_match.group(2)))
    dip_match = re.search(r"\b(?:dip|dips|drop|drops|fall|falls)\s*(?:to|below)?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", text)
    if dip_match:
        return ThresholdSpec(direction="below", threshold=float(dip_match.group(1)))
    return None


def fetch_yahoo_last_price(symbol: str, timeout_s: float = 8.0) -> dict[str, Any]:
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": "1d", "interval": "1m"},
        headers=HTTP_HEADERS,
        timeout=float(timeout_s),
    )
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return {"symbol": symbol, "price": "", "source": "yahoo_chart", "error": "missing_result"}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    price = safe_float(meta.get("regularMarketPrice"), math.nan)
    if not math.isfinite(price):
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        parsed = [safe_float(value, math.nan) for value in closes]
        parsed = [value for value in parsed if math.isfinite(value) and value > 0]
        price = parsed[-1] if parsed else math.nan
    return {
        "symbol": symbol,
        "price": round(price, 6) if math.isfinite(price) else "",
        "source": "yahoo_chart",
        "exchange_timezone": str(meta.get("exchangeTimezoneName") or ""),
        "regular_market_time": meta.get("regularMarketTime", ""),
    }


def fetch_yahoo_price_history_stats(
    symbol: str,
    *,
    range_value: str = "3mo",
    interval: str = "1d",
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": range_value, "interval": interval},
        headers=HTTP_HEADERS,
        timeout=float(timeout_s),
    )
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return {"symbol": symbol, "source": "yahoo_chart", "error": "missing_result"}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    parsed = [safe_float(value, math.nan) for value in closes]
    prices = [value for value in parsed if math.isfinite(value) and value > 0]
    returns = [
        math.log(prices[index] / prices[index - 1])
        for index in range(1, len(prices))
        if prices[index - 1] > 0 and prices[index] > 0
    ]
    annual_vol = _annualized_volatility(returns, periods_per_year=252.0)
    price = safe_float(meta.get("regularMarketPrice"), math.nan)
    if not math.isfinite(price) and prices:
        price = prices[-1]
    return {
        "symbol": symbol,
        "price": round(price, 6) if math.isfinite(price) else "",
        "source": "yahoo_chart",
        "range": range_value,
        "interval": interval,
        "observations": len(prices),
        "annual_volatility": round(annual_vol, 6) if math.isfinite(annual_vol) else "",
        "exchange_timezone": str(meta.get("exchangeTimezoneName") or ""),
        "regular_market_time": meta.get("regularMarketTime", ""),
    }


def external_context(config: ExternalFairValueConfig) -> dict[str, Any]:
    if not bool(config.fetch_external_prices):
        return {}
    context: dict[str, Any] = {}
    try:
        context["wti"] = fetch_yahoo_price_history_stats(
            "CL=F",
            range_value="3mo",
            interval="1d",
            timeout_s=min(float(config.request_timeout_s), 8.0),
        )
    except requests.RequestException as exc:
        context["wti"] = {
            "symbol": "CL=F",
            "price": "",
            "source": "yahoo_chart",
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }
    return context


def _annualized_volatility(returns: list[float], periods_per_year: float) -> float:
    values = [value for value in returns if math.isfinite(value)]
    if len(values) < 2:
        return math.nan
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(variance, 0.0)) * math.sqrt(periods_per_year)


def candidate_flags(
    *,
    family: str,
    modelability_score: int,
    books_healthy: bool,
    yes_spread: float,
    volume_24hr: float,
    hours_to_end: float,
    threshold_direction: str,
    external_value: float,
    config: ExternalFairValueConfig,
) -> list[str]:
    flags: list[str] = []
    if modelability_score < int(config.min_modelability_score):
        flags.append("low_modelability")
    if not books_healthy:
        flags.append("unhealthy_book")
    if not math.isfinite(yes_spread) or yes_spread > float(config.max_yes_spread):
        flags.append("wide_or_missing_spread")
    if volume_24hr < float(config.min_volume_24hr):
        flags.append("low_volume")
    if not math.isfinite(hours_to_end):
        flags.append("missing_end_date")
    elif hours_to_end < float(config.min_hours_to_end):
        flags.append("near_or_past_end")
    if family == "wti_daily":
        if not threshold_direction:
            flags.append("missing_threshold")
        if not math.isfinite(external_value):
            flags.append("missing_external_proxy")
    return flags


def research_score(
    *,
    family: str,
    modelability_score: int,
    volume_24hr: float,
    yes_mid: float,
    yes_spread: float,
    hours_to_end: float,
    candidate_ok: bool,
    threshold_distance: float,
) -> float:
    family_bonus = {
        "wti_daily": 1.35,
        "fed_rates": 1.15,
        "macro_release": 1.10,
        "weather": 1.05,
        "polling_politics": 0.95,
        "sports": 0.85,
        "crypto": 0.60,
    }.get(family, 0.25)
    model_component = max(float(modelability_score), 0.0)
    volume_component = math.log1p(max(volume_24hr, 0.0)) / 8.0
    spread_component = 1.0 / (1.0 + max(yes_spread if math.isfinite(yes_spread) else 0.25, 0.0) * 8.0)
    time_component = 1.0 if not math.isfinite(hours_to_end) else min(max(hours_to_end / 12.0, 0.15), 1.25)
    mid_component = 0.7 + min(abs(yes_mid - 0.5), 0.45) if yes_mid > 0 else 0.5
    if math.isfinite(threshold_distance):
        distance_component = 1.0 / (1.0 + abs(threshold_distance) / 3.0)
    else:
        distance_component = 0.75
    candidate_multiplier = 1.0 if candidate_ok else 0.35
    return (
        model_component
        * family_bonus
        * (0.5 + volume_component)
        * spread_component
        * time_component
        * mid_component
        * distance_component
        * candidate_multiplier
    )


def run_periodic_external_fair_value_scan(
    config: ExternalFairValueConfig,
    iterations: int,
    interval_seconds: float,
) -> None:
    for iteration in range(max(int(iterations), 1)):
        try:
            report = scan_external_fair_value(config)
            print(json.dumps(report, indent=2), flush=True)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "external_fair_value_scan_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                ),
                flush=True,
            )
        if iteration + 1 >= max(int(iterations), 1):
            break
        time.sleep(max(float(interval_seconds), 1.0))


def _preliminary_row(
    scan_ts: str,
    market: dict[str, Any],
    config: ExternalFairValueConfig,
    contexts: dict[str, Any],
) -> dict[str, Any]:
    event = _first_event(market)
    question = str(market.get("question") or "")
    slug = str(market.get("slug") or market.get("market_slug") or "")
    family = classify_market(question, slug, str(event.get("title") or ""))
    tokens = _yes_no_token_ids(market)
    threshold = parse_threshold(question)
    external_value = math.nan
    external_distance = math.nan
    external_status = ""
    external_source = ""
    if family.family == "wti_daily":
        wti = contexts.get("wti") if isinstance(contexts, dict) else {}
        if isinstance(wti, dict):
            external_value = safe_float(wti.get("price"), math.nan)
            external_source = str(wti.get("source") or "")
        if threshold is not None and math.isfinite(external_value):
            if threshold.direction == "above":
                external_distance = external_value - threshold.threshold
                external_status = "currently_above" if external_distance > 0 else "currently_below"
            else:
                external_distance = threshold.threshold - external_value
                external_status = "currently_below" if external_distance > 0 else "currently_above"

    end_date = str(market.get("endDate") or market.get("end_date") or "")
    hours_to_end = market_hours_to_end(end_date, scan_ts)
    volume_24hr = safe_float(market.get("volume24hr", market.get("volume24hrClob", "")), 0.0)
    liquidity = safe_float(market.get("liquidity", market.get("liquidityClob", "")), 0.0)
    return {
        "scan_ts_utc": scan_ts,
        "family": family.family,
        "modelability_score": family.modelability_score,
        "modelability_rationale": family.rationale,
        "event_slug": str(event.get("slug") or ""),
        "event_title": str(event.get("title") or ""),
        "market_slug": slug,
        "question": question,
        "condition_id": str(market.get("conditionId") or market.get("condition_id") or ""),
        "end_date": end_date,
        "hours_to_end": round(hours_to_end, 6) if math.isfinite(hours_to_end) else "",
        "volume24hr": round(volume_24hr, 6),
        "liquidity": round(liquidity, 6),
        "outcomes": ";".join(market_outcomes(market)),
        "yes_token_id": tokens.get("yes", ""),
        "no_token_id": tokens.get("no", ""),
        "threshold_direction": threshold.direction if threshold else "",
        "threshold": threshold.threshold if threshold else "",
        "external_value": round(external_value, 6) if math.isfinite(external_value) else "",
        "external_source": external_source,
        "external_status": external_status,
        "external_distance_to_threshold": (
            round(external_distance, 6) if math.isfinite(external_distance) else ""
        ),
    }


def _yes_no_token_ids(market: dict[str, Any]) -> dict[str, str]:
    token_ids = market_token_ids(market)
    outcomes = [outcome.lower() for outcome in market_outcomes(market)]
    result: dict[str, str] = {}
    for outcome in ("yes", "no"):
        if outcome in outcomes:
            index = outcomes.index(outcome)
            if index < len(token_ids):
                result[outcome] = token_ids[index]
    if result.get("yes") and result.get("no"):
        return result
    if len(token_ids) >= 2:
        return {"yes": token_ids[0], "no": token_ids[1]}
    return {}


def _contains_any_word(text: str, words: Iterable[str]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _gamma_config(config: ExternalFairValueConfig) -> StructuralArbitrageConfig:
    return StructuralArbitrageConfig(
        output_dir=config.output_dir,
        gamma_base_url=config.gamma_base_url,
        clob_base_url=config.clob_base_url,
        market_limit=config.max_markets,
        market_page_size=config.market_page_size,
        book_chunk_size=config.book_chunk_size,
        max_book_tokens=config.max_book_tokens,
        request_timeout_s=config.request_timeout_s,
    )


def _first_event(market: dict[str, Any]) -> dict[str, Any]:
    events = market.get("events") or []
    return events[0] if events and isinstance(events[0], dict) else {}


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
