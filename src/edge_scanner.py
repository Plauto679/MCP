from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .fair_value import clamp, taker_breakeven_probability, taker_fee_fraction


SLUG_RE = re.compile(r"btc-updown-5m-(\d+)$")
DEFAULT_FEE_RATE = 0.07
SIGNAL_COLUMNS = [
    "sample_ts_utc",
    "slug",
    "elapsed_s",
    "yes_best_bid",
    "yes_best_ask",
    "yes_best_bid_size",
    "yes_best_ask_size",
    "yes_spread",
    "yes_mid",
    "yes_bid_depth_40_60",
    "yes_ask_depth_40_60",
    "no_best_bid",
    "no_best_ask",
    "no_best_bid_size",
    "no_best_ask_size",
    "no_spread",
    "no_mid",
    "no_bid_depth_40_60",
    "no_ask_depth_40_60",
    "opening_price",
    "latest_price",
    "fair_yes",
    "fair_no",
    "raw_fair_yes",
    "raw_fair_no",
    "market_fair_yes",
    "delta_bps",
    "confidence",
    "continuation_probability",
    "contrarian_probability",
    "kalman_delta_bps",
    "kalman_velocity_bps_per_min",
    "kalman_projected_delta_bps",
    "kalman_residual_bps",
    "kalman_abs_residual_bps",
    "kalman_uncertainty_bps",
    "kalman_trend_agreement",
]


@dataclass(frozen=True)
class ScannerConfig:
    data_dir: Path
    output_dir: Path
    start_utc: pd.Timestamp | None = None
    end_utc: pd.Timestamp | None = None
    fee_rate: float = DEFAULT_FEE_RATE
    min_bucket_samples: int = 80
    min_train_samples: int = 120
    min_bucket_windows: int = 20
    write_candidates: bool = False
    max_signal_rows: int = 0


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def parse_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    try:
        timestamp = pd.Timestamp(value)
    except Exception:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def slug_start_ts(slug: Any) -> int | None:
    match = SLUG_RE.search(str(slug or ""))
    if not match:
        return None
    return int(match.group(1))


def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, low_memory=False)


def load_window_labels(data_dir: Path) -> pd.DataFrame:
    labels = read_csv_if_exists(data_dir / "fair_value_window_labels.csv")
    outcomes = read_csv_if_exists(data_dir / "fair_value_outcomes.csv")
    frames: list[pd.DataFrame] = []
    if not labels.empty:
        frames.append(labels.copy())
    if not outcomes.empty and {"slug", "opening_price", "closing_price"}.issubset(outcomes.columns):
        temp = outcomes[["slug", "opening_price", "closing_price", "price_delta", "settlement_source"]].copy()
        temp["opening_price"] = pd.to_numeric(temp["opening_price"], errors="coerce")
        temp["closing_price"] = pd.to_numeric(temp["closing_price"], errors="coerce")
        temp = temp.dropna(subset=["slug", "opening_price", "closing_price"])
        if not temp.empty:
            temp["window_start_ts"] = temp["slug"].map(slug_start_ts)
            temp["price_delta"] = temp["closing_price"] - temp["opening_price"]
            temp["yes_won"] = (temp["price_delta"] > 0).astype(int)
            temp["label_source"] = temp["settlement_source"].fillna("outcomes")
            frames.append(temp[[
                "slug",
                "window_start_ts",
                "opening_price",
                "closing_price",
                "price_delta",
                "yes_won",
                "label_source",
            ]])
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True, sort=False)
    result["window_start_ts"] = pd.to_numeric(result.get("window_start_ts"), errors="coerce")
    result["opening_price"] = pd.to_numeric(result.get("opening_price"), errors="coerce")
    result["closing_price"] = pd.to_numeric(result.get("closing_price"), errors="coerce")
    result["price_delta"] = pd.to_numeric(result.get("price_delta"), errors="coerce")
    result["yes_won"] = pd.to_numeric(result.get("yes_won"), errors="coerce")
    result = result.dropna(subset=["slug", "yes_won"])
    result = result.drop_duplicates("slug", keep="last")
    return result


