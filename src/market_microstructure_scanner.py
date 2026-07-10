from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .market_ws_recorder import iter_summary_rows_from_sqlite


@dataclass(frozen=True)
class MicrostructureScannerConfig:
    input_csv: Path
    output_dir: Path
    maker_pair_max_cost: float = 0.98
    taker_pair_max_cost: float = 0.995
    imbalance_threshold: float = 0.35
    segment_gap_seconds: float = 1.5
    future_horizons: tuple[float, ...] = (1.0, 3.0, 5.0)


NUMERIC_COLUMNS = [
    "window_start_ts",
    "elapsed_s",
    "yes_best_bid",
    "yes_best_bid_size",
    "yes_best_ask",
    "yes_best_ask_size",
    "yes_mid",
    "no_best_bid",
    "no_best_bid_size",
    "no_best_ask",
    "no_best_ask_size",
    "no_mid",
    "directional_top_imbalance",
    "directional_depth_imbalance_40_60",
    "maker_pair_bid_sum",
    "maker_pair_edge",
    "taker_pair_ask_sum",
    "taker_pair_edge",
    "mid_pair_sum",
]


def _delay_key(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value).replace(".", "p")


def _read_events(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    if path.suffix == ".sqlite" or path.name.endswith(".sqlite.gz"):
        df = pd.DataFrame(iter_summary_rows_from_sqlite(path))
    else:
        df = pd.read_csv(path, dtype=str, low_memory=False)
    if df.empty:
        return df
    df["receive_ts_utc"] = pd.to_datetime(df["receive_ts_utc"], errors="coerce", utc=True)
    for column in NUMERIC_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["receive_ts_utc", "slug"])
    return df.sort_values(["slug", "receive_ts_utc"]).reset_index(drop=True)


def _event_candidates(df: pd.DataFrame, config: MicrostructureScannerConfig) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    candidates = df[
        (
            df["maker_pair_bid_sum"].notna()
            & (df["maker_pair_bid_sum"] > 0)
            & (df["maker_pair_bid_sum"] <= float(config.maker_pair_max_cost))
        )
        | (
            df["taker_pair_ask_sum"].notna()
            & (df["taker_pair_ask_sum"] > 0)
            & (df["taker_pair_ask_sum"] <= float(config.taker_pair_max_cost))
        )
    ].copy()
    if candidates.empty:
        return candidates
    candidates["maker_pair_candidate"] = (
        candidates["maker_pair_bid_sum"].notna()
        & (candidates["maker_pair_bid_sum"] > 0)
        & (candidates["maker_pair_bid_sum"] <= float(config.maker_pair_max_cost))
    )
    candidates["taker_pair_candidate"] = (
        candidates["taker_pair_ask_sum"].notna()
        & (candidates["taker_pair_ask_sum"] > 0)
        & (candidates["taker_pair_ask_sum"] <= float(config.taker_pair_max_cost))
    )
    return candidates


def _segment_candidates(candidates: pd.DataFrame, config: MicrostructureScannerConfig) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    gap = pd.Timedelta(seconds=float(config.segment_gap_seconds))
    segment_id = 0
    for slug, group in candidates.groupby("slug", sort=False):
        group = group.sort_values("receive_ts_utc").reset_index(drop=True)
        if group.empty:
            continue
        start_idx = 0
        for index in range(1, len(group) + 1):
            split = index == len(group)
            if not split:
                split = group.loc[index, "receive_ts_utc"] - group.loc[index - 1, "receive_ts_utc"] > gap
            if not split:
                continue
            segment = group.iloc[start_idx:index]
            segment_id += 1
            start_ts = segment["receive_ts_utc"].iloc[0]
            end_ts = segment["receive_ts_utc"].iloc[-1]
            rows.append({
                "segment_id": segment_id,
                "slug": slug,
                "start_ts_utc": start_ts.isoformat(),
                "end_ts_utc": end_ts.isoformat(),
                "duration_s": round(max((end_ts - start_ts).total_seconds(), 0.0), 3),
                "observations": int(len(segment)),
                "first_elapsed_s": round(float(segment["elapsed_s"].iloc[0]), 3) if segment["elapsed_s"].notna().any() else "",
                "last_elapsed_s": round(float(segment["elapsed_s"].iloc[-1]), 3) if segment["elapsed_s"].notna().any() else "",
                "maker_pair_observations": int(segment["maker_pair_candidate"].sum()),
                "taker_pair_observations": int(segment["taker_pair_candidate"].sum()),
                "min_maker_pair_bid_sum": round(float(segment["maker_pair_bid_sum"].min()), 6),
                "max_maker_pair_edge": round(float(segment["maker_pair_edge"].max()), 6),
                "min_taker_pair_ask_sum": round(float(segment["taker_pair_ask_sum"].min()), 6),
                "max_taker_pair_edge": round(float(segment["taker_pair_edge"].max()), 6),
            })
            start_idx = index
    return pd.DataFrame(rows)


def _timestamp_ns(series: pd.Series) -> np.ndarray:
    return series.dt.tz_convert(None).astype("datetime64[ns]").astype("int64").to_numpy()


