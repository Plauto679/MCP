from __future__ import annotations

import json
import math
from bisect import bisect_left
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

from .fair_value import taker_fee_fraction
from .market_feature_analysis import (
    load_labels,
    parse_float,
    read_feature_rows,
    timestamp,
    window_start_ts,
    yes_mid,
)


@dataclass(frozen=True)
class LateContinuationExecutionConfig:
    feature_dir: Path
    labels_csv: Path
    output_dir: Path
    entry_elapsed_s: tuple[float, ...] = (210.0, 225.0, 240.0, 255.0, 270.0)
    taker_delays_s: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 5.0)
    max_prices: tuple[float, ...] = (0.94, 0.95, 0.96, 0.97, 0.98, 0.99)
    min_mid_edges: tuple[float, ...] = (0.0, 0.05, 0.10, 0.20, 0.30)
    min_confirm_imbalances: tuple[float, ...] = (0.0, 1.0, 1.5)
    max_spreads: tuple[float, ...] = (0.01, 0.02, 0.03, 0.05)
    max_entry_lag_s: float = 2.0
    max_execution_elapsed_s: float = 295.0
    min_top_shares: float = 0.0
    require_uncrossed_books: bool = False
    max_quote_age_ms: float = 0.0
    fee_rate: float = 0.07
    train_fraction: float = 0.70
    min_train_attempts: int = 25
    min_test_attempts: int = 10
    min_train_windows: int = 20
    write_candidates: bool = False


