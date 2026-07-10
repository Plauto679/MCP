from __future__ import annotations

import calendar
import csv
import datetime as dt
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from .market_ws_recorder import HTTP_HEADERS, safe_float


FRED_DFF_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

MONTH_CODES = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}

MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


@dataclass(frozen=True)
class FedRateProbability:
    probability: float
    source: str
    outcome_type: str
    meeting_month: str
    contract_symbol: str = ""
    futures_price: float = math.nan
    current_effr: float = math.nan
    implied_monthly_rate: float = math.nan
    expected_post_meeting_rate: float = math.nan
    expected_change_bps: float = math.nan
    warning: str = ""


def fetch_fed_rates_context(
    rows: Iterable[dict[str, Any]],
    *,
    manual_probabilities_csv: Path | None = None,
    current_effr_fallback: float = 3.625,
    request_timeout_s: float = 8.0,
) -> dict[str, Any]:
    meeting_keys = sorted(
        {
            key
            for row in rows
            for key in [_meeting_key(row)]
            if key
        }
    )
    fred_error = ""
    try:
        current_effr = _fetch_latest_effr(timeout_s=request_timeout_s)
    except requests.RequestException as exc:
        current_effr = math.nan
        fred_error = f"{type(exc).__name__}: {str(exc)[:240]}"
    context: dict[str, Any] = {
        "source": "fred_dff_yahoo_zq_proxy",
        "method": "30d_fed_funds_futures_month_average_proxy",
        "current_effr": (
            round(current_effr, 6)
            if math.isfinite(current_effr)
            else round(max(float(current_effr_fallback), 0.0), 6)
        ),
        "current_effr_source": "fred_dff" if math.isfinite(current_effr) else "fallback",
        "current_effr_error": fred_error,
        "meeting_keys": meeting_keys,
        "futures": {},
        "manual_probabilities": _load_manual_probabilities(manual_probabilities_csv),
        "caveats": [
            "This is not CME FedWatch official distribution data.",
            "It uses FRED effective fed funds plus Yahoo 30-Day Fed Funds futures as a conservative proxy.",
            "A single monthly futures price gives an expected rate, not a full outcome distribution.",
        ],
    }
    for year, month in meeting_keys:
        symbol = fed_funds_symbol(year, month)
        try:
            quote = fetch_yahoo_futures_price(symbol, timeout_s=request_timeout_s)
        except requests.RequestException as exc:
            quote = {
                "symbol": symbol,
                "source": "yahoo_chart",
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
            }
        context["futures"][f"{year:04d}-{month:02d}"] = quote
    return context


def fed_probability_details(row: dict[str, Any], context: dict[str, Any] | None) -> FedRateProbability:
    manual = _manual_probability(row, context or {})
    if manual:
        return manual

    question = str(row.get("question") or "")
    meeting = _meeting_key(row)
    outcome_type, threshold_bps = parse_rate_outcome(question)
    if "meeting" not in question.lower():
        return FedRateProbability(
            math.nan,
            "fed_proxy_unsupported_non_meeting_market",
            outcome_type,
            _meeting_label(meeting),
            warning="unsupported_non_meeting_market",
        )
    if meeting is None:
        return FedRateProbability(math.nan, "fed_proxy_missing_meeting", outcome_type, "", warning="missing_meeting")
    if not outcome_type:
        return FedRateProbability(math.nan, "fed_proxy_unsupported_outcome", "", _meeting_label(meeting), warning="unsupported_outcome")

    year, month = meeting
    future = ((context or {}).get("futures") or {}).get(f"{year:04d}-{month:02d}", {})
    futures_price = safe_float(future.get("price"), math.nan) if isinstance(future, dict) else math.nan
    current_effr = safe_float((context or {}).get("current_effr"), math.nan)
    if not math.isfinite(futures_price) or not math.isfinite(current_effr):
        return FedRateProbability(
            math.nan,
            "fed_proxy_missing_market_data",
            outcome_type,
            _meeting_label(meeting),
            contract_symbol=fed_funds_symbol(year, month),
            futures_price=futures_price,
            current_effr=current_effr,
            warning="missing_market_data",
        )

    decision_date = _decision_date(row, year, month)
    implied_monthly_rate = 100.0 - futures_price
    post_rate = _post_meeting_rate_from_month_average(
        implied_monthly_rate=implied_monthly_rate,
        current_effr=current_effr,
        decision_date=decision_date,
    )
    expected_change_bps = (post_rate - current_effr) * 100.0
    probability = _probability_from_expected_change(
        expected_change_bps,
        outcome_type=outcome_type,
        threshold_bps=threshold_bps,
    )
    return FedRateProbability(
        probability=probability,
        source="fred_dff_yahoo_zq_proxy",
        outcome_type=outcome_type,
        meeting_month=_meeting_label(meeting),
        contract_symbol=fed_funds_symbol(year, month),
        futures_price=futures_price,
        current_effr=current_effr,
        implied_monthly_rate=implied_monthly_rate,
        expected_post_meeting_rate=post_rate,
        expected_change_bps=expected_change_bps,
    )


def parse_rate_outcome(question: str) -> tuple[str, float]:
    text = str(question or "").lower()
    bps_match = re.search(r"\b([0-9]+)\+?\s*bps\b", text)
    threshold_bps = float(bps_match.group(1)) if bps_match else 25.0
    if "no change" in text or "pause" in text:
        return "no_change", 0.0
    if "decide differently" in text:
        return "different", 0.0
    if any(token in text for token in ("increase", "hike", "raise")):
        return "hike_at_least", threshold_bps
    if any(token in text for token in ("decrease", "cut", "lower")):
        return "cut_at_least", threshold_bps
    return "", math.nan


