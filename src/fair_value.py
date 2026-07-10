from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), float(lower)), float(upper))


def sigmoid(value: float) -> float:
    if value >= 40:
        return 1.0
    if value <= -40:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def taker_fee_fraction(price: float, fee_rate: float = 0.07) -> float:
    p = clamp(price, 0.01, 0.99)
    return max(float(fee_rate), 0.0) * (1.0 - p)


def taker_fee_for_stake(stake_usd: float, price: float, fee_rate: float = 0.07) -> float:
    return round(max(float(stake_usd), 0.0) * taker_fee_fraction(price, fee_rate), 4)


def taker_breakeven_probability(price: float, fee_rate: float = 0.07) -> float:
    p = clamp(price, 0.01, 0.99)
    return p * (1.0 + taker_fee_fraction(p, fee_rate))


def taker_ev_per_usd(fair_probability: float, ask_price: float, fee_rate: float = 0.07) -> float:
    p = clamp(ask_price, 0.01, 0.99)
    fair = clamp(fair_probability, 0.0, 1.0)
    return (fair / p) - 1.0 - taker_fee_fraction(p, fee_rate)


def maker_ev_per_usd(fair_probability: float, bid_price: float) -> float:
    p = clamp(bid_price, 0.01, 0.99)
    fair = clamp(fair_probability, 0.0, 1.0)
    return (fair / p) - 1.0


_EMPIRICAL_CONTINUATION_BY_ELAPSED = {
    # Built from Binance BTCUSDT 1m data, 2024-01-01 through 2026-06-29.
    # Values are P(current side wins the 5m window | elapsed, abs delta bps).
    60.0: [
        (0.0, 0.5000),
        (1.0, 0.5429),
        (2.0, 0.5929),
        (4.0, 0.6462),
        (6.0, 0.7000),
        (8.0, 0.7302),
        (12.0, 0.7647),
        (16.0, 0.8009),
        (24.0, 0.8177),
        (40.0, 0.8604),
        (80.0, 0.9559),
    ],
    120.0: [
        (0.0, 0.5000),
        (1.0, 0.5573),
        (2.0, 0.6203),
        (4.0, 0.6885),
        (6.0, 0.7532),
        (8.0, 0.7940),
        (12.0, 0.8343),
        (16.0, 0.8762),
        (24.0, 0.8999),
        (40.0, 0.9318),
        (80.0, 0.9559),
    ],
    180.0: [
        (0.0, 0.5000),
        (1.0, 0.5759),
        (2.0, 0.6544),
        (4.0, 0.7412),
        (6.0, 0.8128),
        (8.0, 0.8638),
        (12.0, 0.8994),
        (16.0, 0.9303),
        (24.0, 0.9546),
        (40.0, 0.9745),
        (80.0, 0.9831),
    ],
    240.0: [
        (0.0, 0.5000),
        (1.0, 0.6265),
        (2.0, 0.7184),
        (4.0, 0.8169),
        (6.0, 0.8917),
        (8.0, 0.9302),
        (12.0, 0.9596),
        (16.0, 0.9755),
        (24.0, 0.9874),
        (40.0, 0.9927),
        (80.0, 0.9959),
    ],
}


def _interpolate_points(points: list[tuple[float, float]], x_value: float) -> float:
    x = max(float(x_value), 0.0)
    if x <= points[0][0]:
        return points[0][1]
    for index in range(1, len(points)):
        left_x, left_y = points[index - 1]
        right_x, right_y = points[index]
        if x <= right_x:
            span = max(right_x - left_x, 1e-9)
            weight = (x - left_x) / span
            return left_y + (right_y - left_y) * weight
    return points[-1][1]


def empirical_continuation_probability(elapsed_seconds: float, abs_delta_bps: float) -> float:
    elapsed = clamp(elapsed_seconds, 0.0, 300.0)
    anchors = sorted(_EMPIRICAL_CONTINUATION_BY_ELAPSED)
    if elapsed <= anchors[0]:
        return _interpolate_points(_EMPIRICAL_CONTINUATION_BY_ELAPSED[anchors[0]], abs_delta_bps)
    for index in range(1, len(anchors)):
        left = anchors[index - 1]
        right = anchors[index]
        if elapsed <= right:
            left_p = _interpolate_points(_EMPIRICAL_CONTINUATION_BY_ELAPSED[left], abs_delta_bps)
            right_p = _interpolate_points(_EMPIRICAL_CONTINUATION_BY_ELAPSED[right], abs_delta_bps)
            weight = (elapsed - left) / max(right - left, 1e-9)
            return left_p + (right_p - left_p) * weight
    return _interpolate_points(_EMPIRICAL_CONTINUATION_BY_ELAPSED[anchors[-1]], abs_delta_bps)