def build_late_continuation_candidates(config: LateContinuationExecutionConfig) -> pd.DataFrame:
    loaded = read_feature_rows(config.feature_dir)
    labels = load_labels(config.labels_csv)
    if not loaded.rows or not labels:
        return pd.DataFrame()

    grouped = _group_rows(loaded.rows)
    ordered_slugs = sorted(
        [slug for slug in grouped if slug in labels],
        key=lambda slug: window_start_ts(slug) or 0,
    )
    train_slugs = set(ordered_slugs[: _split_index(len(ordered_slugs), config.train_fraction)])
    scenario_rows: list[dict[str, Any]] = []

    scenarios = list(product(
        config.entry_elapsed_s,
        config.taker_delays_s,
        config.max_prices,
        config.min_mid_edges,
        config.min_confirm_imbalances,
        config.max_spreads,
    ))
    for slug in ordered_slugs:
        group = grouped[slug]
        times = [timestamp(row) for row in group]
        elapsed = [parse_float(row.get("elapsed_s")) for row in group]
        if any(item is None for item in times) or any(item is None for item in elapsed):
            continue
        numeric_times = [float(item or 0.0) for item in times]
        numeric_elapsed = [float(item or 0.0) for item in elapsed]
        yes_won = int(labels[slug])
        split = "train" if slug in train_slugs else "test"

        for entry_s, delay_s, max_price, min_mid_edge, min_imbalance, max_spread in scenarios:
            decision_index = _first_elapsed_index(numeric_elapsed, entry_s)
            if decision_index is None:
                continue
            decision_row = group[decision_index]
            decision_elapsed = numeric_elapsed[decision_index]
            if decision_elapsed - entry_s > config.max_entry_lag_s:
                continue
            if config.require_uncrossed_books and not books_uncrossed(decision_row):
                continue

            mid = yes_mid(decision_row)
            if mid is None or abs(mid - 0.5) < min_mid_edge:
                continue
            side = "YES" if mid >= 0.5 else "NO"
            decision_price = side_ask(decision_row, side)
            if decision_price is None or not 0.0 < decision_price <= max_price:
                continue
            decision_size = side_ask_size(decision_row, side)
            if config.min_top_shares > 0.0 and (decision_size is None or decision_size < config.min_top_shares):
                continue
            decision_quote_age_ms = side_quote_age_ms(decision_row, side)
            if not quote_age_ok(decision_quote_age_ms, config.max_quote_age_ms):
                continue
            spread = side_spread(decision_row, side)
            if spread is None or spread > max_spread:
                continue
            imbalance = parse_float(decision_row.get("directional_top_imbalance"))
            if not imbalance_confirms(side, imbalance, min_imbalance):
                continue

            execution_index = bisect_left(numeric_times, numeric_times[decision_index] + delay_s)
            executed = False
            execution_price: float | None = None
            execution_elapsed: float | None = None
            if execution_index < len(group):
                execution_row = group[execution_index]
                execution_elapsed = numeric_elapsed[execution_index]
                candidate_price = side_ask(execution_row, side)
                candidate_size = side_ask_size(execution_row, side)
                execution_quote_age_ms = side_quote_age_ms(execution_row, side)
                if (
                    (not config.require_uncrossed_books or books_uncrossed(execution_row))
                    and
                    candidate_price is not None
                    and 0.0 < candidate_price <= max_price
                    and (
                        config.min_top_shares <= 0.0
                        or (candidate_size is not None and candidate_size >= config.min_top_shares)
                    )
                    and quote_age_ok(execution_quote_age_ms, config.max_quote_age_ms)
                    and execution_elapsed <= config.max_execution_elapsed_s
                ):
                    executed = True
                    execution_price = candidate_price
                    execution_size = candidate_size
                else:
                    execution_size = None
                    execution_quote_age_ms = None
            else:
                execution_size = None
                execution_quote_age_ms = None

            side_won = yes_won if side == "YES" else 1 - yes_won
            pnl_per_usd = (
                taker_pnl_per_usd(side_won=bool(side_won), price=float(execution_price), fee_rate=config.fee_rate)
                if executed and execution_price is not None
                else 0.0
            )
            scenario_rows.append({
                "scenario_id": scenario_id(entry_s, delay_s, max_price, min_mid_edge, min_imbalance, max_spread),
                "slug": slug,
                "split": split,
                "entry_elapsed_s": entry_s,
                "delay_s": delay_s,
                "max_price": max_price,
                "min_mid_edge": min_mid_edge,
                "min_confirm_imbalance": min_imbalance,
                "max_spread": max_spread,
                "decision_sample_ts_utc": decision_row.get("sample_ts_utc", ""),
                "decision_elapsed_s": decision_elapsed,
                "execution_elapsed_s": execution_elapsed if execution_elapsed is not None else "",
                "side": side,
                "yes_won": yes_won,
                "side_won": int(side_won),
                "yes_mid": round(float(mid), 6),
                "decision_price": round(float(decision_price), 6),
                "decision_size": round(float(decision_size), 6) if decision_size is not None else "",
                "decision_quote_age_ms": round(float(decision_quote_age_ms), 6)
                if decision_quote_age_ms is not None
                else "",
                "execution_price": round(float(execution_price), 6) if execution_price is not None else "",
                "execution_size": round(float(execution_size), 6) if execution_size is not None else "",
                "execution_quote_age_ms": round(float(execution_quote_age_ms), 6)
                if execution_quote_age_ms is not None
                else "",
                "spread": round(float(spread), 6),
                "directional_top_imbalance": round(float(imbalance), 6) if imbalance is not None else "",
                "executed": int(executed),
                "pnl_per_attempt_usd": round(float(pnl_per_usd), 8),
            })

    if not scenario_rows:
        return pd.DataFrame()
    return pd.DataFrame(scenario_rows)