def fed_funds_symbol(year: int, month: int) -> str:
    code = MONTH_CODES[int(month)]
    return f"ZQ{code}{int(year) % 100:02d}.CBT"


def fetch_yahoo_futures_price(symbol: str, *, timeout_s: float = 8.0) -> dict[str, Any]:
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": "5d", "interval": "1d"},
        headers=HTTP_HEADERS,
        timeout=float(timeout_s),
    )
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return {"symbol": symbol, "source": "yahoo_chart", "error": "missing_result"}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    price = safe_float(meta.get("regularMarketPrice"), math.nan)
    if not math.isfinite(price):
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        parsed = [safe_float(value, math.nan) for value in closes]
        prices = [value for value in parsed if math.isfinite(value) and value > 0]
        price = prices[-1] if prices else math.nan
    return {
        "symbol": symbol,
        "price": round(price, 6) if math.isfinite(price) else "",
        "source": "yahoo_chart",
        "regular_market_time": meta.get("regularMarketTime", ""),
        "exchange_timezone": str(meta.get("exchangeTimezoneName") or ""),
    }


def _fetch_latest_effr(*, timeout_s: float) -> float:
    response = requests.get(
        FRED_DFF_CSV_URL,
        params={"id": "DFF"},
        headers=HTTP_HEADERS,
        timeout=float(timeout_s),
    )
    response.raise_for_status()
    rows = list(csv.DictReader(response.text.splitlines()))
    for row in reversed(rows):
        value = safe_float(row.get("DFF"), math.nan)
        if math.isfinite(value):
            return value
    return math.nan


def _load_manual_probabilities(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _manual_probability(row: dict[str, Any], context: dict[str, Any]) -> FedRateProbability | None:
    rows = context.get("manual_probabilities") or []
    if not isinstance(rows, list):
        return None
    market_slug = str(row.get("market_slug") or "")
    condition_id = str(row.get("condition_id") or "")
    meeting = _meeting_key(row)
    outcome_type, _ = parse_rate_outcome(str(row.get("question") or ""))
    for manual in rows:
        if market_slug and market_slug == str(manual.get("market_slug") or ""):
            probability = safe_float(manual.get("probability"), math.nan)
        elif condition_id and condition_id == str(manual.get("condition_id") or ""):
            probability = safe_float(manual.get("probability"), math.nan)
        elif meeting and _manual_meeting_match(manual, meeting) and outcome_type == str(manual.get("outcome_type") or ""):
            probability = safe_float(manual.get("probability"), math.nan)
        else:
            continue
        if math.isfinite(probability):
            if probability > 1.0 and probability <= 100.0:
                probability /= 100.0
            return FedRateProbability(
                probability=min(max(probability, 0.0), 1.0),
                source=str(manual.get("source") or "manual_fedwatch_csv"),
                outcome_type=outcome_type,
                meeting_month=_meeting_label(meeting) if meeting else "",
            )
    return None


def _manual_meeting_match(row: dict[str, Any], meeting: tuple[int, int]) -> bool:
    year, month = meeting
    return int(safe_float(row.get("year"), -1)) == year and int(safe_float(row.get("month"), -1)) == month


def _meeting_key(row: dict[str, Any]) -> tuple[int, int] | None:
    question = str(row.get("question") or "")
    text = question.lower()
    for name, month in MONTH_NAMES.items():
        match = re.search(rf"\b{name}\b(?:\s+|-)(20[0-9]{{2}})\b", text)
        if match:
            return int(match.group(1)), month
    end_date = _parse_utc(row.get("end_date"))
    if end_date:
        return end_date.year, end_date.month
    return None


def _decision_date(row: dict[str, Any], year: int, month: int) -> dt.date:
    end = _parse_utc(row.get("end_date"))
    if end and end.year == year and end.month == month:
        return end.date()
    return dt.date(year, month, min(15, calendar.monthrange(year, month)[1]))


def _post_meeting_rate_from_month_average(
    *,
    implied_monthly_rate: float,
    current_effr: float,
    decision_date: dt.date,
) -> float:
    days_in_month = calendar.monthrange(decision_date.year, decision_date.month)[1]
    post_days = max(days_in_month - decision_date.day + 1, 1)
    pre_days = max(days_in_month - post_days, 0)
    return (implied_monthly_rate * days_in_month - current_effr * pre_days) / post_days


def _probability_from_expected_change(expected_change_bps: float, *, outcome_type: str, threshold_bps: float) -> float:
    if not math.isfinite(expected_change_bps):
        return math.nan
    scale = 8.0
    if outcome_type == "hike_at_least":
        return _sigmoid((expected_change_bps - max(threshold_bps, 0.0)) / scale)
    if outcome_type == "cut_at_least":
        return _sigmoid((-expected_change_bps - max(threshold_bps, 0.0)) / scale)
    no_change = 1.0 - _sigmoid((abs(expected_change_bps) - 25.0) / scale)
    no_change = min(max(no_change, 0.02), 0.98)
    if outcome_type == "no_change":
        return no_change
    if outcome_type == "different":
        return 1.0 - no_change
    return math.nan


def _sigmoid(value: float) -> float:
    if value >= 50:
        return 1.0
    if value <= -50:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def _meeting_label(meeting: tuple[int, int] | None) -> str:
    if not meeting:
        return ""
    year, month = meeting
    return f"{year:04d}-{month:02d}"


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