def _future_value_at(times_ns: np.ndarray, values: np.ndarray, horizon_seconds: float) -> np.ndarray:
    target = times_ns + int(float(horizon_seconds) * 1_000_000_000)
    idx = np.searchsorted(times_ns, target, side="left")
    out = np.full(len(times_ns), np.nan, dtype="float64")
    valid = idx < len(times_ns)
    out[valid] = values[idx[valid]]
    return out


def _imbalance_replay(df: pd.DataFrame, config: MicrostructureScannerConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    pieces: list[pd.DataFrame] = []
    for _, group in df.groupby("slug", sort=False):
        group = group.sort_values("receive_ts_utc").copy()
        group = group.dropna(subset=["directional_top_imbalance", "yes_mid"])
        group = group[group["yes_mid"] > 0]
        if len(group) < 3:
            continue
        times_ns = _timestamp_ns(group["receive_ts_utc"])
        yes_mid = group["yes_mid"].to_numpy(dtype="float64")
        base = group[[
            "receive_ts_utc",
            "slug",
            "elapsed_s",
            "yes_mid",
            "no_mid",
            "directional_top_imbalance",
            "directional_depth_imbalance_40_60",
            "maker_pair_bid_sum",
            "taker_pair_ask_sum",
        ]].copy()
        for horizon in config.future_horizons:
            key = _delay_key(horizon)
            future_mid = _future_value_at(times_ns, yes_mid, horizon)
            base[f"yes_mid_future_{key}s"] = future_mid
            base[f"yes_mid_change_{key}s"] = future_mid - yes_mid
        pieces.append(base)
    if not pieces:
        return pd.DataFrame(), pd.DataFrame()
    replay = pd.concat(pieces, ignore_index=True, sort=False)
    replay["imbalance_direction"] = np.where(replay["directional_top_imbalance"] > 0, "Yes", "No")
    replay["abs_directional_top_imbalance"] = replay["directional_top_imbalance"].abs()
    replay = replay[replay["abs_directional_top_imbalance"] >= float(config.imbalance_threshold)].copy()
    if replay.empty:
        return replay, pd.DataFrame()

    summary_rows: list[dict[str, Any]] = []
    for horizon in config.future_horizons:
        key = _delay_key(horizon)
        change_col = f"yes_mid_change_{key}s"
        valid = replay.dropna(subset=[change_col])
        if valid.empty:
            continue
        predicted_yes = valid["imbalance_direction"] == "Yes"
        future_up = valid[change_col] > 0
        hit = (predicted_yes & future_up) | (~predicted_yes & (valid[change_col] < 0))
        summary_rows.append({
            "horizon_s": float(horizon),
            "samples": int(len(valid)),
            "windows": int(valid["slug"].nunique()),
            "hit_rate": round(float(hit.mean()), 6),
            "avg_abs_imbalance": round(float(valid["abs_directional_top_imbalance"].mean()), 6),
            "avg_abs_yes_mid_change": round(float(valid[change_col].abs().mean()), 6),
            "avg_signed_change_when_yes_pressure": round(float(valid.loc[predicted_yes, change_col].mean()), 6)
            if predicted_yes.any()
            else "",
            "avg_signed_change_when_no_pressure": round(float(valid.loc[~predicted_yes, change_col].mean()), 6)
            if (~predicted_yes).any()
            else "",
        })
    return replay, pd.DataFrame(summary_rows)


def scan_microstructure(config: MicrostructureScannerConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    events = _read_events(config.input_csv)
    candidates = _event_candidates(events, config)
    segments = _segment_candidates(candidates, config)
    imbalance_events, imbalance_summary = _imbalance_replay(events, config)

    if not candidates.empty:
        candidates.to_csv(config.output_dir / "paired_candidates.csv", index=False)
    if not segments.empty:
        segments.to_csv(config.output_dir / "paired_segments.csv", index=False)
    if not imbalance_events.empty:
        imbalance_events.to_csv(config.output_dir / "imbalance_events.csv", index=False)
    if not imbalance_summary.empty:
        imbalance_summary.to_csv(config.output_dir / "imbalance_summary.csv", index=False)

    report = {
        "input_csv": str(config.input_csv),
        "event_rows": int(len(events)),
        "slugs": int(events["slug"].nunique()) if not events.empty else 0,
        "paired_candidate_rows": int(len(candidates)),
        "paired_segments": int(len(segments)),
        "imbalance_event_rows": int(len(imbalance_events)),
        "maker_pair_max_cost": config.maker_pair_max_cost,
        "taker_pair_max_cost": config.taker_pair_max_cost,
        "imbalance_threshold": config.imbalance_threshold,
        "top_paired_segments": segments.head(20).to_dict(orient="records") if not segments.empty else [],
        "imbalance_summary": imbalance_summary.to_dict(orient="records") if not imbalance_summary.empty else [],
        "outputs": {
            "paired_candidates": str(config.output_dir / "paired_candidates.csv"),
            "paired_segments": str(config.output_dir / "paired_segments.csv"),
            "imbalance_events": str(config.output_dir / "imbalance_events.csv"),
            "imbalance_summary": str(config.output_dir / "imbalance_summary.csv"),
            "report": str(config.output_dir / "report.json"),
        },
    }
    (config.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
