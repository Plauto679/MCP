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

from .maker_rewards_model import (
    HistoricalQuoteStats,
    estimate_maker_snapshot_economics,
    infer_category,
)
from .external_fair_value import fetch_yahoo_price_history_stats
from .fed_rates_model import fed_probability_details, fetch_fed_rates_context
from .market_ws_recorder import safe_float
from .wti_barrier_model import wti_probability_from_row


@dataclass(frozen=True)
class RewardFairValueEvaluatorConfig:
    maker_history_csv: Path
    external_history_csv: Path
    maker_paper_events_csv: Path
    output_dir: Path
    interval_seconds: float = 600.0
    quote_size_override: float = 0.0
    default_fill_rate: float = 0.02
    max_rows: int = 500
    max_snapshot_age_hours: float = 12.0
    fetch_wti_volatility: bool = True
    wti_annual_volatility_fallback: float = 0.35
    fetch_fed_rates_context: bool = True
    fed_current_effr_fallback: float = 3.625
    fed_manual_probabilities_csv: Path | None = None
    min_external_edge_abs: float = 0.08
    min_maker_fair_value_edge_abs: float = 0.03
    min_maker_pessimistic_ev_usd: float = 0.25
    min_maker_history_observations: int = 30
    max_maker_fill_rate: float = 0.25