def _iter_signal_rows(path: Path, start_utc: pd.Timestamp | None, end_utc: pd.Timestamp | None) -> Iterable[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            timestamp = parse_timestamp(row.get("sample_ts_utc"))
            if timestamp is None:
                continue
            if start_utc is not None and timestamp < start_utc:
                continue
            if end_utc is not None and timestamp > end_utc:
                continue
            filtered = {column: row.get(column, "") for column in SIGNAL_COLUMNS}
            filtered["sample_ts_utc"] = timestamp.isoformat()
            yield filtered


def _side_price(row: dict[str, Any], side: str, route: str) -> float:
    prefix = "yes" if side == "Yes" else "no"
    key = f"{prefix}_best_ask" if route == "taker" else f"{prefix}_best_bid"
    return finite_float(row.get(key))


def _side_fair_probability(row: dict[str, Any], side: str) -> float:
    value = finite_float(row.get("fair_yes"), 0.5)
    if side == "No":
        value = finite_float(row.get("fair_no"), 1.0 - value)
    return clamp(value, 0.001, 0.999)


def _side_market_probability(row: dict[str, Any], side: str) -> float:
    market_yes = finite_float(row.get("market_fair_yes"), math.nan)
    if not math.isfinite(market_yes) or market_yes <= 0:
        yes_mid = finite_float(row.get("yes_mid"))
        no_mid = finite_float(row.get("no_mid"))
        if yes_mid > 0 and no_mid > 0:
            market_yes = yes_mid / (yes_mid + no_mid)
        elif yes_mid > 0:
            market_yes = yes_mid
        elif no_mid > 0:
            market_yes = 1.0 - no_mid
        else:
            market_yes = 0.5
    return clamp(market_yes if side == "Yes" else 1.0 - market_yes, 0.001, 0.999)


def _side_relation(delta_bps: float, side: str) -> str:
    if abs(delta_bps) < 0.05:
        return "neutral"
    current_side = "Yes" if delta_bps > 0 else "No"
    return "continuation" if side == current_side else "reversal"


def _archetype(elapsed_s: float, abs_delta_bps: float, price: float, relation: str) -> str:
    if relation == "continuation" and elapsed_s >= 240.0 and abs_delta_bps >= 5.0:
        return "late_continuation"
    if relation == "continuation" and elapsed_s >= 220.0 and abs_delta_bps >= 1.0 and price <= 0.90:
        return "momentum"
    if relation == "reversal" and price <= 0.42:
        return "cheap_reversal"
    if relation == "reversal" and 0.44 <= price <= 0.55:
        return "core_reversal"
    if abs_delta_bps <= 2.0 and 0.45 <= price <= 0.55:
        return "neutral_mid"
    return "generic"


def _bucket(value: float, edges: list[float], labels: list[str]) -> str:
    for index, edge in enumerate(edges):
        if value < edge:
            return labels[index]
    return labels[-1]


def _elapsed_bucket(elapsed_s: float) -> str:
    return _bucket(
        elapsed_s,
        [60, 120, 180, 220, 240, 260, 280, 300],
        ["0-60", "60-120", "120-180", "180-220", "220-240", "240-260", "260-280", "280-300", "300+"],
    )


def _price_bucket(price: float) -> str:
    return _bucket(
        price,
        [0.20, 0.30, 0.40, 0.46, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90],
        ["0.01-0.20", "0.20-0.30", "0.30-0.40", "0.40-0.46", "0.46-0.50", "0.50-0.55", "0.55-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-0.99"],
    )


def _delta_bucket(abs_delta_bps: float) -> str:
    return _bucket(
        abs_delta_bps,
        [1, 2, 4, 6, 8, 12, 20, 40],
        ["0-1", "1-2", "2-4", "4-6", "6-8", "8-12", "12-20", "20-40", "40+"],
    )


def _confidence_bucket(confidence: float) -> str:
    return _bucket(
        confidence,
        [0.1, 0.25, 0.5, 0.75],
        ["0-0.10", "0.10-0.25", "0.25-0.50", "0.50-0.75", "0.75-1.00"],
    )


def _realized_pnl_per_usd(side_won: int, route: str, price: float, fee_rate: float) -> float:
    fee_fraction = 0.0 if route == "maker" else taker_fee_fraction(price, fee_rate)
    if int(side_won) == 1:
        return (1.0 / price) - 1.0 - fee_fraction
    return -1.0 - fee_fraction


def build_candidate_dataset(config: ScannerConfig) -> pd.DataFrame:
    labels = load_window_labels(config.data_dir)
    if labels.empty:
        return pd.DataFrame()
    labels_by_slug = {
        str(row["slug"]): row
        for _, row in labels.iterrows()
        if not pd.isna(row.get("yes_won"))
    }
    signal_path = config.data_dir / "fair_value_signals.csv"
    if not signal_path.exists():
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    seen_signals = 0
    for signal in _iter_signal_rows(signal_path, config.start_utc, config.end_utc):
        label = labels_by_slug.get(str(signal.get("slug") or ""))
        if label is None:
            continue
        seen_signals += 1
        if config.max_signal_rows and seen_signals > config.max_signal_rows:
            break

        yes_won = int(float(label["yes_won"]))
        elapsed_s = finite_float(signal.get("elapsed_s"))
        delta_bps = finite_float(signal.get("delta_bps"))
        abs_delta_bps = abs(delta_bps)
        confidence = finite_float(signal.get("confidence"))
        continuation_probability = clamp(finite_float(signal.get("continuation_probability"), 0.5), 0.001, 0.999)
        sample_ts = signal["sample_ts_utc"]
        slug = str(signal.get("slug") or "")
        window_start_ts = slug_start_ts(slug)

        for side in ("Yes", "No"):
            side_won = yes_won if side == "Yes" else 1 - yes_won
            relation = _side_relation(delta_bps, side)
            side_fair = _side_fair_probability(signal, side)
            side_market = _side_market_probability(signal, side)
            side_continuation = continuation_probability
            if relation == "reversal":
                side_continuation = 1.0 - continuation_probability
            elif relation == "neutral":
                side_continuation = 0.5

            for route in ("taker", "maker"):
                price = _side_price(signal, side, route)
                if price <= 0.01 or price >= 0.99:
                    continue
                fee_fraction = 0.0 if route == "maker" else taker_fee_fraction(price, config.fee_rate)
                break_even = price if route == "maker" else taker_breakeven_probability(price, config.fee_rate)
                realized_pnl = _realized_pnl_per_usd(side_won, route, price, config.fee_rate)
                model_edge = side_fair - break_even
                market_edge = side_market - break_even
                archetype = _archetype(elapsed_s, abs_delta_bps, price, relation)
                rows.append({
                    "sample_ts_utc": sample_ts,
                    "slug": slug,
                    "window_start_ts": window_start_ts,
                    "side": side,
                    "route": route,
                    "price": round(price, 6),
                    "fee_fraction": round(fee_fraction, 8),
                    "break_even_probability": round(break_even, 8),
                    "side_won": side_won,
                    "realized_pnl_per_usd": round(realized_pnl, 8),
                    "elapsed_s": round(elapsed_s, 3),
                    "delta_bps": round(delta_bps, 6),
                    "abs_delta_bps": round(abs_delta_bps, 6),
                    "relation": relation,
                    "archetype": archetype,
                    "fair_probability": round(side_fair, 8),
                    "market_probability": round(side_market, 8),
                    "continuation_probability": round(side_continuation, 8),
                    "confidence": round(confidence, 8),
                    "model_edge": round(model_edge, 8),
                    "market_edge": round(market_edge, 8),
                    "elapsed_bucket": _elapsed_bucket(elapsed_s),
                    "price_bucket": _price_bucket(price),
                    "delta_bucket": _delta_bucket(abs_delta_bps),
                    "confidence_bucket": _confidence_bucket(confidence),
                    "yes_best_bid": finite_float(signal.get("yes_best_bid")),
                    "yes_best_ask": finite_float(signal.get("yes_best_ask")),
                    "no_best_bid": finite_float(signal.get("no_best_bid")),
                    "no_best_ask": finite_float(signal.get("no_best_ask")),
                    "spread": finite_float(signal.get("yes_spread")) if side == "Yes" else finite_float(signal.get("no_spread")),
                    "bid_depth_40_60": finite_float(signal.get("yes_bid_depth_40_60")) if side == "Yes" else finite_float(signal.get("no_bid_depth_40_60")),
                    "ask_depth_40_60": finite_float(signal.get("yes_ask_depth_40_60")) if side == "Yes" else finite_float(signal.get("no_ask_depth_40_60")),
                    "kalman_delta_bps": finite_float(signal.get("kalman_delta_bps")),
                    "kalman_velocity_bps_per_min": finite_float(signal.get("kalman_velocity_bps_per_min")),
                    "kalman_projected_delta_bps": finite_float(signal.get("kalman_projected_delta_bps")),
                    "kalman_residual_bps": finite_float(signal.get("kalman_residual_bps")),
                    "kalman_abs_residual_bps": finite_float(signal.get("kalman_abs_residual_bps")),
                    "kalman_uncertainty_bps": finite_float(signal.get("kalman_uncertainty_bps")),
                    "kalman_trend_agreement": finite_float(signal.get("kalman_trend_agreement")),
                })
    return pd.DataFrame(rows)


def _summarize_group(grouped: pd.core.groupby.DataFrameGroupBy) -> pd.DataFrame:
    summary = grouped.agg(
        samples=("side_won", "size"),
        windows=("slug", "nunique"),
        win_rate=("side_won", "mean"),
        avg_price=("price", "mean"),
        avg_break_even=("break_even_probability", "mean"),
        avg_pnl_per_usd=("realized_pnl_per_usd", "mean"),
        median_pnl_per_usd=("realized_pnl_per_usd", "median"),
        avg_model_edge=("model_edge", "mean"),
        avg_market_edge=("market_edge", "mean"),
        avg_confidence=("confidence", "mean"),
        avg_abs_delta_bps=("abs_delta_bps", "mean"),
        avg_elapsed_s=("elapsed_s", "mean"),
        avg_spread=("spread", "mean"),
        avg_bid_depth_40_60=("bid_depth_40_60", "mean"),
        avg_ask_depth_40_60=("ask_depth_40_60", "mean"),
    ).reset_index()
    summary["edge_over_breakeven"] = summary["win_rate"] - summary["avg_break_even"]
    summary["roi_per_100_usd"] = summary["avg_pnl_per_usd"] * 100.0
    return summary.sort_values(["avg_pnl_per_usd", "samples"], ascending=[False, False])


def summarize_candidates(candidates: pd.DataFrame, min_samples: int, min_windows: int) -> dict[str, pd.DataFrame]:
    if candidates.empty:
        empty = pd.DataFrame()
        return {"archetype": empty, "route": empty, "bucket": empty, "top_bucket": empty}
    outputs: dict[str, pd.DataFrame] = {}
    outputs["archetype"] = _summarize_group(candidates.groupby(["archetype", "route"], dropna=False))
    outputs["route"] = _summarize_group(candidates.groupby(["route", "relation"], dropna=False))
    bucket_cols = ["archetype", "route", "relation", "elapsed_bucket", "price_bucket", "delta_bucket"]
    bucket = _summarize_group(candidates.groupby(bucket_cols, dropna=False))
    outputs["bucket"] = bucket
    outputs["top_bucket"] = bucket[
        (bucket["samples"] >= int(min_samples))
        & (bucket["windows"] >= int(min_windows))
        & (bucket["avg_pnl_per_usd"] > 0.0)
        & (bucket["edge_over_breakeven"] > 0.0)
    ].copy()
    return outputs


def walk_forward_bucket_scan(
    candidates: pd.DataFrame,
    min_train_samples: int,
    min_train_windows: int,
    min_train_pnl_per_usd: float = 0.015,
    min_train_edge_over_breakeven: float = 0.015,
    folds: int = 6,
) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()
    df = candidates.dropna(subset=["window_start_ts"]).copy()
    df["window_start_ts"] = pd.to_numeric(df["window_start_ts"], errors="coerce")
    df = df.dropna(subset=["window_start_ts"]).sort_values("window_start_ts")
    windows = sorted(df["window_start_ts"].dropna().unique())
    if len(windows) < folds + 2:
        return pd.DataFrame()
    fold_edges = [windows[int(len(windows) * i / folds)] for i in range(1, folds)]
    rows: list[dict[str, Any]] = []
    group_cols = ["archetype", "route", "relation", "elapsed_bucket", "price_bucket", "delta_bucket"]
    for fold_index, cutoff in enumerate(fold_edges, start=1):
        train = df[df["window_start_ts"] < cutoff]
        next_cutoff = fold_edges[fold_index] if fold_index < len(fold_edges) else max(windows) + 1
        test = df[(df["window_start_ts"] >= cutoff) & (df["window_start_ts"] < next_cutoff)]
        if train.empty or test.empty:
            continue
        train_summary = _summarize_group(train.groupby(group_cols, dropna=False))
        selected = train_summary[
            (train_summary["samples"] >= min_train_samples)
            & (train_summary["windows"] >= min_train_windows)
            & (train_summary["avg_pnl_per_usd"] >= min_train_pnl_per_usd)
            & (train_summary["edge_over_breakeven"] >= min_train_edge_over_breakeven)
        ].copy()
        if selected.empty:
            rows.append({
                "fold": fold_index,
                "cutoff_window_start_ts": cutoff,
                "selected_buckets": 0,
                "test_samples": 0,
                "test_windows": 0,
                "test_win_rate": math.nan,
                "test_avg_break_even": math.nan,
                "test_avg_pnl_per_usd": 0.0,
                "test_roi_per_100_usd": 0.0,
            })
            continue
        keys = selected[group_cols].drop_duplicates()
        marked = test.merge(keys.assign(_selected_bucket=1), on=group_cols, how="left")
        picked = marked[marked["_selected_bucket"].eq(1)].copy()
        rows.append({
            "fold": fold_index,
            "cutoff_window_start_ts": cutoff,
            "selected_buckets": len(keys),
            "test_samples": len(picked),
            "test_windows": picked["slug"].nunique() if not picked.empty else 0,
            "test_win_rate": picked["side_won"].mean() if not picked.empty else math.nan,
            "test_avg_break_even": picked["break_even_probability"].mean() if not picked.empty else math.nan,
            "test_avg_pnl_per_usd": picked["realized_pnl_per_usd"].mean() if not picked.empty else 0.0,
            "test_roi_per_100_usd": picked["realized_pnl_per_usd"].mean() * 100.0 if not picked.empty else 0.0,
            "test_archetypes": ",".join(sorted(picked["archetype"].dropna().unique())) if not picked.empty else "",
        })
    return pd.DataFrame(rows)


def run_edge_scanner(config: ScannerConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    candidates = build_candidate_dataset(config)
    outputs = summarize_candidates(
        candidates,
        min_samples=config.min_bucket_samples,
        min_windows=config.min_bucket_windows,
    )
    walk_forward = walk_forward_bucket_scan(
        candidates,
        min_train_samples=config.min_train_samples,
        min_train_windows=config.min_bucket_windows,
    )

    if config.write_candidates and not candidates.empty:
        candidates.to_csv(config.output_dir / "candidate_samples.csv", index=False)
    for name, frame in outputs.items():
        frame.to_csv(config.output_dir / f"{name}_summary.csv", index=False)
    walk_forward.to_csv(config.output_dir / "walk_forward_bucket_scan.csv", index=False)

    rule_candidates_path = config.output_dir / "edge_rule_candidates.json"
    rule_columns = ["archetype", "route", "relation", "elapsed_bucket", "price_bucket", "delta_bucket"]
    rule_stats = [
        "samples",
        "windows",
        "win_rate",
        "avg_price",
        "avg_break_even",
        "avg_pnl_per_usd",
        "edge_over_breakeven",
        "roi_per_100_usd",
        "avg_model_edge",
        "avg_market_edge",
        "avg_confidence",
        "avg_abs_delta_bps",
        "avg_elapsed_s",
    ]
    rule_candidates: list[dict[str, Any]] = []
    if not outputs["top_bucket"].empty:
        for _, row in outputs["top_bucket"].head(100).iterrows():
            rule_candidates.append({
                "conditions": {column: row.get(column, "") for column in rule_columns},
                "stats": {
                    column: (
                        int(row[column])
                        if column in {"samples", "windows"}
                        else round(float(row[column]), 8)
                    )
                    for column in rule_stats
                    if column in row and pd.notna(row[column])
                },
            })
    with rule_candidates_path.open("w", encoding="utf-8") as handle:
        json.dump(rule_candidates, handle, indent=2)

    report = {
        "candidate_rows": int(len(candidates)),
        "signal_windows": int(candidates["slug"].nunique()) if not candidates.empty else 0,
        "start_utc": config.start_utc.isoformat() if config.start_utc is not None else "",
        "end_utc": config.end_utc.isoformat() if config.end_utc is not None else "",
        "fee_rate": config.fee_rate,
        "min_bucket_samples": config.min_bucket_samples,
        "min_train_samples": config.min_train_samples,
        "min_bucket_windows": config.min_bucket_windows,
        "outputs": {
            "archetype_summary": str(config.output_dir / "archetype_summary.csv"),
            "route_summary": str(config.output_dir / "route_summary.csv"),
            "bucket_summary": str(config.output_dir / "bucket_summary.csv"),
            "top_bucket_summary": str(config.output_dir / "top_bucket_summary.csv"),
            "edge_rule_candidates": str(rule_candidates_path),
            "walk_forward": str(config.output_dir / "walk_forward_bucket_scan.csv"),
        },
    }
    if not candidates.empty:
        report["candidate_span"] = {
            "min_sample_ts_utc": str(candidates["sample_ts_utc"].min()),
            "max_sample_ts_utc": str(candidates["sample_ts_utc"].max()),
        }
        report["archetype_rows"] = candidates["archetype"].value_counts().to_dict()
        report["route_rows"] = candidates["route"].value_counts().to_dict()
    if not outputs["top_bucket"].empty:
        top = outputs["top_bucket"].head(10).copy()
        report["top_buckets_preview"] = top.to_dict(orient="records")
    if not walk_forward.empty:
        report["walk_forward_totals"] = {
            "folds": int(len(walk_forward)),
            "test_samples": int(walk_forward["test_samples"].sum()),
            "weighted_avg_pnl_per_usd": float(
                (walk_forward["test_avg_pnl_per_usd"] * walk_forward["test_samples"]).sum()
                / max(walk_forward["test_samples"].sum(), 1)
            ),
        }
    with (config.output_dir / "report.json").open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    return report
