from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .market_ws_recorder import safe_float


@dataclass(frozen=True)
class WtiProbabilityInput:
    spot: float
    threshold: float
    direction: str
    hours_to_end: float
    annual_volatility: float
    contract_type: str = "auto"


def infer_wti_contract_type(question: str) -> str:
    text = str(question or "").lower()
    if any(token in text for token in ("hit", "reach", "reaches", "high", "low", "dip", "drop")):
        return "touch"
    if any(token in text for token in ("close", "closes", "closing", "settle", "settles")):
        return "terminal"
    return "touch"


def wti_probability_from_row(row: dict[str, Any], annual_volatility: float = 0.35) -> float:
    spot = safe_float(row.get("external_value"), math.nan)
    threshold = safe_float(row.get("threshold"), math.nan)
    direction = str(row.get("threshold_direction") or "")
    hours_to_end = safe_float(row.get("hours_to_end"), math.nan)
    contract_type = infer_wti_contract_type(str(row.get("question") or ""))
    if not math.isfinite(hours_to_end):
        hours_to_end = 24.0 * 7.0
    return wti_threshold_probability(
        WtiProbabilityInput(
            spot=spot,
            threshold=threshold,
            direction=direction,
            hours_to_end=hours_to_end,
            annual_volatility=annual_volatility,
            contract_type=contract_type,
        )
    )


def wti_threshold_probability(value: WtiProbabilityInput) -> float:
    spot = float(value.spot)
    threshold = float(value.threshold)
    direction = str(value.direction or "").lower()
    hours_to_end = max(float(value.hours_to_end), 0.0)
    annual_volatility = max(float(value.annual_volatility), 0.01)
    contract_type = str(value.contract_type or "touch").lower()

    if not all(math.isfinite(item) for item in (spot, threshold, hours_to_end, annual_volatility)):
        return math.nan
    if spot <= 0 or threshold <= 0 or direction not in {"above", "below"}:
        return math.nan
    if direction == "above" and spot >= threshold:
        return 1.0
    if direction == "below" and spot <= threshold:
        return 1.0
    if hours_to_end <= 0:
        return 0.0

    sigma_t = annual_volatility * math.sqrt(hours_to_end / (365.0 * 24.0))
    if sigma_t <= 0:
        return 0.0

    distance = math.log(threshold / spot)
    if direction == "below":
        distance = math.log(spot / threshold)
    z = distance / sigma_t

    if contract_type == "terminal":
        if direction == "above":
            return _clamp01(1.0 - _normal_cdf(z))
        return _clamp01(_normal_cdf(-z))

    # Reflection-principle approximation for driftless log-price first passage.
    return _clamp01(2.0 * (1.0 - _normal_cdf(z)))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return math.nan
    return min(max(value, 0.0), 1.0)
