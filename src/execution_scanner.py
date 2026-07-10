from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .edge_scanner import (
    _archetype,
    _delta_bucket,
    _elapsed_bucket,
    _iter_signal_rows,
    _price_bucket,
    _side_relation,
    finite_float,
    load_window_labels,
)
from .fair_value import taker_fee_fraction


@dataclass(frozen=True)
class ExecutionScannerConfig:
    data_dir: Path
    output_dir: Path
    start_utc: pd.Timestamp | None = None
    end_utc: pd.Timestamp | None = None
    fee_rate: float = 0.07
    taker_delays: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 5.0)
    maker_waits: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0)
    min_bucket_attempts: int = 120
    min_bucket_windows: int = 20
    max_signal_rows: int = 0
    write_dataset: bool = False


def _timestamp_ns(series: pd.Series) -> np.ndarray:
    timestamps = pd.to_datetime(series, errors="coerce", utc=True)
    return timestamps.dt.tz_convert(None).astype("datetime64[ns]").astype("int64").to_numpy()


def _to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def _read_signals(config: ExecutionScannerConfig) -> pd.DataFrame:
    signal_path = config.data_dir / "fair_value_signals.csv"
    if not signal_path.exists():
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(_iter_signal_rows(signal_path, config.start_utc, config.end_utc), start=1):
        rows.append(row)
        if config.max_signal_rows and index >= config.max_signal_rows:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["sample_ts_utc"] = pd.to_datetime(df["sample_ts_utc"], errors="coerce", utc=True)
    df = _to_numeric(df, [
        "elapsed_s",
        "yes_best_bid",
        "yes_best_ask",
        "no_best_bid",
        "no_best_ask",
        "yes_spread",
        "no_spread",
        "yes_bid_depth_40_60",
        "yes_ask_depth_40_60",
        "no_bid_depth_40_60",
        "no_ask_depth_40_60",
        "delta_bps",
        "confidence",
        "continuation_probability",
        "kalman_delta_bps",
        "kalman_velocity_bps_per_min",
        "kalman_projected_delta_bps",
        "kalman_residual_bps",
        "kalman_abs_residual_bps",
        "kalman_uncertainty_bps",
        "kalman_trend_agreement",
    ])
    return df.dropna(subset=["sample_ts_utc", "slug", "elapsed_s"]).sort_values(["slug", "sample_ts_utc"])


def _label_signals(signals: pd.DataFrame, data_dir: Path) -> pd.DataFrame:
    labels = load_window_labels(data_dir)
    if labels.empty or signals.empty:
        return pd.DataFrame()
    labels = labels[["slug", "yes_won", "window_start_ts"]].copy()
    labels["yes_won"] = pd.to_numeric(labels["yes_won"], errors="coerce")
    labels = labels.dropna(subset=["slug", "yes_won"])
    merged = signals.merge(labels, on="slug", how="inner")
    merged["yes_won"] = merged["yes_won"].astype(int)
    return merged


def _pnl_per_usd(side_won: np.ndarray, price: np.ndarray, route: str, fee_rate: float) -> np.ndarray:
    price = np.asarray(price, dtype="float64")
    side_won = np.asarray(side_won, dtype="float64")
    fee = np.zeros_like(price)
    if route == "taker":
        valid = np.isfinite(price) & (price > 0)
        if valid.any():
            fee[valid] = np.array(
                [taker_fee_fraction(float(item), fee_rate) for item in price[valid]],
                dtype="float64",
            )
    tradable = np.isfinite(price) & (price > 0.01) & (price < 0.99)
    win_pnl = np.full_like(price, np.nan)
    loss_pnl = np.full_like(price, np.nan)
    win_pnl[tradable] = (1.0 / price[tradable]) - 1.0 - fee[tradable]
    loss_pnl[tradable] = -1.0 - fee[tradable]
    pnl = np.where(side_won > 0.5, win_pnl, loss_pnl)
    pnl[~tradable] = np.nan
    return pnl