def summarize_late_continuation_execution(
    candidates: pd.DataFrame,
    config: LateContinuationExecutionConfig,
) -> dict[str, pd.DataFrame]:
    if candidates.empty:
        empty = pd.DataFrame()
        return {"split_summary": empty, "train_test": empty, "selected_train": empty}

    split_summary = _summarize(candidates, [
        "split",
        "scenario_id",
        "entry_elapsed_s",
        "delay_s",
        "max_price",
        "min_mid_edge",
        "min_confirm_imbalance",
        "max_spread",
    ])
    all_summary = _summarize(
        candidates.assign(split="all"),
        [
            "split",
            "scenario_id",
            "entry_elapsed_s",
            "delay_s",
            "max_price",
            "min_mid_edge",
            "min_confirm_imbalance",
            "max_spread",
        ],
    )
    split_summary = pd.concat([split_summary, all_summary], ignore_index=True, sort=False)

    train = split_summary[split_summary["split"] == "train"].copy()
    test = split_summary[split_summary["split"] == "test"].copy()
    merged = train.merge(
        test,
        on=[
            "scenario_id",
            "entry_elapsed_s",
            "delay_s",
            "max_price",
            "min_mid_edge",
            "min_confirm_imbalance",
            "max_spread",
        ],
        how="inner",
        suffixes=("_train", "_test"),
    )
    selected = merged[
        (merged["attempts_train"] >= int(config.min_train_attempts))
        & (merged["windows_train"] >= int(config.min_train_windows))
        & (merged["attempts_test"] >= int(config.min_test_attempts))
        & (merged["avg_pnl_per_attempt_usd_train"] > 0.0)
    ].copy()
    if not selected.empty:
        selected["test_minus_train_pnl"] = (
            selected["avg_pnl_per_attempt_usd_test"] - selected["avg_pnl_per_attempt_usd_train"]
        )
        selected = selected.sort_values(
            ["avg_pnl_per_attempt_usd_test", "attempts_test", "avg_pnl_per_attempt_usd_train"],
            ascending=[False, False, False],
        )
    split_summary = split_summary.sort_values(
        ["split", "avg_pnl_per_attempt_usd", "attempts"],
        ascending=[True, False, False],
    )
    return {"split_summary": split_summary, "train_test": merged, "selected_train": selected}


