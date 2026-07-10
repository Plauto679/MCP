from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .market_ws_recorder import safe_float


OPEN = "OPEN"
QUOTE_CANCELED_BY_FILTER = "QUOTE_CANCELED_BY_FILTER"
FILLED = "FILLED_BY_NEXT_SNAPSHOT"
NOT_FILLED = "NOT_FILLED_BY_NEXT_SNAPSHOT"
POSITION_OPEN = "POSITION_OPEN"
POSITION_CLOSED_TAKE_PROFIT = "POSITION_CLOSED_TAKE_PROFIT"
POSITION_CLOSED_STOP_LOSS = "POSITION_CLOSED_STOP_LOSS"
POSITION_CLOSED_MAX_HOLD = "POSITION_CLOSED_MAX_HOLD"
POSITION_CLOSED_SIGNAL_LOSS = "POSITION_CLOSED_SIGNAL_LOSS"
POSITION_CLOSED_STALE = "POSITION_CLOSED_STALE"

POSITION_CLOSED_STATUSES = {
    POSITION_CLOSED_TAKE_PROFIT,
    POSITION_CLOSED_STOP_LOSS,
    POSITION_CLOSED_MAX_HOLD,
    POSITION_CLOSED_SIGNAL_LOSS,
    POSITION_CLOSED_STALE,
}

DEFAULT_ALLOWED_NEW_ORDER_FAMILIES = (
    "fed_rates",
    "wti_daily",
    "macro_release",
)

DEFAULT_BLOCKED_NEW_ORDER_TEXT_TERMS = (
    "dota",
    "dota2",
    "league of legends",
    "counter-strike",
    "cs2",
    "valorant",
    "esports",
    "e-sports",
    "gamerlegion",
    "team falcons",
    "betboom",
    "team to win",
    "to win 2-0",
    "match result",
    "map winner",
    "ufc",
    "fifwc",
    "formula 1",
)


@dataclass(frozen=True)
class ShadowPaperTraderConfig:
    shadow_actions_csv: Path
    maker_history_csv: Path
    output_dir: Path
    quote_size_override: float = 0.0
    directional_size_override: float = 50.0
    directional_entry_mode: str = "maker"
    min_reward_score: float = 0.0
    include_asks: bool = False
    max_new_markets_per_cycle: int = 20
    active_exit_enabled: bool = True
    take_profit_per_share: float = 0.03
    stop_loss_per_share: float = 0.05
    max_position_cycles: int = 72
    close_on_signal_loss: bool = False
    allowed_new_order_families: tuple[str, ...] | str = DEFAULT_ALLOWED_NEW_ORDER_FAMILIES
    blocked_new_order_text_terms: tuple[str, ...] | str = DEFAULT_BLOCKED_NEW_ORDER_TEXT_TERMS
    min_new_order_hours_to_end: float = 6.0
    require_new_order_hours_to_end: bool = True
    new_order_cooldown_hours: float = 12.0
    max_new_directional_markets_per_cycle: int = 3
    cancel_open_quotes_failing_filter: bool = True
    cancel_stale_open_quotes: bool = True
    max_open_quote_wall_hours: float = 2.0
    close_stale_positions: bool = True
    max_position_without_update_hours: float = 24.0
    stale_end_grace_hours: float = 2.0


