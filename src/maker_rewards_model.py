from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .market_ws_recorder import safe_float


FEE_RATE_BY_CATEGORY: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
}

MAKER_REBATE_FRACTION_BY_CATEGORY: dict[str, float] = {
    "crypto": 0.20,
    "sports": 0.25,
    "finance": 0.25,
    "politics": 0.25,
    "economics": 0.25,
    "culture": 0.25,
    "weather": 0.25,
    "other": 0.25,
    "mentions": 0.25,
    "tech": 0.25,
    "geopolitics": 0.0,
}


@dataclass(frozen=True)
class HistoricalQuoteStats:
    observations: int = 0
    fill_rate: float = 0.0
    avg_mark_pnl_per_filled_quote_usd: float = 0.0


@dataclass(frozen=True)
class MakerEconomicsScenario:
    name: str
    liquidity_reward_capture: float
    rebate_capture: float
    adverse_selection_multiplier: float


DEFAULT_SCENARIOS: tuple[MakerEconomicsScenario, ...] = (
    MakerEconomicsScenario(
        name="pessimistic",
        liquidity_reward_capture=0.25,
        rebate_capture=0.0,
        adverse_selection_multiplier=1.25,
    ),
    MakerEconomicsScenario(
        name="base",
        liquidity_reward_capture=0.50,
        rebate_capture=0.50,
        adverse_selection_multiplier=1.0,
    ),
    MakerEconomicsScenario(
        name="optimistic",
        liquidity_reward_capture=1.0,
        rebate_capture=1.0,
        adverse_selection_multiplier=0.75,
    ),
)