def run_late_continuation_execution_scanner(config: LateContinuationExecutionConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    candidates = build_late_continuation_candidates(config)
    summaries = summarize_late_continuation_execution(candidates, config)
    for name, frame in summaries.items():
        frame.to_csv(config.output_dir / f"{name}.csv", index=False)
    if config.write_candidates and not candidates.empty:
        candidates.to_csv(config.output_dir / "candidate_trades.csv", index=False)

    report = {
        "candidate_rows": int(len(candidates)),
        "candidate_windows": int(candidates["slug"].nunique()) if not candidates.empty else 0,
        "feature_dir": str(config.feature_dir),
        "labels_csv": str(config.labels_csv),
        "fee_rate": config.fee_rate,
        "train_fraction": config.train_fraction,
        "fill_model": {
            "min_top_shares": config.min_top_shares,
            "require_uncrossed_books": config.require_uncrossed_books,
            "max_quote_age_ms": config.max_quote_age_ms,
        },
        "outputs": {
            "split_summary": str(config.output_dir / "split_summary.csv"),
            "train_test": str(config.output_dir / "train_test.csv"),
            "selected_train": str(config.output_dir / "selected_train.csv"),
        },
    }
    if not summaries["selected_train"].empty:
        preview_cols = [
            "scenario_id",
            "attempts_train",
            "windows_train",
            "executed_train",
            "execution_rate_train",
            "accuracy_executed_train",
            "avg_execution_price_train",
            "avg_pnl_per_attempt_usd_train",
            "attempts_test",
            "windows_test",
            "executed_test",
            "execution_rate_test",
            "accuracy_executed_test",
            "avg_execution_price_test",
            "avg_pnl_per_attempt_usd_test",
        ]
        report["selected_preview"] = (
            summaries["selected_train"][[col for col in preview_cols if col in summaries["selected_train"].columns]]
            .head(20)
            .to_dict(orient="records")
        )
    with (config.output_dir / "report.json").open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    return report


def _summarize(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    df = df.copy()
    for column in [
        "executed",
        "decision_price",
        "execution_price",
        "decision_size",
        "execution_size",
        "decision_quote_age_ms",
        "execution_quote_age_ms",
        "side_won",
        "pnl_per_attempt_usd",
    ]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    grouped = df.groupby(group_cols, dropna=False)
    summary = grouped.agg(
        attempts=("slug", "size"),
        windows=("slug", "nunique"),
        executed=("executed", "sum"),
        avg_decision_price=("decision_price", "mean"),
        avg_execution_price=("execution_price", "mean"),
        avg_decision_size=("decision_size", "mean"),
        avg_execution_size=("execution_size", "mean"),
        avg_decision_quote_age_ms=("decision_quote_age_ms", "mean"),
        avg_execution_quote_age_ms=("execution_quote_age_ms", "mean"),
        accuracy_attempts=("side_won", "mean"),
        avg_pnl_per_attempt_usd=("pnl_per_attempt_usd", "mean"),
        total_pnl_one_usd_attempts=("pnl_per_attempt_usd", "sum"),
    ).reset_index()
    summary["execution_rate"] = summary["executed"] / summary["attempts"].replace(0, pd.NA)
    executed = df[df["executed"].astype(bool)]
    if executed.empty:
        summary["accuracy_executed"] = pd.NA
        summary["avg_pnl_per_executed_usd"] = pd.NA
    else:
        executed_summary = executed.groupby(group_cols, dropna=False).agg(
            accuracy_executed=("side_won", "mean"),
            avg_pnl_per_executed_usd=("pnl_per_attempt_usd", "mean"),
        ).reset_index()
        summary = summary.merge(executed_summary, on=group_cols, how="left")
    return summary


def side_ask(row: dict[str, Any], side: str) -> float | None:
    return parse_float(row.get("yes_best_ask" if side == "YES" else "no_best_ask"))


def side_ask_size(row: dict[str, Any], side: str) -> float | None:
    return parse_float(row.get("yes_best_ask_size" if side == "YES" else "no_best_ask_size"))


def side_spread(row: dict[str, Any], side: str) -> float | None:
    return parse_float(row.get("yes_spread" if side == "YES" else "no_spread"))


def side_quote_age_ms(row: dict[str, Any], side: str) -> float | None:
    return parse_float(row.get("yes_quote_age_ms" if side == "YES" else "no_quote_age_ms"))


def quote_age_ok(value: float | None, max_quote_age_ms: float) -> bool:
    limit = max(float(max_quote_age_ms), 0.0)
    if limit <= 0.0:
        return True
    return value is not None and math.isfinite(float(value)) and float(value) <= limit


def books_uncrossed(row: dict[str, Any]) -> bool:
    yes_bid = parse_float(row.get("yes_best_bid"))
    yes_ask = parse_float(row.get("yes_best_ask"))
    no_bid = parse_float(row.get("no_best_bid"))
    no_ask = parse_float(row.get("no_best_ask"))
    values = (yes_bid, yes_ask, no_bid, no_ask)
    if any(value is None or not math.isfinite(float(value)) for value in values):
        return False
    return (
        0.0 < float(yes_bid) <= float(yes_ask) <= 1.0
        and 0.0 < float(no_bid) <= float(no_ask) <= 1.0
    )


def imbalance_confirms(side: str, imbalance: float | None, min_imbalance: float) -> bool:
    threshold = max(float(min_imbalance), 0.0)
    if threshold <= 0.0:
        return True
    if imbalance is None or not math.isfinite(float(imbalance)):
        return False
    return imbalance >= threshold if side == "YES" else imbalance <= -threshold


def taker_pnl_per_usd(side_won: bool, price: float, fee_rate: float) -> float:
    fee = taker_fee_fraction(float(price), fee_rate)
    if side_won:
        return (1.0 / float(price)) - 1.0 - fee
    return -1.0 - fee


def scenario_id(
    entry_s: float,
    delay_s: float,
    max_price: float,
    min_mid_edge: float,
    min_imbalance: float,
    max_spread: float,
) -> str:
    return (
        f"t{_key(entry_s)}_d{_key(delay_s)}_px{_key(max_price)}_"
        f"edge{_key(min_mid_edge)}_imb{_key(min_imbalance)}_spr{_key(max_spread)}"
    )


def _key(value: float) -> str:
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text.replace(".", "p")


def _group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        slug = str(row.get("slug") or "")
        if slug:
            grouped.setdefault(slug, []).append(row)
    for group in grouped.values():
        group.sort(key=lambda row: timestamp(row) or 0.0)
    return grouped


def _first_elapsed_index(elapsed: list[float], target: float) -> int | None:
    index = bisect_left(elapsed, float(target))
    return index if index < len(elapsed) else None


def _split_index(count: int, train_fraction: float) -> int:
    if count <= 1:
        return count
    fraction = min(max(float(train_fraction), 0.1), 0.9)
    index = int(count * fraction)
    return max(1, min(index, count - 1))