def run_shadow_paper_trader(config: ShadowPaperTraderConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    run_ts = utc_iso()
    orders_path = config.output_dir / "paper_orders.csv"
    events_path = config.output_dir / "paper_order_events.csv"
    report_path = config.output_dir / "report.json"

    orders = _read_csv(orders_path)
    history_rows = _read_csv(config.maker_history_csv)
    history_by_market = _history_by_market(history_rows)
    latest_rows = {
        market: rows[-1]
        for market, rows in history_by_market.items()
        if rows
    }
    actions = _read_csv(config.shadow_actions_csv)
    candidate_actions = [
        row for row in actions
        if str(row.get("action") or "") in {"paper_maker_quote", "paper_directional_yes", "paper_directional_no"}
    ]
    selected_actions, new_order_filter = _select_new_order_actions(
        candidate_actions,
        latest_rows,
        config,
        run_ts,
    )
    selected_action_markets = {str(row.get("market_slug") or "") for row in selected_actions}

    settled_orders = 0
    canceled_open_quotes = 0
    positions_closed_this_cycle = 0
    stale_positions_closed_this_cycle = 0
    for order in orders:
        _migrate_legacy_filled_order(order)
        if str(order.get("status") or "") != OPEN:
            continue
        if bool(config.cancel_open_quotes_failing_filter):
            cancel_reason = _open_quote_filter_cancel_reason(order, latest_rows, config, run_ts)
            if cancel_reason:
                _cancel_open_quote(order, cancel_reason, run_ts)
                canceled_open_quotes += 1
                continue
        next_row = _next_snapshot(history_by_market.get(str(order.get("market_slug") or ""), []), order)
        if not next_row:
            if bool(config.cancel_stale_open_quotes):
                stale_reason = _stale_open_quote_reason(order, latest_rows, config, run_ts)
                if stale_reason:
                    _cancel_open_quote(order, stale_reason, run_ts)
                    canceled_open_quotes += 1
            continue
        _settle_order(order, next_row)
        settled_orders += 1

    if bool(config.active_exit_enabled):
        for order in orders:
            if str(order.get("status") or "") != POSITION_OPEN:
                continue
            rows = history_by_market.get(str(order.get("market_slug") or ""), [])
            if _manage_position(order, rows, selected_action_markets, config):
                positions_closed_this_cycle += 1
            elif bool(config.close_stale_positions):
                stale_reason = _stale_position_reason(order, latest_rows, config, run_ts)
                if stale_reason and _close_stale_position(order, latest_rows, stale_reason, run_ts):
                    positions_closed_this_cycle += 1
                    stale_positions_closed_this_cycle += 1

    quote_sides = ("bid", "ask") if bool(config.include_asks) else ("bid",)
    open_keys = {
        _open_position_key(order)
        for order in orders
        if str(order.get("status") or "") in {OPEN, POSITION_OPEN}
    }
    cooldown_keys = _cooldown_keys(orders, run_ts, float(config.new_order_cooldown_hours))
    existing_ids = {str(order.get("order_id") or "") for order in orders}
    new_orders: list[dict[str, Any]] = []
    new_directional_markets = 0

    for action in selected_actions:
        market_slug = str(action.get("market_slug") or "")
        snapshot = latest_rows.get(market_slug)
        if not snapshot:
            continue
        if str(action.get("action") or "") in {"paper_directional_yes", "paper_directional_no"}:
            if new_directional_markets >= int(config.max_new_directional_markets_per_cycle):
                continue
            outcome = _directional_outcome(action)
            if not outcome:
                continue
            if str(config.directional_entry_mode or "maker").strip().lower() == "taker":
                order = _build_directional_position(
                    run_ts=run_ts,
                    action=action,
                    snapshot=snapshot,
                    directional_size_override=float(config.directional_size_override),
                )
            else:
                order = _build_order(
                    run_ts=run_ts,
                    action=action,
                    snapshot=snapshot,
                    outcome=outcome,
                    quote_side="bid",
                    quote_size_override=float(config.directional_size_override),
                    min_reward_score=0.0,
                )
            if order:
                order_id = str(order["order_id"])
                cooldown_key = _cooldown_key(order)
                if (
                    order_id not in existing_ids
                    and _open_position_key(order) not in open_keys
                    and cooldown_key not in cooldown_keys
                ):
                    existing_ids.add(order_id)
                    open_keys.add(_open_position_key(order))
                    cooldown_keys.add(cooldown_key)
                    new_orders.append(order)
                    new_directional_markets += 1
            continue

        for outcome in ("yes", "no"):
            if outcome not in _outcomes_for_action(action):
                continue
            for quote_side in quote_sides:
                order = _build_order(
                    run_ts=run_ts,
                    action=action,
                    snapshot=snapshot,
                    outcome=outcome,
                    quote_side=quote_side,
                    quote_size_override=float(config.quote_size_override),
                    min_reward_score=float(config.min_reward_score),
                )
                if not order:
                    continue
                order_id = str(order["order_id"])
                cooldown_key = _cooldown_key(order)
                if order_id in existing_ids or _open_position_key(order) in open_keys or cooldown_key in cooldown_keys:
                    continue
                existing_ids.add(order_id)
                open_keys.add(_open_position_key(order))
                cooldown_keys.add(cooldown_key)
                new_orders.append(order)

    orders.extend(new_orders)
    _write_csv(orders_path, orders, append=False)

    event_rows = [row for row in orders if str(row.get("status") or "") != OPEN]
    _write_csv(events_path, event_rows, append=False)
    position_events_path = config.output_dir / "paper_position_events.csv"
    position_event_rows = [row for row in orders if str(row.get("status") or "") in POSITION_CLOSED_STATUSES]
    _write_csv(position_events_path, position_event_rows, append=False)

    overall = _summary_row({}, orders)
    by_market = _summaries(orders, ["market_slug", "question"])
    by_outcome_side = _summaries(orders, ["outcome", "quote_side"])
    active_exit = _active_exit_summary(orders)
    report = {
        "run_ts_utc": run_ts,
        "mode": "dry_research_only",
        "shadow_actions_csv": str(config.shadow_actions_csv),
        "maker_history_csv": str(config.maker_history_csv),
        "orders_total": len(orders),
        "new_orders_opened": len(new_orders),
        "orders_settled_this_cycle": settled_orders,
        "open_quotes_canceled_by_filter_this_cycle": canceled_open_quotes,
        "positions_closed_this_cycle": positions_closed_this_cycle,
        "stale_positions_closed_this_cycle": stale_positions_closed_this_cycle,
        "new_order_filter": new_order_filter,
        "overall": overall,
        "active_exit": active_exit,
        "top_markets_conservative_net": sorted(
            by_market,
            key=lambda row: safe_float(row.get("net_pnl_conservative_reward_usd"), -math.inf),
            reverse=True,
        )[:20],
        "worst_markets_mark_to_mid": sorted(
            by_market,
            key=lambda row: safe_float(row.get("mark_to_mid_pnl_usd"), math.inf),
        )[:20],
        "by_outcome_side": by_outcome_side,
        "caveats": [
            "This ledger simulates selected paper maker and directional actions from the latest evaluator output.",
            "paper_directional_yes/no actions default to maker-style bids on the model-favored outcome; taker mode is optional.",
            "Quotes are held for one scanner cycle, then marked as filled or not filled using the next available snapshot.",
            "PnL is mark-to-mid after the next snapshot plus reward proxies; it is not final event-resolution PnL.",
            "Default mode is bid-only YES/NO to avoid pretending we can place ask-side quotes without inventory.",
            "Filled bid quotes are also tracked as paper inventory with take-profit, stop-loss, and max-hold exits.",
            "Active-exit realized PnL uses immediate taker-style exit prices from the visible book, not guaranteed future fills.",
            "New paper entries are filtered to avoid fast sports/esports and unmodelled families while the external fair-value adapters mature.",
            "Stale open quotes/positions are cancelled or closed in the dry ledger to avoid reporting phantom exposure.",
            "Reward values are proxies, not official wallet-specific Polymarket payouts.",
        ],
        "outputs": {
            "orders": str(orders_path),
            "events": str(events_path),
            "position_events": str(position_events_path),
            "report": str(report_path),
        },
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _select_new_order_actions(
    candidate_actions: list[dict[str, Any]],
    latest_rows: dict[str, dict[str, Any]],
    config: ShadowPaperTraderConfig,
    run_ts: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    max_selected = max(int(config.max_new_markets_per_cycle), 0)
    allowed_families = set(_csv_tuple(config.allowed_new_order_families))
    blocked_terms = _csv_tuple(config.blocked_new_order_text_terms)
    skipped_by_reason: dict[str, int] = defaultdict(int)
    skipped_examples: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    considered = 0

    if max_selected <= 0:
        return [], {
            "enabled": True,
            "candidate_actions_total": len(candidate_actions),
            "actions_considered": 0,
            "selected_actions": 0,
            "allowed_families": sorted(allowed_families),
            "blocked_text_terms": list(blocked_terms),
            "min_hours_to_end": float(config.min_new_order_hours_to_end),
            "require_hours_to_end": bool(config.require_new_order_hours_to_end),
            "cooldown_hours": float(config.new_order_cooldown_hours),
            "skipped_by_reason": {"max_new_markets_per_cycle_zero": len(candidate_actions)},
            "skipped_examples": [],
        }

    for action in candidate_actions:
        considered += 1
        market_slug = str(action.get("market_slug") or "")
        snapshot = latest_rows.get(market_slug)
        reason = "missing_latest_snapshot"
        if snapshot:
            reason = _new_order_skip_reason(
                action=action,
                snapshot=snapshot,
                run_ts=run_ts,
                allowed_families=allowed_families,
                blocked_terms=blocked_terms,
                min_hours_to_end=float(config.min_new_order_hours_to_end),
                require_hours_to_end=bool(config.require_new_order_hours_to_end),
            )

        if reason:
            skipped_by_reason[reason] += 1
            if len(skipped_examples) < 12:
                question = str(action.get("question") or "")
                if not question and snapshot:
                    question = str(snapshot.get("question") or "")
                skipped_examples.append(
                    {
                        "reason": reason,
                        "market_slug": market_slug,
                        "family": str(action.get("family") or ""),
                        "question": question,
                    }
                )
            continue

        selected.append(action)
        if len(selected) >= max_selected:
            break

    return selected, {
        "enabled": True,
        "candidate_actions_total": len(candidate_actions),
        "actions_considered": considered,
        "selected_actions": len(selected),
        "allowed_families": sorted(allowed_families),
        "blocked_text_terms": list(blocked_terms),
        "min_hours_to_end": float(config.min_new_order_hours_to_end),
        "require_hours_to_end": bool(config.require_new_order_hours_to_end),
        "cooldown_hours": float(config.new_order_cooldown_hours),
        "skipped_by_reason": dict(sorted(skipped_by_reason.items())),
        "skipped_examples": skipped_examples,
    }


def _new_order_skip_reason(
    *,
    action: dict[str, Any],
    snapshot: dict[str, Any],
    run_ts: str,
    allowed_families: set[str],
    blocked_terms: tuple[str, ...],
    min_hours_to_end: float,
    require_hours_to_end: bool,
) -> str:
    family = str(action.get("family") or "").strip().lower()
    text = " ".join(
        str(value or "")
        for value in (
            family,
            action.get("market_slug"),
            action.get("question"),
            snapshot.get("market_slug"),
            snapshot.get("question"),
            snapshot.get("event_slug"),
            snapshot.get("group_item_title"),
        )
    ).lower()
    if any(term in text for term in blocked_terms):
        return "blocked_fast_market_text"
    if allowed_families and family not in allowed_families:
        return "family_not_allowed"

    hours_to_end = _hours_to_end(snapshot, run_ts)
    if not math.isfinite(hours_to_end):
        return "missing_end_date" if require_hours_to_end else ""
    if hours_to_end < min_hours_to_end:
        return "too_close_to_end"
    return ""


def _open_quote_filter_cancel_reason(
    order: dict[str, Any],
    latest_rows: dict[str, dict[str, Any]],
    config: ShadowPaperTraderConfig,
    run_ts: str,
) -> str:
    market_slug = str(order.get("market_slug") or "")
    snapshot = latest_rows.get(market_slug) or order
    reason = _new_order_skip_reason(
        action=order,
        snapshot=snapshot,
        run_ts=run_ts,
        allowed_families=set(_csv_tuple(config.allowed_new_order_families)),
        blocked_terms=_csv_tuple(config.blocked_new_order_text_terms),
        min_hours_to_end=float(config.min_new_order_hours_to_end),
        require_hours_to_end=bool(config.require_new_order_hours_to_end),
    )
    if reason:
        return reason
    if market_slug not in latest_rows:
        return "missing_latest_snapshot"
    return ""


def _stale_open_quote_reason(
    order: dict[str, Any],
    latest_rows: dict[str, dict[str, Any]],
    config: ShadowPaperTraderConfig,
    run_ts: str,
) -> str:
    market_slug = str(order.get("market_slug") or "")
    latest = latest_rows.get(market_slug)
    if latest and _is_past_end(latest, run_ts, float(config.stale_end_grace_hours)):
        return "past_end"
    opened = _parse_utc(order.get("opened_ts_utc") or order.get("entry_scan_ts_utc"))
    now = _parse_utc(run_ts)
    if opened and now:
        age_hours = (now - opened).total_seconds() / 3600.0
        if age_hours >= float(config.max_open_quote_wall_hours):
            return "stale_open_quote"
    return ""


def _stale_position_reason(
    order: dict[str, Any],
    latest_rows: dict[str, dict[str, Any]],
    config: ShadowPaperTraderConfig,
    run_ts: str,
) -> str:
    market_slug = str(order.get("market_slug") or "")
    latest = latest_rows.get(market_slug)
    if latest and _is_past_end(latest, run_ts, float(config.stale_end_grace_hours)):
        return "past_end"
    last_scan = _parse_utc(order.get("position_last_scan_ts_utc") or order.get("position_open_scan_ts_utc"))
    now = _parse_utc(run_ts)
    if last_scan and now:
        age_hours = (now - last_scan).total_seconds() / 3600.0
        if age_hours >= float(config.max_position_without_update_hours):
            return "stale_position_no_updates"
    return ""


def _cancel_open_quote(order: dict[str, Any], reason: str, run_ts: str) -> None:
    opened = _parse_utc(order.get("entry_scan_ts_utc"))
    canceled = _parse_utc(run_ts)
    order["status"] = QUOTE_CANCELED_BY_FILTER
    order["settled_ts_utc"] = run_ts
    order["next_scan_ts_utc"] = ""
    order["seconds_open"] = round((canceled - opened).total_seconds(), 3) if opened and canceled else ""
    order["filled"] = False
    order["touch_price_next"] = ""
    order["next_mid"] = ""
    order["pnl_per_share_if_filled"] = ""
    order["filter_cancel_reason"] = reason


def _close_stale_position(
    order: dict[str, Any],
    latest_rows: dict[str, dict[str, Any]],
    reason: str,
    run_ts: str,
) -> bool:
    market_slug = str(order.get("market_slug") or "")
    snapshot = latest_rows.get(market_slug)
    outcome = str(order.get("outcome") or "")
    quote_side = str(order.get("quote_side") or "")
    entry_price = safe_float(order.get("quote"), math.nan)
    quote_size = safe_float(order.get("quote_size"), 0.0)
    current_mid = safe_float((snapshot or {}).get(f"{outcome}_mid"), math.nan)
    if not math.isfinite(current_mid):
        current_mid = safe_float(order.get("position_last_mid"), math.nan)
    if not math.isfinite(entry_price) or entry_price <= 0 or quote_size <= 0 or not math.isfinite(current_mid):
        return False

    pnl_per_share = entry_price - current_mid if quote_side == "ask" else current_mid - entry_price
    realized_pnl = pnl_per_share * quote_size
    aggressive = realized_pnl + safe_float(order.get("reward_proxy_aggressive_usd"), 0.0)
    conservative = realized_pnl + safe_float(order.get("reward_proxy_conservative_usd"), 0.0)
    opened = _parse_utc(order.get("position_open_scan_ts_utc") or order.get("next_scan_ts_utc"))
    closed = _parse_utc(run_ts)

    order["status"] = POSITION_CLOSED_STALE
    order["position_exit_reason"] = reason
    order["position_exit_price"] = round(current_mid, 6)
    order["position_last_mid"] = round(current_mid, 6)
    order["position_unrealized_mid_pnl_usd"] = 0.0
    order["position_realized_pnl_usd"] = round(realized_pnl, 6)
    order["position_net_realized_aggressive_reward_usd"] = round(aggressive, 6)
    order["position_net_realized_conservative_reward_usd"] = round(conservative, 6)
    order["position_closed_ts_utc"] = run_ts
    if opened and closed:
        order["position_seconds_held"] = round((closed - opened).total_seconds(), 3)
    return True


def _is_past_end(snapshot: dict[str, Any], run_ts: str, grace_hours: float) -> bool:
    end_ts = _parse_utc(snapshot.get("end_date"))
    now = _parse_utc(run_ts)
    if end_ts is None or now is None:
        return False
    return now >= end_ts + dt.timedelta(hours=max(float(grace_hours), 0.0))


def _outcomes_for_action(action: dict[str, Any]) -> tuple[str, ...]:
    side = str(action.get("side") or "").strip().upper()
    if side in {"YES", "YES_ONLY", "BID_YES"}:
        return ("yes",)
    if side in {"NO", "NO_ONLY", "BID_NO"}:
        return ("no",)
    return ("yes", "no")


def _directional_outcome(action: dict[str, Any]) -> str:
    side = str(action.get("side") or "").strip().upper()
    if side == "YES" or str(action.get("action") or "") == "paper_directional_yes":
        return "yes"
    if side == "NO" or str(action.get("action") or "") == "paper_directional_no":
        return "no"
    return ""


def _cooldown_key(order: dict[str, Any]) -> tuple[str, str]:
    return (
        str(order.get("market_slug") or ""),
        str(order.get("outcome") or ""),
    )


def _cooldown_keys(orders: list[dict[str, Any]], run_ts: str, cooldown_hours: float) -> set[tuple[str, str]]:
    if cooldown_hours <= 0:
        return set()
    now = _parse_utc(run_ts)
    if now is None:
        return set()
    keys: set[tuple[str, str]] = set()
    cooldown = dt.timedelta(hours=float(cooldown_hours))
    for order in orders:
        status = str(order.get("status") or "")
        if status in {OPEN, POSITION_OPEN}:
            keys.add(_cooldown_key(order))
            continue
        if status not in POSITION_CLOSED_STATUSES:
            continue
        closed = _parse_utc(order.get("position_closed_ts_utc") or order.get("settled_ts_utc"))
        if closed and now - closed <= cooldown:
            keys.add(_cooldown_key(order))
    return keys


def _csv_tuple(value: tuple[str, ...] | list[str] | str) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value)
    return tuple(str(part).strip().lower() for part in parts if str(part).strip())


def _build_order(
    *,
    run_ts: str,
    action: dict[str, Any],
    snapshot: dict[str, Any],
    outcome: str,
    quote_side: str,
    quote_size_override: float,
    min_reward_score: float,
) -> dict[str, Any]:
    quote = safe_float(snapshot.get(f"{outcome}_{quote_side}_quote"), math.nan)
    reward_score = safe_float(snapshot.get(f"{outcome}_{quote_side}_reward_score"), 0.0)
    entry_mid = safe_float(snapshot.get(f"{outcome}_mid"), math.nan)
    entry_scan_ts = str(snapshot.get("scan_ts_utc") or "")
    market_slug = str(snapshot.get("market_slug") or action.get("market_slug") or "")
    hours_to_end = _hours_to_end(snapshot, run_ts)
    if (
        not market_slug
        or not entry_scan_ts
        or not math.isfinite(quote)
        or quote <= 0
        or not math.isfinite(entry_mid)
        or entry_mid <= 0
        or reward_score <= min_reward_score
    ):
        return {}

    quote_size = safe_float(snapshot.get("quote_size"), 0.0)
    if quote_size_override > 0:
        quote_size = quote_size_override
    if quote_size <= 0:
        return {}

    order_id = _order_id(
        run_ts,
        entry_scan_ts,
        market_slug,
        outcome,
        quote_side,
        quote,
    )
    return {
        "order_id": order_id,
        "opened_ts_utc": run_ts,
        "entry_scan_ts_utc": entry_scan_ts,
        "market_slug": market_slug,
        "question": str(snapshot.get("question") or action.get("question") or ""),
        "family": str(action.get("family") or ""),
        "action_confidence": str(action.get("confidence") or ""),
        "action_priority_score": action.get("action_priority_score", ""),
        "outcome": outcome,
        "quote_side": quote_side,
        "quote": round(quote, 6),
        "quote_size": round(quote_size, 6),
        "entry_mid": round(entry_mid, 6),
        "entry_best_bid": snapshot.get(f"{outcome}_best_bid", ""),
        "entry_best_ask": snapshot.get(f"{outcome}_best_ask", ""),
        "end_date": snapshot.get("end_date", ""),
        "entry_hours_to_end": round(hours_to_end, 6) if math.isfinite(hours_to_end) else "",
        "daily_rate": snapshot.get("daily_rate", ""),
        "market_competitiveness": snapshot.get("market_competitiveness", ""),
        "reward_score": round(reward_score, 6),
        "fair_value_probability_proxy": action.get("fair_value_probability_proxy", ""),
        "fair_value_edge_proxy": action.get("fair_value_edge_proxy", ""),
        "net_ev_pessimistic_usd": action.get("net_ev_pessimistic_usd", ""),
        "net_ev_base_usd": action.get("net_ev_base_usd", ""),
        "history_observations": action.get("history_observations", ""),
        "history_fill_rate": action.get("history_fill_rate", ""),
        "status": OPEN,
        "settled_ts_utc": "",
        "next_scan_ts_utc": "",
        "seconds_open": "",
        "next_mid": "",
        "touch_price_next": "",
        "filled": "",
        "entry_edge_to_mid": "",
        "next_move_for_quote": "",
        "pnl_per_share_if_filled": "",
        "mark_to_mid_pnl_usd": 0.0,
        "reward_proxy_aggressive_usd": 0.0,
        "reward_proxy_conservative_usd": 0.0,
        "net_pnl_aggressive_reward_usd": 0.0,
        "net_pnl_conservative_reward_usd": 0.0,
        "position_open_scan_ts_utc": "",
        "position_last_scan_ts_utc": "",
        "position_cycles_held": 0,
        "position_last_mid": "",
        "position_exit_price": "",
        "position_unrealized_mid_pnl_usd": 0.0,
        "position_realized_pnl_usd": 0.0,
        "position_net_realized_aggressive_reward_usd": 0.0,
        "position_net_realized_conservative_reward_usd": 0.0,
        "position_exit_reason": "",
        "position_closed_ts_utc": "",
        "position_seconds_held": "",
    }


def _build_directional_position(
    *,
    run_ts: str,
    action: dict[str, Any],
    snapshot: dict[str, Any],
    directional_size_override: float,
) -> dict[str, Any]:
    side = str(action.get("side") or "").strip().upper()
    if not side:
        side = "YES" if str(action.get("action") or "") == "paper_directional_yes" else "NO"
    outcome = "yes" if side == "YES" else "no" if side == "NO" else ""
    if not outcome:
        return {}

    entry_price = safe_float(snapshot.get(f"{outcome}_best_ask"), math.nan)
    entry_mid = safe_float(snapshot.get(f"{outcome}_mid"), math.nan)
    if not math.isfinite(entry_price) or entry_price <= 0:
        entry_price = entry_mid
    entry_scan_ts = str(snapshot.get("scan_ts_utc") or "")
    market_slug = str(snapshot.get("market_slug") or action.get("market_slug") or "")
    if not market_slug or not entry_scan_ts or not math.isfinite(entry_price) or entry_price <= 0:
        return {}
    if not math.isfinite(entry_mid) or entry_mid <= 0:
        entry_mid = entry_price

    quote_size = max(float(directional_size_override), 0.0)
    if quote_size <= 0:
        quote_size = min(max(safe_float(snapshot.get("quote_size"), 0.0), 0.0), 50.0)
    if quote_size <= 0:
        return {}

    mark_to_mid = (entry_mid - entry_price) * quote_size
    hours_to_end = _hours_to_end(snapshot, run_ts)
    order_id = _order_id(
        run_ts,
        entry_scan_ts,
        market_slug,
        outcome,
        "taker_buy",
        entry_price,
    )
    return {
        "order_id": order_id,
        "opened_ts_utc": run_ts,
        "entry_scan_ts_utc": entry_scan_ts,
        "market_slug": market_slug,
        "question": str(snapshot.get("question") or action.get("question") or ""),
        "family": str(action.get("family") or ""),
        "action_confidence": str(action.get("confidence") or ""),
        "action_priority_score": action.get("action_priority_score", ""),
        "outcome": outcome,
        "quote_side": "taker_buy",
        "quote": round(entry_price, 6),
        "quote_size": round(quote_size, 6),
        "entry_mid": round(entry_mid, 6),
        "entry_best_bid": snapshot.get(f"{outcome}_best_bid", ""),
        "entry_best_ask": snapshot.get(f"{outcome}_best_ask", ""),
        "end_date": snapshot.get("end_date", ""),
        "entry_hours_to_end": round(hours_to_end, 6) if math.isfinite(hours_to_end) else "",
        "daily_rate": snapshot.get("daily_rate", ""),
        "market_competitiveness": snapshot.get("market_competitiveness", ""),
        "reward_score": 0.0,
        "fair_value_probability_proxy": action.get("fair_value_probability_proxy", ""),
        "fair_value_edge_proxy": action.get("fair_value_edge_proxy", ""),
        "net_ev_pessimistic_usd": action.get("net_ev_pessimistic_usd", ""),
        "net_ev_base_usd": action.get("net_ev_base_usd", ""),
        "history_observations": action.get("history_observations", ""),
        "history_fill_rate": action.get("history_fill_rate", ""),
        "status": POSITION_OPEN,
        "settled_ts_utc": run_ts,
        "next_scan_ts_utc": entry_scan_ts,
        "seconds_open": 0.0,
        "next_mid": round(entry_mid, 6),
        "touch_price_next": round(entry_price, 6),
        "filled": True,
        "entry_edge_to_mid": round(entry_mid - entry_price, 6),
        "next_move_for_quote": 0.0,
        "pnl_per_share_if_filled": round(entry_mid - entry_price, 6),
        "mark_to_mid_pnl_usd": round(mark_to_mid, 6),
        "reward_proxy_aggressive_usd": 0.0,
        "reward_proxy_conservative_usd": 0.0,
        "net_pnl_aggressive_reward_usd": round(mark_to_mid, 6),
        "net_pnl_conservative_reward_usd": round(mark_to_mid, 6),
        "position_open_scan_ts_utc": entry_scan_ts,
        "position_last_scan_ts_utc": entry_scan_ts,
        "position_cycles_held": 0,
        "position_last_mid": round(entry_mid, 6),
        "position_exit_price": "",
        "position_unrealized_mid_pnl_usd": round(mark_to_mid, 6),
        "position_realized_pnl_usd": 0.0,
        "position_net_realized_aggressive_reward_usd": 0.0,
        "position_net_realized_conservative_reward_usd": 0.0,
        "position_exit_reason": "",
        "position_closed_ts_utc": "",
        "position_seconds_held": "",
    }


def _settle_order(order: dict[str, Any], next_row: dict[str, Any]) -> None:
    outcome = str(order.get("outcome") or "")
    quote_side = str(order.get("quote_side") or "")
    quote = safe_float(order.get("quote"), math.nan)
    quote_size = safe_float(order.get("quote_size"), 0.0)
    entry_mid = safe_float(order.get("entry_mid"), math.nan)
    next_mid = safe_float(next_row.get(f"{outcome}_mid"), math.nan)
    opened = _parse_utc(order.get("entry_scan_ts_utc"))
    settled = _parse_utc(next_row.get("scan_ts_utc"))
    seconds = (settled - opened).total_seconds() if opened and settled else 0.0

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

    mark_to_mid = pnl_per_share * quote_size if filled and math.isfinite(pnl_per_share) else 0.0
    reward_proxy = _reward_proxy_usd(order, safe_float(order.get("reward_score"), 0.0), seconds)
    conservative_reward = 0.0 if filled else reward_proxy

    order["status"] = POSITION_OPEN if filled else NOT_FILLED
    order["settled_ts_utc"] = utc_iso()
    order["next_scan_ts_utc"] = str(next_row.get("scan_ts_utc") or "")
    order["seconds_open"] = round(seconds, 3)
    order["next_mid"] = round(next_mid, 6) if math.isfinite(next_mid) else ""
    order["touch_price_next"] = round(touch_price, 6) if math.isfinite(touch_price) else ""
    order["filled"] = filled
    order["entry_edge_to_mid"] = round(entry_edge_to_mid, 6) if math.isfinite(entry_edge_to_mid) else ""
    order["next_move_for_quote"] = round(next_move_for_quote, 6) if math.isfinite(next_move_for_quote) else ""
    order["pnl_per_share_if_filled"] = round(pnl_per_share, 6) if math.isfinite(pnl_per_share) else ""
    order["mark_to_mid_pnl_usd"] = round(mark_to_mid, 6)
    order["reward_proxy_aggressive_usd"] = round(reward_proxy, 6)
    order["reward_proxy_conservative_usd"] = round(conservative_reward, 6)
    order["net_pnl_aggressive_reward_usd"] = round(mark_to_mid + reward_proxy, 6)
    order["net_pnl_conservative_reward_usd"] = round(mark_to_mid + conservative_reward, 6)
    if filled:
        order["position_open_scan_ts_utc"] = str(next_row.get("scan_ts_utc") or "")
        order["position_last_scan_ts_utc"] = str(next_row.get("scan_ts_utc") or "")
        order["position_cycles_held"] = 0
        order["position_last_mid"] = round(next_mid, 6) if math.isfinite(next_mid) else ""
        order["position_unrealized_mid_pnl_usd"] = round(mark_to_mid, 6)


def _migrate_legacy_filled_order(order: dict[str, Any]) -> None:
    if str(order.get("status") or "") != FILLED:
        return
    order["status"] = POSITION_OPEN
    order.setdefault("position_open_scan_ts_utc", str(order.get("next_scan_ts_utc") or ""))
    order.setdefault("position_last_scan_ts_utc", str(order.get("next_scan_ts_utc") or ""))
    order.setdefault("position_cycles_held", 0)
    order.setdefault("position_last_mid", str(order.get("next_mid") or ""))
    order.setdefault("position_unrealized_mid_pnl_usd", str(order.get("mark_to_mid_pnl_usd") or 0.0))
    order.setdefault("position_realized_pnl_usd", 0.0)
    order.setdefault("position_net_realized_aggressive_reward_usd", 0.0)
    order.setdefault("position_net_realized_conservative_reward_usd", 0.0)


def _manage_position(
    order: dict[str, Any],
    rows: list[dict[str, Any]],
    selected_action_markets: set[str],
    config: ShadowPaperTraderConfig,
) -> bool:
    snapshot = _next_position_snapshot(rows, order)
    if not snapshot:
        return False

    outcome = str(order.get("outcome") or "")
    quote_side = str(order.get("quote_side") or "")
    entry_price = safe_float(order.get("quote"), math.nan)
    quote_size = safe_float(order.get("quote_size"), 0.0)
    current_mid = safe_float(snapshot.get(f"{outcome}_mid"), math.nan)
    if not math.isfinite(entry_price) or entry_price <= 0 or quote_size <= 0 or not math.isfinite(current_mid):
        return False

    if quote_side == "ask":
        exit_price = safe_float(snapshot.get(f"{outcome}_best_ask"), math.nan)
        if not math.isfinite(exit_price) or exit_price <= 0:
            exit_price = current_mid
        pnl_per_share = entry_price - exit_price
        unrealized_mid_pnl = (entry_price - current_mid) * quote_size
    else:
        exit_price = safe_float(snapshot.get(f"{outcome}_best_bid"), math.nan)
        if not math.isfinite(exit_price) or exit_price <= 0:
            exit_price = current_mid
        pnl_per_share = exit_price - entry_price
        unrealized_mid_pnl = (current_mid - entry_price) * quote_size

    cycles_held = int(safe_float(order.get("position_cycles_held"), 0.0)) + 1
    order["position_cycles_held"] = cycles_held
    order["position_last_scan_ts_utc"] = str(snapshot.get("scan_ts_utc") or "")
    order["position_last_mid"] = round(current_mid, 6)
    order["position_exit_price"] = round(exit_price, 6)
    order["position_unrealized_mid_pnl_usd"] = round(unrealized_mid_pnl, 6)

    exit_reason = ""
    if pnl_per_share >= float(config.take_profit_per_share):
        exit_reason = "take_profit"
        status = POSITION_CLOSED_TAKE_PROFIT
    elif pnl_per_share <= -float(config.stop_loss_per_share):
        exit_reason = "stop_loss"
        status = POSITION_CLOSED_STOP_LOSS
    elif cycles_held >= int(config.max_position_cycles):
        exit_reason = "max_hold"
        status = POSITION_CLOSED_MAX_HOLD
    elif bool(config.close_on_signal_loss) and str(order.get("market_slug") or "") not in selected_action_markets:
        exit_reason = "signal_loss"
        status = POSITION_CLOSED_SIGNAL_LOSS
    else:
        return False

    realized_pnl = pnl_per_share * quote_size
    aggressive = realized_pnl + safe_float(order.get("reward_proxy_aggressive_usd"), 0.0)
    conservative = realized_pnl + safe_float(order.get("reward_proxy_conservative_usd"), 0.0)
    opened = _parse_utc(order.get("position_open_scan_ts_utc") or order.get("next_scan_ts_utc"))
    closed = _parse_utc(snapshot.get("scan_ts_utc"))

    order["status"] = status
    order["position_exit_reason"] = exit_reason
    order["position_realized_pnl_usd"] = round(realized_pnl, 6)
    order["position_net_realized_aggressive_reward_usd"] = round(aggressive, 6)
    order["position_net_realized_conservative_reward_usd"] = round(conservative, 6)
    order["position_closed_ts_utc"] = utc_iso()
    if opened and closed:
        order["position_seconds_held"] = round((closed - opened).total_seconds(), 3)
    return True


def _next_position_snapshot(rows: list[dict[str, Any]], order: dict[str, Any]) -> dict[str, Any]:
    last_ts = _parse_utc(order.get("position_last_scan_ts_utc") or order.get("position_open_scan_ts_utc"))
    if last_ts is None:
        return {}
    for row in rows:
        row_ts = row.get("_scan_dt")
        if isinstance(row_ts, dt.datetime) and row_ts > last_ts:
            return row
    return {}


def _next_snapshot(rows: list[dict[str, Any]], order: dict[str, Any]) -> dict[str, Any]:
    entry_ts = _parse_utc(order.get("entry_scan_ts_utc"))
    if entry_ts is None:
        return {}
    for row in rows:
        row_ts = row.get("_scan_dt")
        if isinstance(row_ts, dt.datetime) and row_ts > entry_ts:
            return row
    return {}


def _hours_to_end(snapshot: dict[str, Any], run_ts: str) -> float:
    end_ts = _parse_utc(snapshot.get("end_date"))
    reference_ts = _parse_utc(snapshot.get("scan_ts_utc")) or _parse_utc(run_ts)
    if end_ts is None or reference_ts is None:
        return math.nan
    return round((end_ts - reference_ts).total_seconds() / 3600.0, 6)


def _history_by_market(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        market_slug = str(row.get("market_slug") or "")
        scan_ts = _parse_utc(row.get("scan_ts_utc"))
        if not market_slug or scan_ts is None:
            continue
        normalized = dict(row)
        normalized["_scan_dt"] = scan_ts
        grouped[market_slug].append(normalized)
    for market_rows in grouped.values():
        market_rows.sort(key=lambda row: row["_scan_dt"])
    return grouped


def _summaries(rows: list[dict[str, Any]], key_fields: list[str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(field, "") for field in key_fields)].append(row)
    summaries = [_summary_row({field: key[index] for index, field in enumerate(key_fields)}, group) for key, group in grouped.items()]
    summaries.sort(key=lambda row: safe_float(row.get("net_pnl_conservative_reward_usd"), -math.inf), reverse=True)
    return summaries


def _summary_row(prefix: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    open_quotes = [row for row in rows if str(row.get("status") or "") == OPEN]
    open_positions = [row for row in rows if str(row.get("status") or "") == POSITION_OPEN]
    closed_positions = [row for row in rows if str(row.get("status") or "") in POSITION_CLOSED_STATUSES]
    settled = [row for row in rows if str(row.get("status") or "") != OPEN]
    filled = [row for row in settled if str(row.get("filled")).lower() == "true"]
    row = {
        **prefix,
        "orders": total,
        "open_orders": len(open_quotes) + len(open_positions),
        "open_quote_orders": len(open_quotes),
        "open_positions": len(open_positions),
        "settled_orders": len(settled),
        "filled_orders": len(filled),
        "closed_positions": len(closed_positions),
        "fill_rate_settled": round(len(filled) / len(settled), 6) if settled else 0.0,
    }
    for field in (
        "mark_to_mid_pnl_usd",
        "reward_proxy_aggressive_usd",
        "reward_proxy_conservative_usd",
        "net_pnl_aggressive_reward_usd",
        "net_pnl_conservative_reward_usd",
    ):
        value = sum(safe_float(order.get(field), 0.0) for order in settled)
        row[field] = round(value, 6)
        row[f"avg_{field}"] = round(value / len(settled), 6) if settled else 0.0
    if filled:
        row["avg_pnl_per_filled_order_usd"] = round(
            sum(safe_float(order.get("mark_to_mid_pnl_usd"), 0.0) for order in filled) / len(filled),
            6,
        )
    else:
        row["avg_pnl_per_filled_order_usd"] = 0.0
    for field in (
        "position_realized_pnl_usd",
        "position_unrealized_mid_pnl_usd",
        "position_net_realized_aggressive_reward_usd",
        "position_net_realized_conservative_reward_usd",
    ):
        source = closed_positions if "realized" in field else open_positions
        value = sum(safe_float(order.get(field), 0.0) for order in source)
        row[field] = round(value, 6)
        row[f"avg_{field}"] = round(value / len(source), 6) if source else 0.0
    mark_pnl = safe_float(row.get("mark_to_mid_pnl_usd"), 0.0)
    conservative_reward = safe_float(row.get("reward_proxy_conservative_usd"), 0.0)
    aggressive_reward = safe_float(row.get("reward_proxy_aggressive_usd"), 0.0)
    row["conservative_reward_fraction_needed_to_break_even"] = (
        round((-mark_pnl) / conservative_reward, 6)
        if mark_pnl < 0 and conservative_reward > 0
        else ""
    )
    row["aggressive_reward_fraction_needed_to_break_even"] = (
        round((-mark_pnl) / aggressive_reward, 6)
        if mark_pnl < 0 and aggressive_reward > 0
        else ""
    )
    return row


def _active_exit_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positions = [
        row for row in rows
        if str(row.get("filled")).lower() == "true"
        or str(row.get("status") or "") in {POSITION_OPEN, *POSITION_CLOSED_STATUSES}
    ]
    open_positions = [row for row in positions if str(row.get("status") or "") == POSITION_OPEN]
    closed_positions = [row for row in positions if str(row.get("status") or "") in POSITION_CLOSED_STATUSES]
    reason_counts: dict[str, int] = defaultdict(int)
    for row in closed_positions:
        reason_counts[str(row.get("position_exit_reason") or "unknown")] += 1
    realized = sum(safe_float(row.get("position_realized_pnl_usd"), 0.0) for row in closed_positions)
    unrealized = sum(safe_float(row.get("position_unrealized_mid_pnl_usd"), 0.0) for row in open_positions)
    return {
        "positions_total": len(positions),
        "open_positions": len(open_positions),
        "closed_positions": len(closed_positions),
        "take_profit_closes": reason_counts.get("take_profit", 0),
        "stop_loss_closes": reason_counts.get("stop_loss", 0),
        "max_hold_closes": reason_counts.get("max_hold", 0),
        "signal_loss_closes": reason_counts.get("signal_loss", 0),
        "realized_position_pnl_usd": round(realized, 6),
        "unrealized_open_position_mid_pnl_usd": round(unrealized, 6),
        "net_realized_aggressive_reward_usd": round(
            sum(safe_float(row.get("position_net_realized_aggressive_reward_usd"), 0.0) for row in closed_positions),
            6,
        ),
        "net_realized_conservative_reward_usd": round(
            sum(safe_float(row.get("position_net_realized_conservative_reward_usd"), 0.0) for row in closed_positions),
            6,
        ),
        "avg_realized_position_pnl_usd": round(realized / len(closed_positions), 6) if closed_positions else 0.0,
    }


def _reward_proxy_usd(order: dict[str, Any], quote_score: float, seconds: float) -> float:
    daily_rate = safe_float(order.get("daily_rate"), 0.0)
    competitiveness = max(safe_float(order.get("market_competitiveness"), 0.0), 0.0)
    if daily_rate <= 0 or quote_score <= 0 or seconds <= 0:
        return 0.0
    reward_pool = daily_rate * seconds / 86400.0
    share = quote_score / (competitiveness + quote_score) if competitiveness + quote_score > 0 else 0.0
    return reward_pool * min(max(share, 0.0), 1.0)


def _open_position_key(order: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(order.get("market_slug") or ""),
        str(order.get("outcome") or ""),
        str(order.get("quote_side") or ""),
    )


def _order_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


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


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, rows: list[dict[str, Any]], append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not append:
        path.write_text("", encoding="utf-8")
        return
    if not rows:
        return
    fieldnames = _fieldnames(rows)
    write_header = not append or not path.exists() or path.stat().st_size == 0
    with path.open("a" if append else "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows([{k: v for k, v in row.items() if not k.startswith("_")} for row in rows])


def _fieldnames(rows: Iterable[dict[str, Any]]) -> list[str]:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if not key.startswith("_") and key not in fieldnames:
                fieldnames.append(key)
    return fieldnames


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