def estimate_fair_yes(
    opening_price: Optional[float],
    latest_price: Optional[float],
    elapsed_seconds: float,
    sensitivity_bps: float = 28.0,
) -> Dict[str, Optional[float]]:
    """Estimate the fair Yes probability from within-window BTC behavior."""
    if not opening_price or not latest_price or opening_price <= 0 or latest_price <= 0:
        return {
            "fair_yes": 0.5,
            "fair_no": 0.5,
            "delta_usd": None,
            "delta_bps": None,
            "confidence": 0.0,
            "continuation_probability": 0.5,
            "contrarian_probability": 0.5,
            "model_family": "empirical_continuation_v1",
        }

    elapsed = clamp(elapsed_seconds, 0.0, 300.0)
    delta = float(latest_price) - float(opening_price)
    delta_bps = (delta / float(opening_price)) * 10000.0
    abs_delta_bps = abs(delta_bps)
    continuation = empirical_continuation_probability(elapsed, abs_delta_bps)
    if abs_delta_bps < 0.05:
        continuation = 0.5

    if delta_bps > 0:
        fair_yes = continuation
    elif delta_bps < 0:
        fair_yes = 1.0 - continuation
    else:
        fair_yes = 0.5

    # Keep nonzero uncertainty: this is a historical conditional probability,
    # not a deterministic read of the window.
    fair_yes = clamp(fair_yes, 0.01, 0.99)
    confidence = clamp(abs(continuation - 0.5) * 2.0, 0.0, 1.0)
    return {
        "fair_yes": round(fair_yes, 6),
        "fair_no": round(1.0 - fair_yes, 6),
        "delta_usd": round(delta, 8),
        "delta_bps": round(delta_bps, 6),
        "confidence": round(confidence, 6),
        "continuation_probability": round(continuation, 6),
        "contrarian_probability": round(1.0 - continuation, 6),
        "model_family": "empirical_continuation_v1",
    }


def market_implied_yes_probability(yes_metrics: Dict, no_metrics: Dict) -> Optional[float]:
    yes_bid = _metric_float(yes_metrics, "best_bid")
    yes_ask = _metric_float(yes_metrics, "best_ask")
    no_bid = _metric_float(no_metrics, "best_bid")
    no_ask = _metric_float(no_metrics, "best_ask")

    yes_mid = (yes_bid + yes_ask) / 2.0 if yes_bid > 0 and yes_ask > 0 else 0.0
    no_mid = (no_bid + no_ask) / 2.0 if no_bid > 0 and no_ask > 0 else 0.0
    if yes_mid > 0 and no_mid > 0:
        return round(clamp(yes_mid / (yes_mid + no_mid), 0.01, 0.99), 6)
    if yes_mid > 0:
        return round(clamp(yes_mid, 0.01, 0.99), 6)
    if no_mid > 0:
        return round(clamp(1.0 - no_mid, 0.01, 0.99), 6)
    return None


def blend_fair_yes_with_market(
    fair_data: Dict[str, Optional[float]],
    yes_metrics: Dict,
    no_metrics: Dict,
) -> Dict[str, Optional[float]]:
    market_yes = market_implied_yes_probability(yes_metrics, no_metrics)
    raw_yes = clamp(float(fair_data.get("fair_yes") or 0.5), 0.01, 0.99)
    confidence = clamp(float(fair_data.get("confidence") or 0.0), 0.0, 1.0)
    result = dict(fair_data)
    result["raw_fair_yes"] = round(raw_yes, 6)
    result["raw_fair_no"] = round(1.0 - raw_yes, 6)
    result["market_fair_yes"] = market_yes if market_yes is not None else ""
    if market_yes is None:
        result["market_blend_weight"] = 0.0
        return result

    # Use the market as a sanity prior, but keep enough empirical weight to let
    # the model buy mispriced contrarian or continuation spots.
    if result.get("model_family") == "empirical_continuation_v1":
        abs_delta_bps = abs(float(result.get("delta_bps") or 0.0))
        raw_weight = clamp(0.45 + 0.35 * confidence, 0.45, 0.80)
        if abs_delta_bps <= 1.0:
            raw_weight = max(raw_weight, 0.55)
    else:
        raw_weight = clamp(0.12 + 0.38 * confidence, 0.12, 0.50)
    blended_yes = clamp((1.0 - raw_weight) * market_yes + raw_weight * raw_yes, 0.01, 0.99)
    result["fair_yes"] = round(blended_yes, 6)
    result["fair_no"] = round(1.0 - blended_yes, 6)
    result["market_blend_weight"] = round(1.0 - raw_weight, 6)
    result["confidence"] = round(confidence * raw_weight, 6)
    return result