def _future_price_at(times_ns: np.ndarray, prices: np.ndarray, delay_seconds: float) -> np.ndarray:
    target = times_ns + int(float(delay_seconds) * 1_000_000_000)
    idx = np.searchsorted(times_ns, target, side="left")
    output = np.full(len(times_ns), np.nan, dtype="float64")
    valid = idx < len(times_ns)
    output[valid] = prices[idx[valid]]
    return output


def _maker_fill_within(
    times_ns: np.ndarray,
    future_ask: np.ndarray,
    maker_bid: np.ndarray,
    wait_seconds: float,
) -> np.ndarray:
    end_targets = times_ns + int(float(wait_seconds) * 1_000_000_000)
    end_idx = np.searchsorted(times_ns, end_targets, side="right")
    filled = np.zeros(len(times_ns), dtype=bool)
    for index in range(len(times_ns)):
        bid = maker_bid[index]
        if not math.isfinite(float(bid)) or bid <= 0.01 or bid >= 0.99:
            continue
        window = future_ask[index + 1:end_idx[index]]
        if len(window) == 0:
            continue
        valid = window[np.isfinite(window)]
        if len(valid) and float(valid.min()) <= float(bid):
            filled[index] = True
    return filled


def _side_frame(group: pd.DataFrame, side: str, config: ExecutionScannerConfig) -> pd.DataFrame:
    prefix = "yes" if side == "Yes" else "no"
    times_ns = _timestamp_ns(group["sample_ts_utc"])
    ask = group[f"{prefix}_best_ask"].to_numpy(dtype="float64")
    bid = group[f"{prefix}_best_bid"].to_numpy(dtype="float64")
    yes_won = group["yes_won"].to_numpy(dtype="int64")
    side_won = yes_won if side == "Yes" else 1 - yes_won
    elapsed = group["elapsed_s"].to_numpy(dtype="float64")
    delta = group["delta_bps"].fillna(0.0).to_numpy(dtype="float64")
    confidence = group["confidence"].fillna(0.0).to_numpy(dtype="float64")
    relation = [_side_relation(float(value), side) for value in delta]
    abs_delta = np.abs(delta)
    archetypes = [
        _archetype(float(e), float(d), finite_float(p), rel)
        for e, d, p, rel in zip(elapsed, abs_delta, ask, relation)
    ]
    result = pd.DataFrame({
        "sample_ts_utc": group["sample_ts_utc"].to_numpy(),
        "slug": group["slug"].astype(str).to_numpy(),
        "window_start_ts": group["window_start_ts"].to_numpy(),
        "side": side,
        "side_won": side_won,
        "elapsed_s": elapsed,
        "delta_bps": delta,
        "abs_delta_bps": abs_delta,
        "confidence": confidence,
        "relation": relation,
        "archetype": archetypes,
        "taker_now_price": ask,
        "maker_price": bid,
        "elapsed_bucket": [_elapsed_bucket(float(item)) for item in elapsed],
        "delta_bucket": [_delta_bucket(float(item)) for item in abs_delta],
        "taker_price_bucket": [_price_bucket(finite_float(item)) for item in ask],
        "maker_price_bucket": [_price_bucket(finite_float(item)) for item in bid],
        "spread": group.get(f"{prefix}_spread", pd.Series(np.nan, index=group.index)).to_numpy(dtype="float64"),
        "bid_depth_40_60": group.get(f"{prefix}_bid_depth_40_60", pd.Series(np.nan, index=group.index)).to_numpy(dtype="float64"),
        "ask_depth_40_60": group.get(f"{prefix}_ask_depth_40_60", pd.Series(np.nan, index=group.index)).to_numpy(dtype="float64"),
    })
    result["taker_now_pnl"] = _pnl_per_usd(side_won, ask, "taker", config.fee_rate)

    for delay in config.taker_delays:
        key = _delay_key(delay)
        delayed_price = ask if delay == 0 else _future_price_at(times_ns, ask, delay)
        result[f"taker_delay_{key}_price"] = delayed_price
        result[f"taker_delay_{key}_pnl"] = _pnl_per_usd(side_won, delayed_price, "taker", config.fee_rate)

    maker_pnl = _pnl_per_usd(side_won, bid, "maker", config.fee_rate)
    for wait in config.maker_waits:
        key = _delay_key(wait)
        filled = _maker_fill_within(times_ns, ask, bid, wait)
        fallback_price = _future_price_at(times_ns, ask, wait)
        fallback_pnl = _pnl_per_usd(side_won, fallback_price, "taker", config.fee_rate)
        result[f"maker_wait_{key}_filled"] = filled
        result[f"maker_wait_{key}_pnl_if_filled"] = np.where(filled, maker_pnl, np.nan)
        result[f"maker_wait_{key}_attempt_pnl"] = np.where(filled, maker_pnl, 0.0)
        result[f"maker_then_taker_{key}_price"] = np.where(filled, bid, fallback_price)
        result[f"maker_then_taker_{key}_pnl"] = np.where(filled, maker_pnl, fallback_pnl)
        result[f"fallback_taker_{key}_pnl"] = fallback_pnl
    return result


