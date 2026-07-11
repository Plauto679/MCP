from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a small sanitized dry-run snapshot for remote monitoring."
    )
    parser.add_argument("--run-name", default="reward_fair_value_dry_live")
    parser.add_argument("--output-dir", default=str(ROOT / "run_reports"))
    parser.add_argument("--history-limit", type=int, default=1000)
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument(
        "--heartbeat-json",
        default="",
        help="Defaults to data/<run-name>/heartbeat.json.",
    )
    parser.add_argument(
        "--shadow-report-json",
        default=str(ROOT / "data" / "shadow_paper_trader_active_live" / "report.json"),
        help="Active-exit dry ledger report.",
    )
    parser.add_argument(
        "--shadow-hold-report-json",
        default=str(ROOT / "data" / "shadow_paper_trader_hold_live" / "report.json"),
        help="Long-hold dry ledger report.",
    )
    parser.add_argument(
        "--shadow-taker-active-report-json",
        default=str(ROOT / "data" / "shadow_paper_trader_taker_active_live" / "report.json"),
        help="Taker diagnostic active-exit dry ledger report.",
    )
    parser.add_argument(
        "--shadow-taker-hold-report-json",
        default=str(ROOT / "data" / "shadow_paper_trader_taker_hold_live" / "report.json"),
        help="Taker diagnostic long-hold dry ledger report.",
    )
    parser.add_argument(
        "--evaluator-report-json",
        default=str(ROOT / "data" / "reward_fair_value_evaluator_live" / "report.json"),
    )
    parser.add_argument(
        "--paper-orders-csv",
        default=str(ROOT / "data" / "shadow_paper_trader_active_live" / "paper_orders.csv"),
        help="Active-exit dry ledger orders.",
    )
    parser.add_argument(
        "--hold-paper-orders-csv",
        default=str(ROOT / "data" / "shadow_paper_trader_hold_live" / "paper_orders.csv"),
        help="Long-hold dry ledger orders.",
    )
    parser.add_argument(
        "--taker-active-paper-orders-csv",
        default=str(ROOT / "data" / "shadow_paper_trader_taker_active_live" / "paper_orders.csv"),
        help="Taker diagnostic active-exit dry ledger orders.",
    )
    parser.add_argument(
        "--taker-hold-paper-orders-csv",
        default=str(ROOT / "data" / "shadow_paper_trader_taker_hold_live" / "paper_orders.csv"),
        help="Taker diagnostic long-hold dry ledger orders.",
    )
    parser.add_argument(
        "--shadow-actions-csv",
        default=str(ROOT / "data" / "reward_fair_value_evaluator_live" / "shadow_actions.csv"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    heartbeat_path = Path(args.heartbeat_json) if args.heartbeat_json else ROOT / "data" / args.run_name / "heartbeat.json"
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    snapshot = build_snapshot(
        run_name=str(args.run_name),
        heartbeat_path=heartbeat_path,
        shadow_report_path=Path(args.shadow_report_json),
        shadow_hold_report_path=Path(args.shadow_hold_report_json),
        shadow_taker_active_report_path=Path(args.shadow_taker_active_report_json),
        shadow_taker_hold_report_path=Path(args.shadow_taker_hold_report_json),
        evaluator_report_path=Path(args.evaluator_report_json),
        paper_orders_path=Path(args.paper_orders_csv),
        hold_paper_orders_path=Path(args.hold_paper_orders_csv),
        taker_active_paper_orders_path=Path(args.taker_active_paper_orders_csv),
        taker_hold_paper_orders_path=Path(args.taker_hold_paper_orders_csv),
        shadow_actions_path=Path(args.shadow_actions_csv),
        window_hours=float(args.window_hours),
    )
    (output_dir / "latest_summary.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (output_dir / "latest_summary.md").write_text(render_markdown(snapshot), encoding="utf-8")
    append_history(output_dir / "history.jsonl", snapshot, max_rows=int(args.history_limit))
    print(json.dumps({"ok": True, "outputs": [str(output_dir / "latest_summary.json"), str(output_dir / "latest_summary.md")]}))
    return 0


def build_snapshot(
    *,
    run_name: str,
    heartbeat_path: Path,
    shadow_report_path: Path,
    shadow_hold_report_path: Path,
    shadow_taker_active_report_path: Path,
    shadow_taker_hold_report_path: Path,
    evaluator_report_path: Path,
    paper_orders_path: Path,
    hold_paper_orders_path: Path,
    taker_active_paper_orders_path: Path,
    taker_hold_paper_orders_path: Path,
    shadow_actions_path: Path,
    window_hours: float,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    heartbeat = read_json(heartbeat_path)
    shadow_report = read_json(shadow_report_path)
    shadow_hold_report = read_json(shadow_hold_report_path)
    shadow_taker_active_report = read_json(shadow_taker_active_report_path)
    shadow_taker_hold_report = read_json(shadow_taker_hold_report_path)
    evaluator_report = read_json(evaluator_report_path)
    orders = read_csv(paper_orders_path)
    hold_orders = read_csv(hold_paper_orders_path)
    taker_active_orders = read_csv(taker_active_paper_orders_path)
    taker_hold_orders = read_csv(taker_hold_paper_orders_path)
    actions = read_csv(shadow_actions_path)

    all_pnl = summarize_orders(orders)
    window_pnl = summarize_orders(
        [
            row for row in orders
            if is_recent(row.get("opened_ts_utc"), now=now, hours=window_hours)
            or is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
        ]
    )
    hold_all_pnl = summarize_orders(hold_orders)
    hold_window_pnl = summarize_orders(
        [
            row for row in hold_orders
            if is_recent(row.get("opened_ts_utc"), now=now, hours=window_hours)
            or is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
        ]
    )
    hold_fed_recent = summarize_orders(
        [
            row for row in hold_orders
            if str(row.get("family") or "") == "fed_rates"
            and (
                is_recent(row.get("opened_ts_utc"), now=now, hours=window_hours)
                or is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
            )
        ]
    )
    taker_active_all_pnl = summarize_orders(taker_active_orders)
    taker_active_window_pnl = summarize_orders(
        [
            row for row in taker_active_orders
            if is_recent(row.get("opened_ts_utc"), now=now, hours=window_hours)
            or is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
        ]
    )
    taker_active_fed_recent = summarize_orders(
        [
            row for row in taker_active_orders
            if str(row.get("family") or "") == "fed_rates"
            and (
                is_recent(row.get("opened_ts_utc"), now=now, hours=window_hours)
                or is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
            )
        ]
    )
    taker_hold_all_pnl = summarize_orders(taker_hold_orders)
    taker_hold_window_pnl = summarize_orders(
        [
            row for row in taker_hold_orders
            if is_recent(row.get("opened_ts_utc"), now=now, hours=window_hours)
            or is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
        ]
    )
    taker_hold_fed_recent = summarize_orders(
        [
            row for row in taker_hold_orders
            if str(row.get("family") or "") == "fed_rates"
            and (
                is_recent(row.get("opened_ts_utc"), now=now, hours=window_hours)
                or is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
            )
        ]
    )
    fed_recent = summarize_orders(
        [
            row for row in orders
            if str(row.get("family") or "") == "fed_rates"
            and (
                is_recent(row.get("opened_ts_utc"), now=now, hours=window_hours)
                or is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
            )
        ]
    )

    open_positions = [
        compact_order(row) for row in orders
        if str(row.get("status") or "") == "POSITION_OPEN"
    ][:20]
    open_quotes = [
        compact_order(row) for row in orders
        if str(row.get("status") or "") == "OPEN"
    ][:20]
    hold_open_positions = [
        compact_order(row) for row in hold_orders
        if str(row.get("status") or "") == "POSITION_OPEN"
    ][:20]
    taker_active_open_positions = [
        compact_order(row) for row in taker_active_orders
        if str(row.get("status") or "") == "POSITION_OPEN"
    ][:20]
    taker_hold_open_positions = [
        compact_order(row) for row in taker_hold_orders
        if str(row.get("status") or "") == "POSITION_OPEN"
    ][:20]
    worst_recent = sorted(
        [
            compact_order(row) for row in orders
            if str(row.get("status") or "").startswith("POSITION_CLOSED")
            and is_recent(row.get("position_closed_ts_utc"), now=now, hours=window_hours)
        ],
        key=lambda row: to_float(row.get("realized_pnl"), 0.0),
    )[:10]

    action_counts = Counter(
        f"{row.get('action', '')}|{row.get('family', '')}|{row.get('side', '')}"
        for row in actions
    )

    return {
        "snapshot_ts_utc": now.isoformat(),
        "run_name": run_name,
        "mode": "dry_research_only",
        "health": {
            "heartbeat_exists": heartbeat_path.exists(),
            "shadow_active_report_exists": shadow_report_path.exists(),
            "shadow_hold_report_exists": shadow_hold_report_path.exists(),
            "shadow_taker_active_report_exists": shadow_taker_active_report_path.exists(),
            "shadow_taker_hold_report_exists": shadow_taker_hold_report_path.exists(),
            "evaluator_report_exists": evaluator_report_path.exists(),
            "cycle": heartbeat.get("cycle", ""),
            "cycle_end": heartbeat.get("cycle_end", ""),
            "exit_codes": heartbeat.get("exit_codes", {}),
        },
        "pnl": {
            "all": all_pnl,
            f"last_{int(window_hours)}h": window_pnl,
            f"fed_rates_last_{int(window_hours)}h": fed_recent,
        },
        "hold_pnl": {
            "all": hold_all_pnl,
            f"last_{int(window_hours)}h": hold_window_pnl,
            f"fed_rates_last_{int(window_hours)}h": hold_fed_recent,
        },
        "active_vs_hold": {
            "all_hold_minus_active_usd": pnl_delta(hold_all_pnl, all_pnl),
            f"last_{int(window_hours)}h_hold_minus_active_usd": pnl_delta(hold_window_pnl, window_pnl),
            f"fed_rates_last_{int(window_hours)}h_hold_minus_active_usd": pnl_delta(hold_fed_recent, fed_recent),
            "note": "Positive means long-hold is currently outperforming active-exit on mark-to-mid PnL.",
        },
        "taker_pnl": {
            "active_all": taker_active_all_pnl,
            f"active_last_{int(window_hours)}h": taker_active_window_pnl,
            f"active_fed_rates_last_{int(window_hours)}h": taker_active_fed_recent,
            "hold_all": taker_hold_all_pnl,
            f"hold_last_{int(window_hours)}h": taker_hold_window_pnl,
            f"hold_fed_rates_last_{int(window_hours)}h": taker_hold_fed_recent,
        },
        "taker_active_vs_hold": {
            "all_hold_minus_active_usd": pnl_delta(taker_hold_all_pnl, taker_active_all_pnl),
            f"last_{int(window_hours)}h_hold_minus_active_usd": pnl_delta(taker_hold_window_pnl, taker_active_window_pnl),
            f"fed_rates_last_{int(window_hours)}h_hold_minus_active_usd": pnl_delta(taker_hold_fed_recent, taker_active_fed_recent),
            "note": "Dry diagnostic only: taker entries are for model validation, not a production recommendation.",
        },
        "reward_proxy": {
            "all_conservative_usd": nested_get(shadow_report, ["overall", "reward_proxy_conservative_usd"]),
            "all_net_conservative_usd": nested_get(shadow_report, ["overall", "net_pnl_conservative_reward_usd"]),
            "hold_all_conservative_usd": nested_get(shadow_hold_report, ["overall", "reward_proxy_conservative_usd"]),
            "hold_all_net_conservative_usd": nested_get(shadow_hold_report, ["overall", "net_pnl_conservative_reward_usd"]),
            "taker_active_all_conservative_usd": nested_get(shadow_taker_active_report, ["overall", "reward_proxy_conservative_usd"]),
            "taker_hold_all_conservative_usd": nested_get(shadow_taker_hold_report, ["overall", "reward_proxy_conservative_usd"]),
            "warning": "Proxy only. Not confirmed Polymarket payout.",
        },
        "exposure": {
            "open_positions": open_positions,
            "open_quotes": open_quotes,
            "hold_open_positions": hold_open_positions,
            "taker_active_open_positions": taker_active_open_positions,
            "taker_hold_open_positions": taker_hold_open_positions,
        },
        "recent_worst_closed": worst_recent,
        "fed_context": summarize_fed_context(evaluator_report.get("fed_context", {})),
        "shadow_action_counts": dict(action_counts.most_common(25)),
        "files": {
            "heartbeat": str(heartbeat_path),
            "shadow_active_report": str(shadow_report_path),
            "shadow_hold_report": str(shadow_hold_report_path),
            "shadow_taker_active_report": str(shadow_taker_active_report_path),
            "shadow_taker_hold_report": str(shadow_taker_hold_report_path),
            "evaluator_report": str(evaluator_report_path),
            "active_paper_orders": str(paper_orders_path),
            "hold_paper_orders": str(hold_paper_orders_path),
            "taker_active_paper_orders": str(taker_active_paper_orders_path),
            "taker_hold_paper_orders": str(taker_hold_paper_orders_path),
        },
    }


def summarize_orders(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in rows if str(row.get("status") or "").startswith("POSITION_CLOSED")]
    open_positions = [row for row in rows if str(row.get("status") or "") == "POSITION_OPEN"]
    open_quotes = [row for row in rows if str(row.get("status") or "") == "OPEN"]
    filled = [row for row in rows if str(row.get("filled") or "").lower() == "true"]
    realized = sum(to_float(row.get("position_realized_pnl_usd"), 0.0) for row in closed)
    unrealized = sum(to_float(row.get("position_unrealized_mid_pnl_usd"), 0.0) for row in open_positions)
    by_family: dict[str, float] = defaultdict(float)
    for row in closed:
        by_family[str(row.get("family") or "unknown")] += to_float(row.get("position_realized_pnl_usd"), 0.0)
    for row in open_positions:
        by_family[str(row.get("family") or "unknown")] += to_float(row.get("position_unrealized_mid_pnl_usd"), 0.0)
    exit_reasons = Counter(str(row.get("position_exit_reason") or "unknown") for row in closed)
    return {
        "orders": len(rows),
        "filled": len(filled),
        "open_quotes": len(open_quotes),
        "open_positions": len(open_positions),
        "closed_positions": len(closed),
        "realized_position_pnl_usd": round(realized, 6),
        "unrealized_position_pnl_usd": round(unrealized, 6),
        "active_total_pnl_usd": round(realized + unrealized, 6),
        "by_family_active_total_pnl_usd": {key: round(value, 6) for key, value in sorted(by_family.items())},
        "exit_reasons": dict(exit_reasons),
    }


def pnl_delta(left: dict[str, Any], right: dict[str, Any]) -> float:
    return round(
        to_float(left.get("active_total_pnl_usd"), 0.0)
        - to_float(right.get("active_total_pnl_usd"), 0.0),
        6,
    )


def compact_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "opened_ts_utc": row.get("opened_ts_utc", ""),
        "closed_ts_utc": row.get("position_closed_ts_utc", ""),
        "family": row.get("family", ""),
        "status": row.get("status", ""),
        "quote_side": row.get("quote_side", ""),
        "market_slug": row.get("market_slug", ""),
        "outcome": row.get("outcome", ""),
        "quote": row.get("quote", ""),
        "entry_mid": row.get("entry_mid", ""),
        "last_mid": row.get("position_last_mid", ""),
        "realized_pnl": row.get("position_realized_pnl_usd", ""),
        "unrealized_pnl": row.get("position_unrealized_mid_pnl_usd", ""),
        "exit_reason": row.get("position_exit_reason", ""),
        "fair_value_probability": row.get("fair_value_probability_proxy", ""),
        "fair_value_edge": row.get("fair_value_edge_proxy", ""),
    }


def summarize_fed_context(context: dict[str, Any]) -> dict[str, Any]:
    futures = context.get("futures") if isinstance(context, dict) else {}
    return {
        "source": context.get("source", "") if isinstance(context, dict) else "",
        "current_effr": context.get("current_effr", "") if isinstance(context, dict) else "",
        "current_effr_source": context.get("current_effr_source", "") if isinstance(context, dict) else "",
        "futures": futures if isinstance(futures, dict) else {},
        "warning": "Fed proxy is not official CME FedWatch unless manual probabilities are supplied.",
    }


def render_markdown(snapshot: dict[str, Any]) -> str:
    all_pnl = snapshot["pnl"]["all"]
    window_key = next(key for key in snapshot["pnl"] if key.startswith("last_"))
    window_pnl = snapshot["pnl"][window_key]
    fed_key = next(key for key in snapshot["pnl"] if key.startswith("fed_rates_last_"))
    fed_pnl = snapshot["pnl"][fed_key]
    hold_all_pnl = snapshot["hold_pnl"]["all"]
    hold_window_pnl = snapshot["hold_pnl"][window_key]
    hold_fed_pnl = snapshot["hold_pnl"][fed_key]
    taker = snapshot["taker_pnl"]
    taker_active_all = taker["active_all"]
    taker_hold_all = taker["hold_all"]
    taker_active_window = taker[f"active_{window_key}"]
    taker_hold_window = taker[f"hold_{window_key}"]
    taker_active_fed = taker[f"active_{fed_key}"]
    taker_hold_fed = taker[f"hold_{fed_key}"]
    health = snapshot["health"]
    lines = [
        "# Dry Run Snapshot",
        "",
        f"- Snapshot UTC: `{snapshot['snapshot_ts_utc']}`",
        f"- Mode: `{snapshot['mode']}`",
        f"- Cycle: `{health.get('cycle', '')}`",
        f"- Cycle end: `{health.get('cycle_end', '')}`",
        f"- Exit codes: `{json.dumps(health.get('exit_codes', {}), sort_keys=True)}`",
        "",
        "## PnL",
        "",
        f"- Active-exit all PnL: `{all_pnl['active_total_pnl_usd']}` "
        f"(realized `{all_pnl['realized_position_pnl_usd']}`, unrealized `{all_pnl['unrealized_position_pnl_usd']}`)",
        f"- Long-hold all PnL: `{hold_all_pnl['active_total_pnl_usd']}` "
        f"(realized `{hold_all_pnl['realized_position_pnl_usd']}`, unrealized `{hold_all_pnl['unrealized_position_pnl_usd']}`)",
        f"- All delta hold-active: `{snapshot['active_vs_hold']['all_hold_minus_active_usd']}`",
        f"- Active-exit {window_key} PnL: `{window_pnl['active_total_pnl_usd']}`",
        f"- Long-hold {window_key} PnL: `{hold_window_pnl['active_total_pnl_usd']}`",
        f"- {window_key} delta hold-active: `{snapshot['active_vs_hold'][f'{window_key}_hold_minus_active_usd']}`",
        f"- Active-exit {fed_key} PnL: `{fed_pnl['active_total_pnl_usd']}`",
        f"- Long-hold {fed_key} PnL: `{hold_fed_pnl['active_total_pnl_usd']}`",
        f"- {fed_key} delta hold-active: `{snapshot['active_vs_hold'][f'{fed_key}_hold_minus_active_usd']}`",
        f"- Active open positions: `{all_pnl['open_positions']}`",
        f"- Long-hold open positions: `{hold_all_pnl['open_positions']}`",
        f"- Active open quotes: `{all_pnl['open_quotes']}`",
        "",
        "## Taker Diagnostic PnL",
        "",
        f"- Taker active all PnL: `{taker_active_all['active_total_pnl_usd']}`",
        f"- Taker long-hold all PnL: `{taker_hold_all['active_total_pnl_usd']}`",
        f"- Taker all delta hold-active: `{snapshot['taker_active_vs_hold']['all_hold_minus_active_usd']}`",
        f"- Taker active {window_key} PnL: `{taker_active_window['active_total_pnl_usd']}`",
        f"- Taker long-hold {window_key} PnL: `{taker_hold_window['active_total_pnl_usd']}`",
        f"- Taker active {fed_key} PnL: `{taker_active_fed['active_total_pnl_usd']}`",
        f"- Taker long-hold {fed_key} PnL: `{taker_hold_fed['active_total_pnl_usd']}`",
        f"- Taker active open positions: `{taker_active_all['open_positions']}`",
        f"- Taker long-hold open positions: `{taker_hold_all['open_positions']}`",
        "- Note: dry diagnostic only; not a production taker strategy.",
        "",
        "## Reward Proxy",
        "",
        f"- Conservative proxy: `{snapshot['reward_proxy']['all_conservative_usd']}`",
        f"- Conservative net proxy: `{snapshot['reward_proxy']['all_net_conservative_usd']}`",
        f"- Long-hold conservative proxy: `{snapshot['reward_proxy']['hold_all_conservative_usd']}`",
        f"- Long-hold conservative net proxy: `{snapshot['reward_proxy']['hold_all_net_conservative_usd']}`",
        "- Warning: proxy only, not confirmed payout.",
        "",
        "## Fed Context",
        "",
        f"- Source: `{snapshot['fed_context']['source']}`",
        f"- Current EFFR: `{snapshot['fed_context']['current_effr']}`",
        f"- EFFR source: `{snapshot['fed_context']['current_effr_source']}`",
        "",
        "## Active Open Positions",
        "",
    ]
    if snapshot["exposure"]["open_positions"]:
        for row in snapshot["exposure"]["open_positions"][:10]:
            lines.append(
                f"- `{row['family']}` `{row['quote_side']}` `{row['outcome']}` "
                f"`{row['market_slug']}` pnl `{row['unrealized_pnl']}` edge `{row['fair_value_edge']}`"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Long-Hold Open Positions", ""])
    if snapshot["exposure"]["hold_open_positions"]:
        for row in snapshot["exposure"]["hold_open_positions"][:10]:
            lines.append(
                f"- `{row['family']}` `{row['quote_side']}` `{row['outcome']}` "
                f"`{row['market_slug']}` pnl `{row['unrealized_pnl']}` edge `{row['fair_value_edge']}`"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Taker Diagnostic Open Positions", ""])
    if snapshot["exposure"]["taker_active_open_positions"]:
        for row in snapshot["exposure"]["taker_active_open_positions"][:10]:
            lines.append(
                f"- active `{row['family']}` `{row['quote_side']}` `{row['outcome']}` "
                f"`{row['market_slug']}` pnl `{row['unrealized_pnl']}` edge `{row['fair_value_edge']}`"
            )
    else:
        lines.append("- Active: none")
    if snapshot["exposure"]["taker_hold_open_positions"]:
        for row in snapshot["exposure"]["taker_hold_open_positions"][:10]:
            lines.append(
                f"- hold `{row['family']}` `{row['quote_side']}` `{row['outcome']}` "
                f"`{row['market_slug']}` pnl `{row['unrealized_pnl']}` edge `{row['fair_value_edge']}`"
            )
    else:
        lines.append("- Hold: none")
    lines.extend(["", "## Recent Worst Closed", ""])
    if snapshot["recent_worst_closed"]:
        for row in snapshot["recent_worst_closed"][:10]:
            lines.append(
                f"- `{row['family']}` `{row['market_slug']}` realized `{row['realized_pnl']}` reason `{row['exit_reason']}`"
            )
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def append_history(path: Path, snapshot: dict[str, Any], *, max_rows: int) -> None:
    existing: list[str] = []
    if path.exists() and path.stat().st_size > 0:
        existing = path.read_text(encoding="utf-8").splitlines()
    existing.append(json.dumps(compact_history_row(snapshot), separators=(",", ":"), sort_keys=True))
    if max_rows > 0 and len(existing) > max_rows:
        existing = existing[-max_rows:]
    path.write_text("\n".join(existing) + "\n", encoding="utf-8")


def compact_history_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    all_pnl = snapshot["pnl"]["all"]
    hold_all_pnl = snapshot["hold_pnl"]["all"]
    taker_active_all = snapshot["taker_pnl"]["active_all"]
    taker_hold_all = snapshot["taker_pnl"]["hold_all"]
    window_key = next(key for key in snapshot["pnl"] if key.startswith("last_"))
    fed_key = next(key for key in snapshot["pnl"] if key.startswith("fed_rates_last_"))
    return {
        "snapshot_ts_utc": snapshot["snapshot_ts_utc"],
        "cycle": snapshot["health"].get("cycle", ""),
        "active_all_pnl": all_pnl["active_total_pnl_usd"],
        "hold_all_pnl": hold_all_pnl["active_total_pnl_usd"],
        "all_hold_minus_active": snapshot["active_vs_hold"]["all_hold_minus_active_usd"],
        "taker_active_all_pnl": taker_active_all["active_total_pnl_usd"],
        "taker_hold_all_pnl": taker_hold_all["active_total_pnl_usd"],
        "taker_all_hold_minus_active": snapshot["taker_active_vs_hold"]["all_hold_minus_active_usd"],
        "active_window_pnl": snapshot["pnl"][window_key]["active_total_pnl_usd"],
        "hold_window_pnl": snapshot["hold_pnl"][window_key]["active_total_pnl_usd"],
        "active_fed_window_pnl": snapshot["pnl"][fed_key]["active_total_pnl_usd"],
        "hold_fed_window_pnl": snapshot["hold_pnl"][fed_key]["active_total_pnl_usd"],
        "active_open_positions": all_pnl["open_positions"],
        "hold_open_positions": hold_all_pnl["open_positions"],
        "open_quotes": all_pnl["open_quotes"],
        "reward_proxy_conservative": snapshot["reward_proxy"]["all_conservative_usd"],
    }


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def is_recent(value: Any, *, now: dt.datetime, hours: float) -> bool:
    parsed = parse_utc(value)
    if parsed is None:
        return False
    return now - parsed <= dt.timedelta(hours=max(float(hours), 0.0))


def parse_utc(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def to_float(value: Any, default: float = math.nan) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def nested_get(payload: dict[str, Any], keys: list[str]) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key, "")
    return current


if __name__ == "__main__":
    raise SystemExit(main())