def normalize_category(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    aliases = {
        "other / general": "other",
        "general": "other",
        "macro": "economics",
        "macro release": "economics",
        "fed rates": "economics",
        "fed": "economics",
        "rates": "economics",
        "polling politics": "politics",
        "polling": "politics",
        "wti daily": "finance",
        "wti": "finance",
        "oil": "finance",
    }
    if text in aliases:
        return aliases[text]
    compact = text.replace(" ", "_")
    if compact in FEE_RATE_BY_CATEGORY:
        return compact
    if text in FEE_RATE_BY_CATEGORY:
        return text
    return "other"


def infer_category(*, family: str = "", question: str = "", raw_category: str = "") -> str:
    if raw_category:
        return normalize_category(raw_category)
    family_category = normalize_category(family)
    if family_category != "other":
        return family_category

    text = f"{family} {question}".lower()
    if any(token in text for token in ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana")):
        return "crypto"
    if any(token in text for token in ("nfl", "nba", "mlb", "nhl", "ufc", "soccer", "football", "tennis", "f1")):
        return "sports"
    if any(token in text for token in ("election", "president", "senate", "congress", "mayor", "governor")):
        return "politics"
    if any(token in text for token in ("fed", "fomc", "cpi", "inflation", "unemployment", "rate cut", "rate hike")):
        return "economics"
    if any(token in text for token in ("wti", "oil", "crude", "stock", "nasdaq", "s&p", "dow")):
        return "finance"
    if any(token in text for token in ("weather", "temperature", "rain", "snow", "hurricane")):
        return "weather"
    return "other"


def fee_rate_for_category(category: str) -> float:
    return FEE_RATE_BY_CATEGORY.get(normalize_category(category), FEE_RATE_BY_CATEGORY["other"])


def maker_rebate_fraction_for_category(category: str) -> float:
    return MAKER_REBATE_FRACTION_BY_CATEGORY.get(
        normalize_category(category),
        MAKER_REBATE_FRACTION_BY_CATEGORY["other"],
    )


def order_position_score(
    *,
    max_spread: float,
    distance_from_mid: float,
    shares: float,
    min_size: float,
    boost: float = 1.0,
) -> float:
    if max_spread <= 0 or shares < min_size or distance_from_mid > max_spread:
        return 0.0
    return ((max_spread - max(distance_from_mid, 0.0)) / max_spread) ** 2 * shares * max(boost, 0.0)


def paired_side_scores(
    *,
    yes_bid_score: float,
    yes_ask_score: float,
    no_bid_score: float,
    no_ask_score: float,
) -> dict[str, float]:
    return {
        "long_yes_score": max(yes_bid_score, 0.0) + max(no_ask_score, 0.0),
        "long_no_score": max(yes_ask_score, 0.0) + max(no_bid_score, 0.0),
    }


def liquidity_reward_score(
    *,
    yes_bid_score: float,
    yes_ask_score: float,
    no_bid_score: float,
    no_ask_score: float,
    market_midpoint: float,
    single_sided_scaling: float = 3.0,
) -> float:
    paired = paired_side_scores(
        yes_bid_score=yes_bid_score,
        yes_ask_score=yes_ask_score,
        no_bid_score=no_bid_score,
        no_ask_score=no_ask_score,
    )
    q_one = paired["long_yes_score"]
    q_two = paired["long_no_score"]
    if q_one <= 0 and q_two <= 0:
        return 0.0
    if 0.10 <= market_midpoint <= 0.90:
        scaling = max(float(single_sided_scaling), 1.0)
        return max(min(q_one, q_two), max(q_one / scaling, q_two / scaling))
    return min(q_one, q_two)


def liquidity_reward_share(our_score: float, competition_score: float) -> float:
    total = max(our_score, 0.0) + max(competition_score, 0.0)
    if total <= 0:
        return 0.0
    return max(our_score, 0.0) / total


def liquidity_reward_usd(
    *,
    daily_rate: float,
    seconds: float,
    our_score: float,
    competition_score: float,
) -> float:
    if daily_rate <= 0 or seconds <= 0 or our_score <= 0:
        return 0.0
    pool = daily_rate * seconds / 86400.0
    return pool * liquidity_reward_share(our_score, competition_score)


def fee_equivalent_usd(*, shares: float, price: float, category: str) -> float:
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    fee = shares * fee_rate_for_category(category) * price * (1.0 - price)
    return round(fee, 5) if fee >= 0.00001 else 0.0


def maker_rebate_proxy_usd(*, shares: float, price: float, category: str) -> float:
    return round(
        fee_equivalent_usd(shares=shares, price=price, category=category)
        * maker_rebate_fraction_for_category(category),
        6,
    )


def estimate_maker_snapshot_economics(
    row: dict[str, Any],
    *,
    interval_seconds: float,
    history_stats: HistoricalQuoteStats | None = None,
    quote_size_override: float = 0.0,
    default_fill_rate: float = 0.0,
    raw_category: str = "",
) -> dict[str, Any]:
    quote_size = safe_float(row.get("quote_size"), 0.0)
    if quote_size_override > 0:
        quote_size = quote_size_override
    category = infer_category(
        family=str(row.get("family") or ""),
        question=str(row.get("question") or ""),
        raw_category=raw_category,
    )
    market_midpoint = _market_midpoint(row)
    reward_score = liquidity_reward_score(
        yes_bid_score=safe_float(row.get("yes_bid_reward_score"), 0.0),
        yes_ask_score=safe_float(row.get("yes_ask_reward_score"), 0.0),
        no_bid_score=safe_float(row.get("no_bid_reward_score"), 0.0),
        no_ask_score=safe_float(row.get("no_ask_reward_score"), 0.0),
        market_midpoint=market_midpoint,
    )
    competition_score = max(safe_float(row.get("market_competitiveness"), 0.0), 0.0)
    daily_rate = safe_float(row.get("daily_rate"), 0.0)
    liquidity_reward = liquidity_reward_usd(
        daily_rate=daily_rate,
        seconds=interval_seconds,
        our_score=reward_score,
        competition_score=competition_score,
    )

    quote_prices = _quote_prices(row)
    rebate_if_all_filled = sum(
        maker_rebate_proxy_usd(shares=quote_size, price=price, category=category)
        for price in quote_prices
    )
    stats = history_stats or HistoricalQuoteStats(fill_rate=default_fill_rate)
    fill_rate = max(stats.fill_rate, default_fill_rate)
    expected_rebate = rebate_if_all_filled * min(max(fill_rate, 0.0), 1.0)
    quote_count = len(quote_prices)
    adverse_selection = quote_count * fill_rate * min(stats.avg_mark_pnl_per_filled_quote_usd, 0.0)

    scenario_values: dict[str, float] = {}
    for scenario in DEFAULT_SCENARIOS:
        scenario_values[scenario.name] = (
            liquidity_reward * scenario.liquidity_reward_capture
            + expected_rebate * scenario.rebate_capture
            + adverse_selection * scenario.adverse_selection_multiplier
        )

    capital = _capital_proxy_usd(row, quote_size)
    return {
        "fee_category": category,
        "fee_rate": round(fee_rate_for_category(category), 6),
        "maker_rebate_fraction": round(maker_rebate_fraction_for_category(category), 6),
        "quote_count": quote_count,
        "quote_size": round(quote_size, 6),
        "capital_proxy_usd": round(capital, 6),
        "our_liquidity_score": round(reward_score, 6),
        "competition_score": round(competition_score, 6),
        "liquidity_share_proxy": round(liquidity_reward_share(reward_score, competition_score), 6),
        "liquidity_reward_proxy_usd": round(liquidity_reward, 6),
        "rebate_if_all_filled_usd": round(rebate_if_all_filled, 6),
        "history_observations": int(stats.observations),
        "history_fill_rate": round(fill_rate, 6),
        "history_avg_mark_pnl_per_fill_usd": round(stats.avg_mark_pnl_per_filled_quote_usd, 6),
        "expected_rebate_proxy_usd": round(expected_rebate, 6),
        "expected_adverse_selection_usd": round(adverse_selection, 6),
        "net_ev_pessimistic_usd": round(scenario_values["pessimistic"], 6),
        "net_ev_base_usd": round(scenario_values["base"], 6),
        "net_ev_optimistic_usd": round(scenario_values["optimistic"], 6),
        "net_ev_base_per_capital": round(scenario_values["base"] / capital, 8) if capital > 0 else "",
    }


def _market_midpoint(row: dict[str, Any]) -> float:
    yes_mid = safe_float(row.get("yes_mid"), math.nan)
    no_mid = safe_float(row.get("no_mid"), math.nan)
    if math.isfinite(yes_mid) and 0 < yes_mid < 1:
        return yes_mid
    if math.isfinite(no_mid) and 0 < no_mid < 1:
        return 1.0 - no_mid
    return 0.5


def _quote_prices(row: dict[str, Any]) -> list[float]:
    prices: list[float] = []
    for field in ("yes_bid_quote", "yes_ask_quote", "no_bid_quote", "no_ask_quote"):
        price = safe_float(row.get(field), math.nan)
        if math.isfinite(price) and 0 < price < 1:
            prices.append(price)
    return prices


def _capital_proxy_usd(row: dict[str, Any], quote_size: float) -> float:
    yes_bid = safe_float(row.get("yes_bid_quote"), math.nan)
    no_bid = safe_float(row.get("no_bid_quote"), math.nan)
    bid_capital = sum(price * quote_size for price in (yes_bid, no_bid) if math.isfinite(price) and price > 0)
    ask_inventory = quote_size * sum(
        1.0 - price
        for price in (
            safe_float(row.get("yes_ask_quote"), math.nan),
            safe_float(row.get("no_ask_quote"), math.nan),
        )
        if math.isfinite(price) and 0 < price < 1
    )
    return bid_capital + ask_inventory