def _delay_key(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value).replace(".", "p")


def build_execution_dataset(config: ExecutionScannerConfig) -> pd.DataFrame:
    signals = _read_signals(config)
    signals = _label_signals(signals, config.data_dir)
    if signals.empty:
        return pd.DataFrame()
    pieces: list[pd.DataFrame] = []
    for _, group in signals.groupby("slug", sort=False):
        group = group.sort_values("sample_ts_utc").reset_index(drop=True)
        if group.empty:
            continue
        pieces.append(_side_frame(group, "Yes", config))
        pieces.append(_side_frame(group, "No", config))
    if not pieces:
        return pd.DataFrame()
    return pd.concat(pieces, ignore_index=True, sort=False)


def _scenario_specs(config: ExecutionScannerConfig) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for delay in config.taker_delays:
        key = _delay_key(delay)
        specs.append({
            "scenario": f"taker_delay_{key}",
            "kind": "taker",
            "delay_s": float(delay),
            "pnl_col": f"taker_delay_{key}_pnl",
            "price_col": f"taker_delay_{key}_price",
            "executed_col": None,
            "attempt_pnl": False,
        })
    for wait in config.maker_waits:
        key = _delay_key(wait)
        specs.append({
            "scenario": f"maker_only_{key}",
            "kind": "maker_only",
            "delay_s": float(wait),
            "pnl_col": f"maker_wait_{key}_pnl_if_filled",
            "price_col": "maker_price",
            "executed_col": f"maker_wait_{key}_filled",
            "attempt_pnl_col": f"maker_wait_{key}_attempt_pnl",
            "attempt_pnl": True,
        })
        specs.append({
            "scenario": f"maker_then_taker_{key}",
            "kind": "maker_then_taker",
            "delay_s": float(wait),
            "pnl_col": f"maker_then_taker_{key}_pnl",
            "price_col": f"maker_then_taker_{key}_price",
            "executed_col": None,
            "attempt_pnl": False,
        })
    return specs


def _summarize_one(df: pd.DataFrame, spec: dict[str, Any], group_cols: list[str]) -> pd.DataFrame:
    price_col = spec["price_col"]
    pnl_col = spec["pnl_col"]
    executed_col = spec.get("executed_col")
    temp = df[group_cols + ["slug", "side_won", pnl_col, price_col]].copy()
    temp = temp.rename(columns={pnl_col: "pnl_per_usd", price_col: "price"})
    temp["valid_price"] = temp["price"].notna() & temp["price"].gt(0.01) & temp["price"].lt(0.99)
    if executed_col:
        temp["executed"] = df[executed_col].astype(bool)
    else:
        temp["executed"] = temp["valid_price"] & temp["pnl_per_usd"].notna()
    temp.loc[~temp["executed"], "price"] = np.nan
    if spec.get("attempt_pnl"):
        temp["attempt_pnl_per_usd"] = df[spec["attempt_pnl_col"]]
    else:
        temp["attempt_pnl_per_usd"] = temp["pnl_per_usd"].fillna(0.0)
    grouped = temp.groupby(group_cols, dropna=False)
    summary = grouped.agg(
        attempts=("slug", "size"),
        windows=("slug", "nunique"),
        executed=("executed", "sum"),
        avg_price=("price", "mean"),
        win_rate_when_executed=("side_won", lambda s: s[temp.loc[s.index, "executed"]].mean() if temp.loc[s.index, "executed"].any() else np.nan),
        avg_pnl_per_executed_usd=("pnl_per_usd", "mean"),
        avg_pnl_per_attempt_usd=("attempt_pnl_per_usd", "mean"),
    ).reset_index()
    summary["fill_rate"] = summary["executed"] / summary["attempts"].replace(0, np.nan)
    summary["scenario"] = spec["scenario"]
    summary["scenario_kind"] = spec["kind"]
    summary["delay_s"] = spec["delay_s"]
    return summary.sort_values(["avg_pnl_per_attempt_usd", "attempts"], ascending=[False, False])