@dataclass(frozen=True)
class FairValueDecision:
    should_enter: bool
    side: str = ""
    route: str = ""
    price: float = 0.0
    maker_price: float = 0.0
    fair_probability: float = 0.0
    edge_probability: float = 0.0
    ev_per_usd: float = 0.0
    fee_fraction: float = 0.0
    entry_model_win_probability: float = 0.0
    entry_model_min_probability: float = 0.0
    entry_model_enabled: bool = False
    cheap_reversal_model_probability: float = 0.0
    cheap_reversal_model_enabled: bool = False
    cheap_reversal_model_version: str = ""
    cheap_reversal_raw_probability: float = 0.0
    break_even_price: float = 0.0
    max_acceptable_price: float = 0.0
    price_margin: float = 0.0
    continuation_probability: float = 0.0
    abs_delta_bps: float = 0.0
    reason: str = "no_edge"


def _metric_float(metrics: Dict, key: str) -> float:
    try:
        return float(metrics.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def best_decision(
    fair_yes: float,
    yes_metrics: Dict,
    no_metrics: Dict,
    min_taker_edge: float = 0.04,
    min_maker_edge: float = 0.015,
    min_ev_per_usd: float = 0.0,
    min_price: float = 0.02,
    max_price: float = 0.98,
    fee_rate: float = 0.07,
    prefer_maker: bool = True,
    min_limit_shares: float = 5.0,
    stake_usd: float = 1.0,
    allow_taker: bool = True,
    allow_maker: bool = True,
    allowed_sides=None,
) -> FairValueDecision:
    fair_yes = clamp(fair_yes, 0.0, 1.0)
    if allowed_sides is not None:
        allowed_sides = {str(side) for side in allowed_sides}
    sides = (
        ("Yes", fair_yes, yes_metrics),
        ("No", 1.0 - fair_yes, no_metrics),
    )
    candidates: list[FairValueDecision] = []
    min_ev = max(float(min_ev_per_usd), 0.0)
    for side, fair, metrics in sides:
        if allowed_sides is not None and side not in allowed_sides:
            continue
        ask = _metric_float(metrics, "best_ask")
        bid = _metric_float(metrics, "best_bid")

        if allow_taker and min_price <= ask <= max_price:
            breakeven = taker_breakeven_probability(ask, fee_rate)
            edge_probability = fair - breakeven
            ev = taker_ev_per_usd(fair, ask, fee_rate)
            if edge_probability >= min_taker_edge and ev >= min_ev:
                candidates.append(
                    FairValueDecision(
                        should_enter=True,
                        side=side,
                        route="taker",
                        price=round(ask, 4),
                        fair_probability=round(fair, 6),
                        edge_probability=round(edge_probability, 6),
                        ev_per_usd=round(ev, 6),
                        fee_fraction=round(taker_fee_fraction(ask, fee_rate), 6),
                        reason="taker_edge",
                    )
                )

        if allow_maker and min_price <= bid <= max_price:
            shares = float(stake_usd) / max(bid, 0.01)
            if shares >= float(min_limit_shares):
                edge_probability = fair - bid
                ev = maker_ev_per_usd(fair, bid)
                if edge_probability >= min_maker_edge and ev >= min_ev:
                    candidates.append(
                        FairValueDecision(
                            should_enter=True,
                            side=side,
                            route="maker",
                            price=round(bid, 4),
                            maker_price=round(bid, 4),
                            fair_probability=round(fair, 6),
                            edge_probability=round(edge_probability, 6),
                            ev_per_usd=round(ev, 6),
                            fee_fraction=0.0,
                            reason="maker_edge",
                        )
                    )

    if not candidates:
        return FairValueDecision(should_enter=False)

    if prefer_maker:
        maker_candidates = [candidate for candidate in candidates if candidate.route == "maker"]
        if maker_candidates:
            return max(maker_candidates, key=lambda item: item.ev_per_usd)
    return max(candidates, key=lambda item: item.ev_per_usd)
