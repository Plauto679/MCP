from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT_DIR / "data"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "research" / "core_maker"


def read_csv_if_exists(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False, **kwargs)


def to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def json_safe(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def merge_nearest_signal(entries: pd.DataFrame, signals_path: Path) -> pd.DataFrame:
    if entries.empty or not signals_path.exists():
        return entries
    usecols = [
        "sample_ts_utc",
        "slug",
        "elapsed_s",
        "opening_price",
        "latest_price",
        "delta_usd",
        "delta_bps",
        "confidence",
        "continuation_probability",
        "contrarian_probability",
        "fair_yes",
        "fair_no",
        "market_fair_yes",
        "decision_tactic",
    ]
    try:
        signals = read_csv_if_exists(signals_path, usecols=lambda col: col in usecols)
    except ValueError:
        return entries
    if signals.empty or "slug" not in signals.columns or "elapsed_s" not in signals.columns:
        return entries
    signals = to_numeric(
        signals,
        [
            "elapsed_s",
            "opening_price",
            "latest_price",
            "delta_usd",
            "delta_bps",
            "confidence",
            "continuation_probability",
            "contrarian_probability",
            "fair_yes",
            "fair_no",
            "market_fair_yes",
        ],
    )
    entries = entries.copy()
    entries["_entry_row"] = np.arange(len(entries))
    entries["elapsed_s"] = pd.to_numeric(entries.get("elapsed_s"), errors="coerce")
    merged_frames: list[pd.DataFrame] = []
    for slug, entry_group in entries.groupby("slug", dropna=False):
        signal_group = signals[signals["slug"] == slug].copy()
        if signal_group.empty:
            merged_frames.append(entry_group)
            continue
        signal_group = signal_group.sort_values("elapsed_s")
        entry_group = entry_group.sort_values("elapsed_s")
        merged = pd.merge_asof(
            entry_group,
            signal_group,
            on="elapsed_s",
            direction="nearest",
            tolerance=2.5,
            suffixes=("", "_signal"),
        )
        merged_frames.append(merged)
    result = pd.concat(merged_frames, ignore_index=True)
    result = result.sort_values("_entry_row").drop(columns=["_entry_row"], errors="ignore")
    return result


def aggregate_trades(trades: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    df = trades.copy()
    grouped = (
        df.groupby(group_cols, dropna=False)
        .agg(
            entries=("result", "size"),
            windows=("slug", "nunique"),
            wins=("is_win", "sum"),
            losses=("is_loss", "sum"),
            pnl_usd=("pnl_usd", "sum"),
            stake_usd=("stake_usd", "sum"),
            fees_usd=("fee_usd", "sum"),
            avg_price=("entry_price", "mean"),
            median_price=("entry_price", "median"),
            avg_fee_per_usd=("fee_per_usd", "mean"),
            avg_breakeven_probability=("breakeven_probability", "mean"),
            avg_ev_per_usd=("ev_per_usd", "mean"),
            avg_fair_probability=("fair_probability", "mean"),
            avg_model_p=("entry_model_win_probability", "mean"),
            avg_elapsed_s=("elapsed_s", "mean"),
            avg_delta_bps=("delta_bps", "mean"),
            avg_abs_delta_bps=("abs_delta_bps", "mean"),
            contrarian_entries=("is_contrarian", "sum"),
            continuation_entries=("is_continuation", "sum"),
        )
        .reset_index()
    )
    grouped["winrate"] = grouped["wins"] / grouped["entries"].replace(0, np.nan)
    grouped["roi_on_stake"] = grouped["pnl_usd"] / grouped["stake_usd"].replace(0, np.nan)
    grouped["realized_edge_vs_breakeven_pp"] = (
        grouped["winrate"] - grouped["avg_breakeven_probability"]
    ) * 100.0
    grouped["one_win_two_losses_per_$"] = (
        (1.0 / grouped["avg_price"].replace(0, np.nan) - 1.0 - grouped["avg_fee_per_usd"])
        + 2.0 * (-1.0 - grouped["avg_fee_per_usd"])
    )
    return grouped.sort_values(["pnl_usd", "entries"], ascending=[False, False])


def loss_streaks(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for key, group in [("overall", trades)] + list(trades.groupby("tactic", dropna=False)):
        tactic = str(key)
        streak = 0
        max_streak = 0
        streak_loss = 0.0
        max_streak_loss = 0.0
        for _, row in group.sort_values("event_ts_utc").iterrows():
            if row.get("result") == "LOSS":
                streak += 1
                streak_loss += abs(float(row.get("pnl_usd") or 0.0))
                if streak > max_streak:
                    max_streak = streak
                    max_streak_loss = streak_loss
            elif row.get("result") == "WIN":
                streak = 0
                streak_loss = 0.0
        rows.append({
            "tactic": tactic,
            "longest_loss_streak": max_streak,
            "loss_usd_during_longest_streak": round(max_streak_loss, 4),
        })
    return pd.DataFrame(rows)


def maker_summary(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if events.empty:
        return pd.DataFrame(), pd.DataFrame()
    df = events.copy()
    df["is_created"] = df["event"].astype(str).str.contains("created", case=False, na=False)
    df["is_filled"] = df["event"].astype(str).str.contains("filled|partial", case=False, na=False)
    df["is_expired"] = df["event"].astype(str).str.contains("expired", case=False, na=False)
    summary = (
        df.groupby(["dry_run", "tactic"], dropna=False)
        .agg(
            events=("event", "size"),
            created=("is_created", "sum"),
            filled=("is_filled", "sum"),
            expired=("is_expired", "sum"),
            avg_maker_price=("maker_price", "mean"),
            avg_best_ask=("best_ask", "mean"),
            avg_ev_per_usd=("ev_per_usd", "mean"),
            avg_elapsed_s=("elapsed_s", "mean"),
            total_stake_seen=("stake_usd", "sum"),
        )
        .reset_index()
    )
    summary["fill_rate_vs_created"] = summary["filled"] / summary["created"].replace(0, np.nan)

    by_price = df.copy()
    by_price["maker_price_bucket"] = pd.cut(
        pd.to_numeric(by_price.get("maker_price"), errors="coerce"),
        bins=[0, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 1.0],
        include_lowest=True,
    )
    price_summary = (
        by_price.groupby(["tactic", "maker_price_bucket"], observed=False, dropna=False)
        .agg(
            events=("event", "size"),
            created=("is_created", "sum"),
            filled=("is_filled", "sum"),
            expired=("is_expired", "sum"),
            avg_elapsed_s=("elapsed_s", "mean"),
        )
        .reset_index()
    )
    price_summary["fill_rate_vs_created"] = price_summary["filled"] / price_summary["created"].replace(0, np.nan)
    return summary, price_summary


def prepare_trades(data_dir: Path) -> pd.DataFrame:
    entries = read_csv_if_exists(data_dir / "fair_value_entries.csv")
    outcomes = read_csv_if_exists(data_dir / "fair_value_outcomes.csv")
    if outcomes.empty:
        return pd.DataFrame()

    numeric_cols = [
        "stake_usd",
        "entry_price",
        "shares",
        "fee_usd",
        "payout_usd",
        "pnl_usd",
        "fair_probability",
        "edge_probability",
        "ev_per_usd",
        "entry_model_win_probability",
        "entry_model_min_probability",
        "elapsed_s",
        "price_delta",
        "opening_price",
        "closing_price",
        "break_even_price",
        "max_acceptable_price",
        "price_margin",
        "continuation_probability",
        "abs_delta_bps",
    ]
    entries = to_numeric(entries, numeric_cols)
    outcomes = to_numeric(outcomes, numeric_cols)
    if not entries.empty and "entry_id" in entries.columns and "entry_id" in outcomes.columns:
        extra_cols = [
            col for col in [
                "slug",
                "entry_id",
                "elapsed_s",
                "break_even_price",
                "max_acceptable_price",
                "price_margin",
                "continuation_probability",
                "abs_delta_bps",
            ]
            if col in entries.columns
        ]
        outcomes = outcomes.merge(
            entries[extra_cols].drop_duplicates(["slug", "entry_id"]),
            on=["slug", "entry_id"],
            how="left",
            suffixes=("", "_entry"),
        )
        for col in ["elapsed_s", "break_even_price", "max_acceptable_price", "price_margin", "continuation_probability", "abs_delta_bps"]:
            entry_col = f"{col}_entry"
            if entry_col in outcomes.columns:
                outcomes[col] = outcomes[col].combine_first(outcomes[entry_col])
                outcomes = outcomes.drop(columns=[entry_col])

    trades = outcomes[outcomes["result"].isin(["WIN", "LOSS"])].copy()
    trades = merge_nearest_signal(trades, data_dir / "fair_value_signals.csv")
    trades = to_numeric(
        trades,
        numeric_cols + ["delta_bps", "delta_usd", "confidence", "continuation_probability_signal", "contrarian_probability"],
    )
    trades["is_win"] = (trades["result"] == "WIN").astype(int)
    trades["is_loss"] = (trades["result"] == "LOSS").astype(int)
    trades["fee_per_usd"] = trades["fee_usd"] / trades["stake_usd"].replace(0, np.nan)
    trades["breakeven_probability"] = trades["entry_price"] * (1.0 + trades["fee_per_usd"].fillna(0.0))
    if "delta_bps" in trades.columns:
        computed_abs_delta = trades["delta_bps"].abs()
        if "abs_delta_bps" in trades.columns:
            trades["abs_delta_bps"] = computed_abs_delta.combine_first(trades["abs_delta_bps"])
        else:
            trades["abs_delta_bps"] = computed_abs_delta
        trades["current_side"] = np.where(trades["delta_bps"] > 0, "up", np.where(trades["delta_bps"] < 0, "down", "flat"))
        trades["is_contrarian"] = (
            ((trades["delta_bps"] > 0) & (trades["direction"] == "No"))
            | ((trades["delta_bps"] < 0) & (trades["direction"] == "Yes"))
        ).astype(int)
        trades["is_continuation"] = (
            ((trades["delta_bps"] > 0) & (trades["direction"] == "Yes"))
            | ((trades["delta_bps"] < 0) & (trades["direction"] == "No"))
        ).astype(int)
    else:
        trades["current_side"] = ""
        trades["is_contrarian"] = 0
        trades["is_continuation"] = 0
    trades["price_bucket"] = pd.cut(
        trades["entry_price"],
        bins=[0, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.75, 1.0],
        include_lowest=True,
    )
    trades["elapsed_bucket"] = pd.cut(
        trades["elapsed_s"],
        bins=[0, 30, 60, 120, 180, 220, 240, 260, 285, 300],
        include_lowest=True,
    )
    return trades


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Fair Value core/maker/giro trade records.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    trades = prepare_trades(data_dir)
    maker_events = read_csv_if_exists(data_dir / "fair_value_maker_events.csv")
    maker_events = to_numeric(
        maker_events,
        ["maker_price", "best_ask", "elapsed_s", "stake_usd", "fair_probability", "edge_probability", "ev_per_usd"],
    )

    tactic_summary = aggregate_trades(trades, ["dry_run", "tactic"])
    route_summary = aggregate_trades(trades, ["dry_run", "tactic", "route"])
    price_summary = aggregate_trades(trades, ["dry_run", "tactic", "price_bucket"])
    direction_summary = aggregate_trades(trades, ["dry_run", "tactic", "is_contrarian", "is_continuation"])
    elapsed_summary = aggregate_trades(trades, ["dry_run", "tactic", "elapsed_bucket"])
    streak_summary = loss_streaks(trades)
    maker_event_summary, maker_price_summary = maker_summary(maker_events)

    trades.to_csv(output_dir / "closed_trades_enriched.csv", index=False)
    tactic_summary.to_csv(output_dir / "tactic_summary.csv", index=False)
    route_summary.to_csv(output_dir / "route_summary.csv", index=False)
    price_summary.to_csv(output_dir / "price_bucket_summary.csv", index=False)
    direction_summary.to_csv(output_dir / "direction_summary.csv", index=False)
    elapsed_summary.to_csv(output_dir / "elapsed_summary.csv", index=False)
    streak_summary.to_csv(output_dir / "streak_summary.csv", index=False)
    maker_event_summary.to_csv(output_dir / "maker_event_summary.csv", index=False)
    maker_price_summary.to_csv(output_dir / "maker_price_summary.csv", index=False)

    report = {
        "closed_trades": int(len(trades)),
        "first_trade_utc": json_safe(pd.to_datetime(trades["event_ts_utc"], errors="coerce", utc=True).min()) if not trades.empty else None,
        "last_trade_utc": json_safe(pd.to_datetime(trades["event_ts_utc"], errors="coerce", utc=True).max()) if not trades.empty else None,
        "total_pnl_usd": float(trades["pnl_usd"].sum()) if not trades.empty else 0.0,
        "winrate": float((trades["result"] == "WIN").mean()) if not trades.empty else None,
        "maker_events": int(len(maker_events)),
        "outputs": {
            "closed_trades_enriched": str(output_dir / "closed_trades_enriched.csv"),
            "tactic_summary": str(output_dir / "tactic_summary.csv"),
            "route_summary": str(output_dir / "route_summary.csv"),
            "price_bucket_summary": str(output_dir / "price_bucket_summary.csv"),
            "direction_summary": str(output_dir / "direction_summary.csv"),
            "elapsed_summary": str(output_dir / "elapsed_summary.csv"),
            "streak_summary": str(output_dir / "streak_summary.csv"),
            "maker_event_summary": str(output_dir / "maker_event_summary.csv"),
            "maker_price_summary": str(output_dir / "maker_price_summary.csv"),
        },
    }
    (output_dir / "core_maker_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[core-maker] trades={report['closed_trades']} pnl=${report['total_pnl_usd']:.2f} winrate={report['winrate']}")
    if not tactic_summary.empty:
        print(tactic_summary[["dry_run", "tactic", "entries", "wins", "losses", "winrate", "pnl_usd", "roi_on_stake"]].to_string(index=False))
    if not maker_event_summary.empty:
        print(maker_event_summary[["dry_run", "tactic", "created", "filled", "expired", "fill_rate_vs_created"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
