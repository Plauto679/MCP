from __future__ import annotations

import csv
import json
import math
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .market_feature_analysis import (
    LoadedFeatureRows,
    compact_numbers,
    group_by_slug,
    median,
    parse_float,
    pct_ratio,
    read_feature_rows,
    timestamp,
)


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    input_dir: Path
    window_seconds: int


@dataclass(frozen=True)
class WindowMicrostructureConfig:
    datasets: tuple[DatasetSpec, ...]
    output_dir: Path
    horizons_s: tuple[float, ...] = (5.0, 20.0, 60.0)
    sample_step_s: float = 5.0


def compare_window_microstructure(config: WindowMicrostructureConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    dataset_reports: list[dict[str, Any]] = []
    event_summaries: list[dict[str, Any]] = []
    elapsed_summaries: list[dict[str, Any]] = []

    for spec in config.datasets:
        loaded = read_feature_rows(spec.input_dir)
        by_slug = group_by_slug(loaded.rows)
        dataset_reports.append(_dataset_health(spec, loaded, by_slug))
        event_summaries.extend(_event_summaries(spec, by_slug, config))
        elapsed_summaries.extend(_elapsed_summaries(spec, loaded.rows))

    outputs = {
        "dataset_health": str(config.output_dir / "dataset_health.csv"),
        "maker_touch_summary": str(config.output_dir / "maker_touch_summary.csv"),
        "elapsed_summary": str(config.output_dir / "elapsed_summary.csv"),
        "report": str(config.output_dir / "report.json"),
    }
    _write_csv(config.output_dir / "dataset_health.csv", dataset_reports)
    _write_csv(config.output_dir / "maker_touch_summary.csv", event_summaries)
    _write_csv(config.output_dir / "elapsed_summary.csv", elapsed_summaries)
    report = {
        "mode": "dry_research_only",
        "datasets": dataset_reports,
        "maker_touch_summary": event_summaries,
        "elapsed_summary": elapsed_summaries,
        "interpretation": _interpret(event_summaries),
        "outputs": outputs,
    }
    (config.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _dataset_health(
    spec: DatasetSpec,
    loaded: LoadedFeatureRows,
    by_slug: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    counts = sorted(len(group) for group in by_slug.values())
    return {
        "dataset": spec.name,
        "input_dir": str(spec.input_dir),
        "window_seconds": spec.window_seconds,
        "files_read": loaded.files_read,
        "files_skipped": loaded.files_skipped,
        "size_mb": round(loaded.total_size_bytes / 1024 / 1024, 3),
        "rows": len(loaded.rows),
        "windows": len(by_slug),
        "rows_per_window_median": median(counts) if counts else 0,
        "first_ts_utc": min((str(row.get("sample_ts_utc") or "") for row in loaded.rows), default=""),
        "last_ts_utc": max((str(row.get("sample_ts_utc") or "") for row in loaded.rows), default=""),
    }


def _event_summaries(
    spec: DatasetSpec,
    by_slug: dict[str, list[dict[str, Any]]],
    config: WindowMicrostructureConfig,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for horizon in config.horizons_s:
        events: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for group in by_slug.values():
            sampled = _sample_group(group, config.sample_step_s)
            times = [timestamp(row) for row in sampled]
            if any(value is None for value in times):
                continue
            usable_times = [float(value) for value in times if value is not None]
            for index, row in enumerate(sampled):
                future_index = bisect_left(usable_times, usable_times[index] + float(horizon))
                if future_index >= len(sampled):
                    continue
                future = sampled[future_index]
                for outcome in ("yes", "no"):
                    for quote_side in ("bid", "ask"):
                        event = _quote_event(row, future, outcome, quote_side)
                        if event:
                            events.setdefault((outcome, quote_side), []).append(event)
        for (outcome, quote_side), group_events in sorted(events.items()):
            summaries.append(_summary_row(spec.name, horizon, outcome, quote_side, group_events))
    return summaries


def _quote_event(
    row: dict[str, Any],
    future: dict[str, Any],
    outcome: str,
    quote_side: str,
) -> dict[str, Any]:
    quote = parse_float(row.get(f"{outcome}_best_{quote_side}"))
    now_mid = parse_float(row.get(f"{outcome}_mid"))
    future_mid = parse_float(future.get(f"{outcome}_mid"))
    if quote is None or now_mid is None or future_mid is None or quote <= 0:
        return {}
    if quote_side == "bid":
        touch_price = parse_float(future.get(f"{outcome}_best_ask"))
        touched = touch_price is not None and touch_price <= quote + 1e-12
        pnl_if_touched = future_mid - quote
        mid_move_for_quote = future_mid - now_mid
    else:
        touch_price = parse_float(future.get(f"{outcome}_best_bid"))
        touched = touch_price is not None and touch_price >= quote - 1e-12
        pnl_if_touched = quote - future_mid
        mid_move_for_quote = now_mid - future_mid
    return {
        "touched": touched,
        "pnl_if_touched": pnl_if_touched if touched else 0.0,
        "mid_move_for_quote": mid_move_for_quote,
        "edge_to_mid": abs(quote - now_mid),
    }


def _summary_row(
    dataset: str,
    horizon_s: float,
    outcome: str,
    quote_side: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    touched = [event for event in events if event["touched"]]
    touch_pnls = compact_numbers(event["pnl_if_touched"] for event in touched)
    moves = compact_numbers(event["mid_move_for_quote"] for event in events)
    adverse_moves = [move for move in moves if move < 0]
    return {
        "dataset": dataset,
        "horizon_s": horizon_s,
        "outcome": outcome,
        "quote_side": quote_side,
        "quote_events": len(events),
        "touched": len(touched),
        "touch_rate": pct_ratio(len(touched), len(events)),
        "avg_pnl_per_touched_share": round(sum(touch_pnls) / len(touch_pnls), 6) if touch_pnls else "",
        "median_pnl_per_touched_share": round(median(touch_pnls), 6) if touch_pnls else "",
        "touch_pnl_sum_one_share": round(sum(touch_pnls), 6) if touch_pnls else 0.0,
        "avg_mid_move_for_quote_cents": round((sum(moves) / len(moves)) * 100.0, 6) if moves else "",
        "adverse_move_rate": pct_ratio(len(adverse_moves), len(moves)),
        "avg_edge_to_mid_cents": round(
            sum(compact_numbers(event["edge_to_mid"] for event in events)) / len(events) * 100.0,
            6,
        ) if events else "",
    }


def _elapsed_summaries(spec: DatasetSpec, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = _elapsed_buckets(spec.window_seconds)
    summaries: list[dict[str, Any]] = []
    for label, start, end in buckets:
        bucket_rows = [
            row
            for row in rows
            if (elapsed := parse_float(row.get("elapsed_s"))) is not None and start <= elapsed < end
        ]
        yes_spreads = compact_numbers(parse_float(row.get("yes_spread")) for row in bucket_rows)
        no_spreads = compact_numbers(parse_float(row.get("no_spread")) for row in bucket_rows)
        maker_edges = compact_numbers(parse_float(row.get("maker_pair_edge")) for row in bucket_rows)
        summaries.append({
            "dataset": spec.name,
            "elapsed_bucket": label,
            "rows": len(bucket_rows),
            "median_yes_spread": round(median(yes_spreads), 6) if yes_spreads else "",
            "median_no_spread": round(median(no_spreads), 6) if no_spreads else "",
            "median_maker_pair_edge": round(median(maker_edges), 6) if maker_edges else "",
            "maker_pair_edge_positive_rate": pct_ratio(sum(1 for edge in maker_edges if edge > 0), len(maker_edges)),
        })
    return summaries


def _sample_group(group: list[dict[str, Any]], step_s: float) -> list[dict[str, Any]]:
    sampled: list[dict[str, Any]] = []
    last_ts: float | None = None
    for row in group:
        row_ts = timestamp(row)
        if row_ts is None:
            continue
        if last_ts is None or row_ts - last_ts >= max(float(step_s), 0.1):
            sampled.append(row)
            last_ts = row_ts
    return sampled


def _elapsed_buckets(window_seconds: int) -> list[tuple[str, float, float]]:
    if window_seconds <= 300:
        return [
            ("0-60", 0.0, 60.0),
            ("60-180", 60.0, 180.0),
            ("180-240", 180.0, 240.0),
            ("240-300", 240.0, 300.0),
        ]
    return [
        ("0-180", 0.0, 180.0),
        ("180-360", 180.0, 360.0),
        ("360-600", 360.0, 600.0),
        ("600-840", 600.0, 840.0),
        ("840-900", 840.0, 900.0),
    ]


def _interpret(rows: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_dataset.setdefault(str(row.get("dataset") or ""), []).append(row)
    for dataset, group in sorted(by_dataset.items()):
        touched = sum(int(row.get("touched") or 0) for row in group)
        events = sum(int(row.get("quote_events") or 0) for row in group)
        pnl = sum(float(row.get("touch_pnl_sum_one_share") or 0.0) for row in group)
        notes.append(
            f"{dataset}: touch_rate={pct_ratio(touched, events):.4f}, "
            f"touch_pnl_sum_one_share={pnl:.4f}"
        )
    return notes


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