def summarize_execution(dataset: pd.DataFrame, config: ExecutionScannerConfig) -> dict[str, pd.DataFrame]:
    if dataset.empty:
        empty = pd.DataFrame()
        return {"archetype": empty, "bucket": empty, "top_bucket": empty}
    specs = _scenario_specs(config)
    archetype_frames = [
        _summarize_one(dataset, spec, ["archetype", "relation"])
        for spec in specs
    ]
    bucket_frames = [
        _summarize_one(dataset, spec, [
            "archetype",
            "relation",
            "elapsed_bucket",
            "taker_price_bucket",
            "delta_bucket",
        ])
        for spec in specs
    ]
    archetype = pd.concat(archetype_frames, ignore_index=True, sort=False)
    bucket = pd.concat(bucket_frames, ignore_index=True, sort=False)
    top_bucket = bucket[
        (bucket["attempts"] >= int(config.min_bucket_attempts))
        & (bucket["windows"] >= int(config.min_bucket_windows))
        & (bucket["avg_pnl_per_attempt_usd"] > 0.0)
    ].copy()
    top_bucket = top_bucket.sort_values(["avg_pnl_per_attempt_usd", "attempts"], ascending=[False, False])
    return {"archetype": archetype, "bucket": bucket, "top_bucket": top_bucket}


def run_execution_scanner(config: ExecutionScannerConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    dataset = build_execution_dataset(config)
    summaries = summarize_execution(dataset, config)
    if config.write_dataset and not dataset.empty:
        dataset.to_csv(config.output_dir / "execution_dataset.csv", index=False)
    for name, frame in summaries.items():
        frame.to_csv(config.output_dir / f"{name}_execution_summary.csv", index=False)

    report = {
        "candidate_rows": int(len(dataset)),
        "signal_windows": int(dataset["slug"].nunique()) if not dataset.empty else 0,
        "start_utc": config.start_utc.isoformat() if config.start_utc is not None else "",
        "end_utc": config.end_utc.isoformat() if config.end_utc is not None else "",
        "fee_rate": config.fee_rate,
        "taker_delays": list(config.taker_delays),
        "maker_waits": list(config.maker_waits),
        "outputs": {
            "archetype_execution_summary": str(config.output_dir / "archetype_execution_summary.csv"),
            "bucket_execution_summary": str(config.output_dir / "bucket_execution_summary.csv"),
            "top_bucket_execution_summary": str(config.output_dir / "top_bucket_execution_summary.csv"),
        },
    }
    if not dataset.empty:
        report["candidate_span"] = {
            "min_sample_ts_utc": str(dataset["sample_ts_utc"].min()),
            "max_sample_ts_utc": str(dataset["sample_ts_utc"].max()),
        }
    if not summaries["archetype"].empty:
        key_cols = [
            "scenario",
            "archetype",
            "relation",
            "attempts",
            "windows",
            "executed",
            "fill_rate",
            "win_rate_when_executed",
            "avg_price",
            "avg_pnl_per_executed_usd",
            "avg_pnl_per_attempt_usd",
        ]
        preview = summaries["archetype"].sort_values(
            ["avg_pnl_per_attempt_usd", "attempts"],
            ascending=[False, False],
        ).head(20)
        report["archetype_preview"] = preview[[c for c in key_cols if c in preview.columns]].to_dict(orient="records")
    with (config.output_dir / "report.json").open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    return report