def run_reward_fair_value_evaluator(config: RewardFairValueEvaluatorConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    run_ts = dt.datetime.now(dt.timezone.utc)
    maker_rows_all = latest_rows_by_market(_read_csv(config.maker_history_csv))
    external_rows_all = latest_rows_by_market(_read_csv(config.external_history_csv))
    maker_rows = _filter_recent_rows(maker_rows_all, run_ts, float(config.max_snapshot_age_hours))
    external_rows = _filter_recent_rows(external_rows_all, run_ts, float(config.max_snapshot_age_hours))
    wti_context = _load_wti_context(config)
    fed_context = _load_fed_context(config, [*maker_rows, *external_rows])
    history_stats = load_quote_history_stats(config.maker_paper_events_csv)

    maker_rankings = [
        evaluate_maker_row(
            row,
            external_rows,
            history_stats,
            config,
            wti_context,
            fed_context,
        )
        for row in maker_rows
    ]
    maker_rankings = [row for row in maker_rankings if row]
    maker_rankings.sort(key=lambda row: safe_float(row.get("combined_priority_score"), -math.inf), reverse=True)

    external_rankings = [
        evaluate_external_row(row, wti_context, fed_context)
        for row in external_rows
    ]
    external_rankings = [row for row in external_rankings if row]
    external_rankings.sort(key=lambda row: safe_float(row.get("external_priority_score"), -math.inf), reverse=True)

    combined = _combined_watchlist(maker_rankings, external_rankings)
    combined.sort(key=lambda row: safe_float(row.get("priority_score"), -math.inf), reverse=True)
    shadow_actions = build_shadow_actions(maker_rankings, external_rankings, config)
    timestamped_shadow_actions = [
        {"run_ts_utc": run_ts.isoformat(), **action}
        for action in shadow_actions
    ]

    maker_path = config.output_dir / "latest_maker_economics.csv"
    external_path = config.output_dir / "latest_external_fair_value.csv"
    combined_path = config.output_dir / "combined_watchlist.csv"
    shadow_actions_path = config.output_dir / "shadow_actions.csv"
    shadow_action_history_path = config.output_dir / "shadow_action_history.csv"
    report_path = config.output_dir / "report.json"
    _write_csv(maker_path, maker_rankings[: int(config.max_rows)], append=False)
    _write_csv(external_path, external_rankings[: int(config.max_rows)], append=False)
    _write_csv(combined_path, combined[: int(config.max_rows)], append=False)
    _write_csv(shadow_actions_path, timestamped_shadow_actions[: int(config.max_rows)], append=False)
    _write_csv(shadow_action_history_path, timestamped_shadow_actions[: int(config.max_rows)], append=True)

    report = {
        "run_ts_utc": run_ts.isoformat(),
        "mode": "dry_research_only",
        "maker_history_csv": str(config.maker_history_csv),
        "external_history_csv": str(config.external_history_csv),
        "maker_paper_events_csv": str(config.maker_paper_events_csv),
        "maker_markets_total": len(maker_rows_all),
        "external_markets_total": len(external_rows_all),
        "maker_markets": len(maker_rows),
        "external_markets": len(external_rows),
        "max_snapshot_age_hours": config.max_snapshot_age_hours,
        "wti_context": wti_context,
        "fed_context": fed_context,
        "history_stats_keys": len(history_stats),
        "top_maker": maker_rankings[:20],
        "top_external": external_rankings[:20],
        "top_combined": combined[:30],
        "top_shadow_actions": shadow_actions[:30],
        "caveats": [
            "This is a research evaluator, not a live trading strategy.",
            "Liquidity rewards are estimated from visible score proxies and market_competitiveness.",
            "Maker rebates are estimated from fee-equivalent formulas and do not include wallet-specific official payouts.",
            "External fair value probabilities are rough proxies except where a family-specific model is explicitly available.",
            "Fed rates probabilities use official/manual FedWatch inputs if supplied; otherwise they use a FRED/Yahoo futures proxy.",
        ],
        "outputs": {
            "maker_economics": str(maker_path),
            "external_fair_value": str(external_path),
            "combined_watchlist": str(combined_path),
            "shadow_actions": str(shadow_actions_path),
            "shadow_action_history": str(shadow_action_history_path),
            "report": str(report_path),
        },
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def build_shadow_actions(
    maker_rankings: list[dict[str, Any]],
    external_rankings: list[dict[str, Any]],
    config: RewardFairValueEvaluatorConfig,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in external_rankings:
        action = _external_shadow_action(row, config)
        if action:
            actions.append(action)
    for row in maker_rankings:
        action = _maker_shadow_action(row, config)
        if action:
            actions.append(action)
    actions.sort(key=lambda row: safe_float(row.get("action_priority_score"), -math.inf), reverse=True)
    return actions


def run_periodic_reward_fair_value_evaluator(
    config: RewardFairValueEvaluatorConfig,
    iterations: int,
    interval_seconds: float,
) -> None:
    for iteration in range(max(int(iterations), 1)):
        try:
            report = run_reward_fair_value_evaluator(config)
            print(json.dumps(report, indent=2), flush=True)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "reward_fair_value_evaluator_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                ),
                flush=True,
            )
        if iteration + 1 >= max(int(iterations), 1):
            break
        time.sleep(max(float(interval_seconds), 1.0))


def evaluate_maker_row(
    row: dict[str, Any],
    external_rows: list[dict[str, Any]],
    history_stats: dict[tuple[str, str, str], HistoricalQuoteStats],
    config: RewardFairValueEvaluatorConfig,
    wti_context: dict[str, Any] | None = None,
    fed_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_slug = str(row.get("market_slug") or "")
    condition_id = str(row.get("condition_id") or "")
    external = _match_external(row, external_rows)
    family = str(external.get("family") or _family_from_question(str(row.get("question") or "")))
    merged = {**row, "family": family}
    stats = _best_history_stats(row, history_stats)
    economics = estimate_maker_snapshot_economics(
        merged,
        interval_seconds=float(config.interval_seconds),
        history_stats=stats,
        quote_size_override=float(config.quote_size_override),
        default_fill_rate=float(config.default_fill_rate),
    )
    fair_value = evaluate_external_row(external, wti_context, fed_context) if external else {}
    fair_edge = safe_float(fair_value.get("fair_value_edge_proxy"), 0.0)
    net_base = safe_float(economics.get("net_ev_base_usd"), 0.0)
    risk_penalty = _risk_penalty(row, economics)
    combined_score = (
        net_base
        + safe_float(economics.get("net_ev_pessimistic_usd"), 0.0) * 0.50
        + fair_edge * 10.0
        - risk_penalty
    )

    return {
        "scan_ts_utc": str(row.get("scan_ts_utc") or ""),
        "condition_id": condition_id,
        "market_slug": market_slug,
        "question": str(row.get("question") or ""),
        "family": family,
        "candidate_ok": str(row.get("candidate_ok") or ""),
        "candidate_flags": str(row.get("candidate_flags") or ""),
        "daily_rate": row.get("daily_rate", ""),
        "volume_24hr": row.get("volume_24hr", ""),
        "hours_to_end": row.get("hours_to_end", ""),
        "avg_spread": row.get("avg_spread", ""),
        "mid_pair_deviation": row.get("mid_pair_deviation", ""),
        **economics,
        "external_market_match": bool(external),
        "external_family": str(external.get("family") or ""),
        "external_yes_mid": fair_value.get("yes_mid", ""),
        "external_probability_proxy": fair_value.get("fair_value_probability_proxy", ""),
        "fair_value_edge_proxy": fair_value.get("fair_value_edge_proxy", ""),
        "external_signal": fair_value.get("external_signal", ""),
        "fair_value_source": fair_value.get("fair_value_source", ""),
        "fed_outcome_type": fair_value.get("fed_outcome_type", ""),
        "fed_contract_symbol": fair_value.get("fed_contract_symbol", ""),
        "fed_futures_price": fair_value.get("fed_futures_price", ""),
        "fed_current_effr": fair_value.get("fed_current_effr", ""),
        "fed_expected_change_bps": fair_value.get("fed_expected_change_bps", ""),
        "risk_penalty": round(risk_penalty, 6),
        "combined_priority_score": round(combined_score, 6),
    }


def evaluate_external_row(
    row: dict[str, Any],
    wti_context: dict[str, Any] | None = None,
    fed_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not row:
        return {}
    family = str(row.get("family") or "other")
    modelability = int(safe_float(row.get("modelability_score"), 0.0))
    yes_mid = safe_float(row.get("yes_mid"), math.nan)
    hours_to_end = safe_float(row.get("hours_to_end"), math.nan)
    volume = safe_float(row.get("volume24hr"), 0.0)
    spread = safe_float(row.get("yes_spread"), math.nan)
    probability = fair_value_probability_proxy(row, wti_context, fed_context)
    edge = probability - yes_mid if math.isfinite(probability) and math.isfinite(yes_mid) else math.nan
    wti_sensitivity = _wti_sensitivity(row, yes_mid, wti_context)
    fed_details = fed_probability_details(row, fed_context) if family == "fed_rates" else None
    external_score = (
        modelability
        * (0.75 + math.log1p(max(volume, 0.0)) / 10.0)
        * (1.0 / (1.0 + max(spread if math.isfinite(spread) else 0.20, 0.0) * 8.0))
        * _family_weight(family)
    )
    if math.isfinite(edge):
        external_score *= 1.0 + min(abs(edge), 0.25) * 3.0
    if math.isfinite(hours_to_end):
        external_score *= min(max(hours_to_end / 12.0, 0.25), 1.50)

    return {
        "scan_ts_utc": str(row.get("scan_ts_utc") or ""),
        "condition_id": str(row.get("condition_id") or ""),
        "market_slug": str(row.get("market_slug") or ""),
        "question": str(row.get("question") or ""),
        "family": family,
        "modelability_score": modelability,
        "candidate_ok": str(row.get("candidate_ok") or ""),
        "candidate_flags": str(row.get("candidate_flags") or ""),
        "volume24hr": row.get("volume24hr", ""),
        "hours_to_end": row.get("hours_to_end", ""),
        "yes_mid": row.get("yes_mid", ""),
        "yes_spread": row.get("yes_spread", ""),
        "threshold_direction": row.get("threshold_direction", ""),
        "threshold": row.get("threshold", ""),
        "external_value": row.get("external_value", ""),
        "external_distance_to_threshold": row.get("external_distance_to_threshold", ""),
        "fair_value_probability_proxy": round(probability, 6) if math.isfinite(probability) else "",
        "fair_value_edge_proxy": round(edge, 6) if math.isfinite(edge) else "",
        "fair_value_source": fed_details.source if fed_details else ("wti_barrier_model" if family == "wti_daily" else ""),
        "fed_outcome_type": fed_details.outcome_type if fed_details else "",
        "fed_meeting_month": fed_details.meeting_month if fed_details else "",
        "fed_contract_symbol": fed_details.contract_symbol if fed_details else "",
        "fed_futures_price": _round_or_blank(fed_details.futures_price if fed_details else math.nan),
        "fed_current_effr": _round_or_blank(fed_details.current_effr if fed_details else math.nan),
        "fed_implied_monthly_rate": _round_or_blank(fed_details.implied_monthly_rate if fed_details else math.nan),
        "fed_expected_post_meeting_rate": _round_or_blank(fed_details.expected_post_meeting_rate if fed_details else math.nan),
        "fed_expected_change_bps": _round_or_blank(fed_details.expected_change_bps if fed_details else math.nan),
        "fed_probability_warning": fed_details.warning if fed_details else "",
        **wti_sensitivity,
        "external_signal": _external_signal(row, probability, edge),
        "external_priority_score": round(external_score, 6),
    }


def fair_value_probability_proxy(
    row: dict[str, Any],
    wti_context: dict[str, Any] | None = None,
    fed_context: dict[str, Any] | None = None,
) -> float:
    family = str(row.get("family") or "")
    if family == "fed_rates":
        return fed_probability_details(row, fed_context).probability
    if family != "wti_daily":
        return math.nan
    annual_volatility = safe_float(
        (wti_context or {}).get("annual_volatility"),
        math.nan,
    )
    if not math.isfinite(annual_volatility):
        annual_volatility = 0.35
    barrier_probability = wti_probability_from_row(row, annual_volatility=annual_volatility)
    if math.isfinite(barrier_probability):
        return barrier_probability
    distance = safe_float(row.get("external_distance_to_threshold"), math.nan)
    if not math.isfinite(distance):
        return math.nan
    hours = safe_float(row.get("hours_to_end"), 12.0)
    if not math.isfinite(hours) or hours <= 0:
        hours = 12.0
    scale = max(0.45, 1.15 * math.sqrt(hours / 24.0))
    return 1.0 / (1.0 + math.exp(-distance / scale))


def _load_wti_context(config: RewardFairValueEvaluatorConfig) -> dict[str, Any]:
    fallback = {
        "symbol": "CL=F",
        "source": "fallback",
        "annual_volatility": round(max(float(config.wti_annual_volatility_fallback), 0.01), 6),
    }
    if not bool(config.fetch_wti_volatility):
        return fallback
    try:
        context = fetch_yahoo_price_history_stats("CL=F", range_value="3mo", interval="1d")
    except Exception as exc:
        return {
            **fallback,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }
    annual_vol = safe_float(context.get("annual_volatility"), math.nan)
    if not math.isfinite(annual_vol) or annual_vol <= 0:
        context["annual_volatility"] = fallback["annual_volatility"]
        context["volatility_source"] = "fallback"
    else:
        context["volatility_source"] = "yahoo_3mo_realized"
    return context


def _load_fed_context(config: RewardFairValueEvaluatorConfig, rows: list[dict[str, Any]]) -> dict[str, Any]:
    fallback = {
        "source": "fallback_disabled_or_error",
        "current_effr": round(max(float(config.fed_current_effr_fallback), 0.0), 6),
        "current_effr_source": "fallback",
        "futures": {},
        "manual_probabilities": [],
    }
    if not bool(config.fetch_fed_rates_context):
        return fallback
    fed_rows = [
        row for row in rows
        if str(row.get("family") or _family_from_question(str(row.get("question") or ""))) == "fed_rates"
    ]
    if not fed_rows:
        return {**fallback, "source": "no_fed_rate_rows"}
    try:
        return fetch_fed_rates_context(
            fed_rows,
            manual_probabilities_csv=config.fed_manual_probabilities_csv,
            current_effr_fallback=float(config.fed_current_effr_fallback),
        )
    except Exception as exc:
        return {
            **fallback,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }


def _wti_sensitivity(
    row: dict[str, Any],
    yes_mid: float,
    wti_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if str(row.get("family") or "") != "wti_daily" or not math.isfinite(yes_mid):
        return {}
    base_vol = safe_float((wti_context or {}).get("annual_volatility"), 0.35)
    base_vol = max(base_vol, 0.01)
    vols = {
        "low": max(base_vol * 0.65, 0.20),
        "base": base_vol,
        "high": min(base_vol * 1.35, 1.20),
        "fallback": 0.35,
    }
    probabilities = {
        name: wti_probability_from_row(row, annual_volatility=vol)
        for name, vol in vols.items()
    }
    edges = {
        name: probabilities[name] - yes_mid
        for name in probabilities
        if math.isfinite(probabilities[name])
    }
    signs = {
        name: _sign(edge)
        for name, edge in edges.items()
        if abs(edge) >= 0.03
    }
    stable = bool(signs) and len(set(signs.values())) == 1
    return {
        "wti_vol_low": round(vols["low"], 6),
        "wti_vol_base": round(vols["base"], 6),
        "wti_vol_high": round(vols["high"], 6),
        "wti_probability_low_vol": _round_or_blank(probabilities.get("low")),
        "wti_probability_high_vol": _round_or_blank(probabilities.get("high")),
        "wti_probability_fallback_vol": _round_or_blank(probabilities.get("fallback")),
        "wti_edge_low_vol": _round_or_blank(edges.get("low")),
        "wti_edge_high_vol": _round_or_blank(edges.get("high")),
        "wti_edge_fallback_vol": _round_or_blank(edges.get("fallback")),
        "wti_signal_stable": stable,
    }


def latest_rows_by_market(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _market_key(row)
        if not key:
            continue
        row_ts = _parse_utc(row.get("scan_ts_utc"))
        if row_ts is None:
            continue
        current_ts = _parse_utc(latest.get(key, {}).get("scan_ts_utc"))
        if current_ts is None or row_ts > current_ts:
            latest[key] = row
    return list(latest.values())


def _filter_recent_rows(
    rows: list[dict[str, Any]],
    now: dt.datetime,
    max_age_hours: float,
) -> list[dict[str, Any]]:
    if max_age_hours <= 0:
        return rows
    recent: list[dict[str, Any]] = []
    for row in rows:
        row_ts = _parse_utc(row.get("scan_ts_utc"))
        if row_ts is None:
            continue
        age_hours = (now - row_ts).total_seconds() / 3600.0
        if age_hours <= max_age_hours:
            recent.append(row)
    return recent


def load_quote_history_stats(path: Path) -> dict[tuple[str, str, str], HistoricalQuoteStats]:
    rows = _read_csv(path)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    grouped_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    overall: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("market_slug") or ""),
            str(row.get("outcome") or ""),
            str(row.get("quote_side") or ""),
        )
        if key[0] and key[1] and key[2]:
            grouped[key].append(row)
        if key[0]:
            grouped_by_market[key[0]].append(row)
        overall.append(row)

    stats: dict[tuple[str, str, str], HistoricalQuoteStats] = {
        key: _history_stats(group)
        for key, group in grouped.items()
    }
    for market_slug, group in grouped_by_market.items():
        stats[(market_slug, "*", "*")] = _history_stats(group)
    if overall:
        stats[("*", "*", "*")] = _history_stats(overall)
    return stats


def _history_stats(rows: list[dict[str, Any]]) -> HistoricalQuoteStats:
    if not rows:
        return HistoricalQuoteStats()
    fills = [row for row in rows if str(row.get("filled_by_next_snapshot")).lower() == "true"]
    avg = 0.0
    if fills:
        avg = sum(safe_float(row.get("mark_to_mid_pnl_usd"), 0.0) for row in fills) / len(fills)
    return HistoricalQuoteStats(
        observations=len(rows),
        fill_rate=len(fills) / len(rows),
        avg_mark_pnl_per_filled_quote_usd=avg,
    )


def _best_history_stats(
    row: dict[str, Any],
    stats: dict[tuple[str, str, str], HistoricalQuoteStats],
) -> HistoricalQuoteStats:
    market_slug = str(row.get("market_slug") or "")
    return (
        stats.get((market_slug, "*", "*"))
        or stats.get(("*", "*", "*"))
        or HistoricalQuoteStats()
    )


def _combined_watchlist(
    maker_rankings: list[dict[str, Any]],
    external_rankings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for row in maker_rankings:
        combined.append(
            {
                "source": "maker_rewards",
                "priority_score": row.get("combined_priority_score", ""),
                "market_slug": row.get("market_slug", ""),
                "question": row.get("question", ""),
                "family": row.get("family", ""),
                "net_ev_base_usd": row.get("net_ev_base_usd", ""),
                "net_ev_pessimistic_usd": row.get("net_ev_pessimistic_usd", ""),
                "liquidity_reward_proxy_usd": row.get("liquidity_reward_proxy_usd", ""),
                "expected_adverse_selection_usd": row.get("expected_adverse_selection_usd", ""),
                "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
                "reason": _maker_reason(row),
            }
        )
    for row in external_rankings:
        combined.append(
            {
                "source": "external_fair_value",
                "priority_score": row.get("external_priority_score", ""),
                "market_slug": row.get("market_slug", ""),
                "question": row.get("question", ""),
                "family": row.get("family", ""),
                "net_ev_base_usd": "",
                "net_ev_pessimistic_usd": "",
                "liquidity_reward_proxy_usd": "",
                "expected_adverse_selection_usd": "",
                "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
                "reason": row.get("external_signal", ""),
            }
        )
    return combined


def _maker_reason(row: dict[str, Any]) -> str:
    if safe_float(row.get("net_ev_pessimistic_usd"), 0.0) > 0:
        return "positive_even_pessimistic_proxy"
    if safe_float(row.get("net_ev_base_usd"), 0.0) > 0:
        return "positive_base_proxy_needs_validation"
    if safe_float(row.get("liquidity_reward_proxy_usd"), 0.0) > abs(safe_float(row.get("expected_adverse_selection_usd"), 0.0)):
        return "reward_may_offset_adverse_selection"
    return "watch_only_negative_or_uncertain"


def _external_shadow_action(
    row: dict[str, Any],
    config: RewardFairValueEvaluatorConfig,
) -> dict[str, Any]:
    family = str(row.get("family") or "")
    edge = safe_float(row.get("fair_value_edge_proxy"), math.nan)
    score = safe_float(row.get("external_priority_score"), 0.0)
    if family == "wti_daily" and math.isfinite(edge) and abs(edge) >= float(config.min_external_edge_abs):
        if str(row.get("wti_signal_stable")).lower() not in {"true", "1", "yes"}:
            return {
                "action": "watch_wti_vol_sensitive",
                "action_family": "external_fair_value",
                "confidence": "low",
                "action_priority_score": round(score + abs(edge) * 5.0, 6),
                "market_slug": row.get("market_slug", ""),
                "question": row.get("question", ""),
                "family": family,
                "side": "VOL_SENSITIVITY",
                "yes_mid": row.get("yes_mid", ""),
                "fair_value_probability_proxy": row.get("fair_value_probability_proxy", ""),
                "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
                "net_ev_pessimistic_usd": "",
                "net_ev_base_usd": "",
                "history_observations": "",
                "history_fill_rate": "",
                "reason": "wti_signal_flips_or_weakens_across_volatility_scenarios",
            }
        action = "paper_directional_yes" if edge > 0 else "paper_directional_no"
        confidence = "high" if abs(edge) >= 0.15 else "medium"
        return {
            "action": action,
            "action_family": "external_fair_value",
            "confidence": confidence,
            "action_priority_score": round(score + abs(edge) * 20.0, 6),
            "market_slug": row.get("market_slug", ""),
            "question": row.get("question", ""),
            "family": family,
            "side": "YES" if edge > 0 else "NO",
            "yes_mid": row.get("yes_mid", ""),
            "fair_value_probability_proxy": row.get("fair_value_probability_proxy", ""),
            "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
            "net_ev_pessimistic_usd": "",
            "net_ev_base_usd": "",
            "history_observations": "",
            "history_fill_rate": "",
            "reason": row.get("external_signal", ""),
        }
    if family == "fed_rates":
        if math.isfinite(edge) and abs(edge) >= float(config.min_external_edge_abs):
            action = "paper_directional_yes" if edge > 0 else "paper_directional_no"
            confidence = "high" if abs(edge) >= 0.15 else "medium"
            return {
                "action": action,
                "action_family": "external_fair_value",
                "confidence": confidence,
                "action_priority_score": round(score + abs(edge) * 20.0, 6),
                "market_slug": row.get("market_slug", ""),
                "question": row.get("question", ""),
                "family": family,
                "side": "YES" if edge > 0 else "NO",
                "yes_mid": row.get("yes_mid", ""),
                "fair_value_probability_proxy": row.get("fair_value_probability_proxy", ""),
                "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
                "net_ev_pessimistic_usd": "",
                "net_ev_base_usd": "",
                "history_observations": "",
                "history_fill_rate": "",
                "reason": row.get("external_signal", ""),
            }
        return {
            "action": "watch_fed_rates",
            "action_family": "external_fair_value",
            "confidence": "low",
            "action_priority_score": round(score, 6),
            "market_slug": row.get("market_slug", ""),
            "question": row.get("question", ""),
            "family": family,
            "side": "WATCH_ONLY",
            "yes_mid": row.get("yes_mid", ""),
            "fair_value_probability_proxy": row.get("fair_value_probability_proxy", ""),
            "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
            "net_ev_pessimistic_usd": "",
            "net_ev_base_usd": "",
            "history_observations": "",
            "history_fill_rate": "",
            "reason": row.get("external_signal", ""),
        }
    return {}


def _maker_shadow_action(
    row: dict[str, Any],
    config: RewardFairValueEvaluatorConfig,
) -> dict[str, Any]:
    pessimistic = safe_float(row.get("net_ev_pessimistic_usd"), -math.inf)
    base = safe_float(row.get("net_ev_base_usd"), -math.inf)
    observations = int(safe_float(row.get("history_observations"), 0.0))
    fill_rate = safe_float(row.get("history_fill_rate"), math.inf)
    family = str(row.get("family") or "")
    fair_edge = safe_float(row.get("fair_value_edge_proxy"), math.nan)
    fair_value_side = ""
    if math.isfinite(fair_edge) and abs(fair_edge) >= float(config.min_maker_fair_value_edge_abs):
        fair_value_side = "YES_ONLY" if fair_edge > 0 else "NO_ONLY"
    elif family in {"fed_rates", "wti_daily", "macro_release"}:
        if base > 0 and observations >= int(config.min_maker_history_observations):
            return {
                "action": "watch_maker_reward",
                "action_family": "maker_rewards",
                "confidence": "low",
                "action_priority_score": round(safe_float(row.get("combined_priority_score"), 0.0) * 0.50, 6),
                "market_slug": row.get("market_slug", ""),
                "question": row.get("question", ""),
                "family": family,
                "side": "WATCH_ONLY",
                "yes_mid": row.get("external_yes_mid", ""),
                "fair_value_probability_proxy": row.get("external_probability_proxy", ""),
                "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
                "net_ev_pessimistic_usd": row.get("net_ev_pessimistic_usd", ""),
                "net_ev_base_usd": row.get("net_ev_base_usd", ""),
                "history_observations": observations,
                "history_fill_rate": row.get("history_fill_rate", ""),
                "reason": "maker_reward_needs_fair_value_edge_before_quote",
            }
        return {}
    if (
        pessimistic >= float(config.min_maker_pessimistic_ev_usd)
        and observations >= int(config.min_maker_history_observations)
        and fill_rate <= float(config.max_maker_fill_rate)
    ):
        return {
            "action": "paper_maker_quote",
            "action_family": "maker_rewards",
            "confidence": "medium",
            "action_priority_score": round(safe_float(row.get("combined_priority_score"), 0.0), 6),
            "market_slug": row.get("market_slug", ""),
            "question": row.get("question", ""),
            "family": family,
            "side": fair_value_side or "BOTH_SCOREABLE_SIDES",
            "yes_mid": row.get("external_yes_mid", ""),
            "fair_value_probability_proxy": row.get("external_probability_proxy", ""),
            "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
            "net_ev_pessimistic_usd": row.get("net_ev_pessimistic_usd", ""),
            "net_ev_base_usd": row.get("net_ev_base_usd", ""),
            "history_observations": observations,
            "history_fill_rate": row.get("history_fill_rate", ""),
            "reason": (
                "positive_pessimistic_reward_proxy_with_fair_value_edge"
                if fair_value_side
                else "positive_pessimistic_reward_proxy_with_history"
            ),
        }
    if base > 0 and observations >= int(config.min_maker_history_observations):
        return {
            "action": "watch_maker_reward",
            "action_family": "maker_rewards",
            "confidence": "low",
            "action_priority_score": round(safe_float(row.get("combined_priority_score"), 0.0) * 0.50, 6),
            "market_slug": row.get("market_slug", ""),
            "question": row.get("question", ""),
            "family": family,
            "side": "WATCH_ONLY",
            "yes_mid": row.get("external_yes_mid", ""),
            "fair_value_probability_proxy": row.get("external_probability_proxy", ""),
            "fair_value_edge_proxy": row.get("fair_value_edge_proxy", ""),
            "net_ev_pessimistic_usd": row.get("net_ev_pessimistic_usd", ""),
            "net_ev_base_usd": row.get("net_ev_base_usd", ""),
            "history_observations": observations,
            "history_fill_rate": row.get("history_fill_rate", ""),
            "reason": "positive_base_proxy_but_pessimistic_or_fill_filters_not_met",
        }
    return {}


def _external_signal(row: dict[str, Any], probability: float, edge: float) -> str:
    family = str(row.get("family") or "")
    if family == "wti_daily" and math.isfinite(probability) and math.isfinite(edge):
        if edge > 0.05:
            return "wti_yes_underpriced_proxy"
        if edge < -0.05:
            return "wti_yes_overpriced_proxy"
        return "wti_near_fair_proxy"
    if family == "fed_rates":
        if math.isfinite(probability) and math.isfinite(edge):
            if edge > 0.05:
                return "fed_yes_underpriced_proxy"
            if edge < -0.05:
                return "fed_yes_overpriced_proxy"
            return "fed_near_fair_proxy"
        return "fed_rates_missing_curve_proxy"
    if family == "macro_release":
        return "needs_external_curve_adapter"
    return "modelable_but_needs_family_adapter"


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _round_or_blank(value: Any) -> float | str:
    parsed = safe_float(value, math.nan)
    return round(parsed, 6) if math.isfinite(parsed) else ""


def _risk_penalty(row: dict[str, Any], economics: dict[str, Any]) -> float:
    penalty = 0.0
    penalty += max(safe_float(row.get("mid_pair_deviation"), 0.0) - 0.02, 0.0) * 10.0
    penalty += max(abs(safe_float(row.get("one_day_price_change"), 0.0)) - 0.10, 0.0) * 5.0
    penalty += max(-safe_float(economics.get("expected_adverse_selection_usd"), 0.0), 0.0) * 0.25
    if str(row.get("candidate_ok")).lower() not in {"true", "1", "yes"}:
        penalty += 1.0
    return penalty


def _match_external(row: dict[str, Any], external_rows: list[dict[str, Any]]) -> dict[str, Any]:
    condition_id = str(row.get("condition_id") or "")
    slug = str(row.get("market_slug") or "")
    for external in external_rows:
        if condition_id and condition_id == str(external.get("condition_id") or ""):
            return external
    for external in external_rows:
        if slug and slug == str(external.get("market_slug") or ""):
            return external
    return {}


def _family_from_question(question: str) -> str:
    return infer_category(question=question)


def _family_weight(family: str) -> float:
    return {
        "wti_daily": 1.30,
        "fed_rates": 1.25,
        "macro_release": 1.15,
        "weather": 1.05,
        "polling_politics": 0.90,
        "sports": 0.85,
        "crypto": 0.55,
    }.get(family, 0.35)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


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


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
