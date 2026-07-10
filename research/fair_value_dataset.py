from __future__ import annotations

import argparse
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.kalman_features import add_kalman_features_to_frame

DATA_DIR = ROOT_DIR / "data"
SLUG_RE = re.compile(r"btc-updown-5m-(\d+)$")
KALMAN_FEATURE_COLUMNS = [
    "kalman_delta_bps",
    "kalman_velocity_bps_per_min",
    "kalman_projected_delta_bps",
    "kalman_residual_bps",
    "kalman_abs_residual_bps",
    "kalman_uncertainty_bps",
    "kalman_trend_agreement",
]


def finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def slug_start_ts(slug: Any) -> Optional[int]:
    match = SLUG_RE.search(str(slug or ""))
    if not match:
        return None
    return int(match.group(1))


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, low_memory=False)


def to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def fetch_binance_5m_label(start_ts: int, timeout: float = 8.0) -> Optional[dict[str, Any]]:
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": "BTCUSDT",
        "interval": "5m",
        "startTime": int(start_ts) * 1000,
        "limit": 1,
    }
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        return None
    item = payload[0]
    opening_price = float(item[1])
    closing_price = float(item[4])
    delta = closing_price - opening_price
    if delta == 0:
        yes_won = None
    else:
        yes_won = int(delta > 0)
    return {
        "slug": f"btc-updown-5m-{start_ts}",
        "window_start_ts": start_ts,
        "open_time_ms": int(item[0]),
        "close_time_ms": int(item[6]),
        "opening_price": opening_price,
        "closing_price": closing_price,
        "price_delta": delta,
        "yes_won": yes_won,
        "label_source": "binance_5m",
    }


def labels_from_outcomes(outcomes: pd.DataFrame) -> pd.DataFrame:
    if outcomes.empty or "slug" not in outcomes.columns:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for slug, group in outcomes.groupby("slug", dropna=True):
        usable = group.copy()
        usable["opening_price_num"] = pd.to_numeric(usable.get("opening_price"), errors="coerce")
        usable["closing_price_num"] = pd.to_numeric(usable.get("closing_price"), errors="coerce")
        usable = usable.dropna(subset=["opening_price_num", "closing_price_num"])
        if usable.empty:
            continue
        row = usable.iloc[-1]
        opening_price = float(row["opening_price_num"])
        closing_price = float(row["closing_price_num"])
        delta = closing_price - opening_price
        rows.append({
            "slug": slug,
            "window_start_ts": slug_start_ts(slug),
            "open_time_ms": "",
            "close_time_ms": "",
            "opening_price": opening_price,
            "closing_price": closing_price,
            "price_delta": delta,
            "yes_won": "" if delta == 0 else int(delta > 0),
            "label_source": row.get("settlement_source") or "outcomes",
        })
    return pd.DataFrame(rows)


def build_window_labels(
    signals: pd.DataFrame,
    outcomes: pd.DataFrame,
    labels_path: Path,
    allow_binance: bool = True,
    sleep_seconds: float = 0.05,
) -> pd.DataFrame:
    existing = read_csv(labels_path)
    labels = labels_from_outcomes(outcomes)
    if not existing.empty:
        labels = pd.concat([existing, labels], ignore_index=True)
    if not labels.empty:
        labels = labels.drop_duplicates("slug", keep="last")

    known_slugs = set(labels["slug"].astype(str)) if not labels.empty else set()
    signal_slugs = sorted(str(slug) for slug in signals.get("slug", pd.Series(dtype=str)).dropna().unique())
    missing = [slug for slug in signal_slugs if slug not in known_slugs and slug_start_ts(slug)]

    fetched: list[dict[str, Any]] = []
    if allow_binance:
        for index, slug in enumerate(missing, start=1):
            start_ts = slug_start_ts(slug)
            if not start_ts:
                continue
            try:
                label = fetch_binance_5m_label(start_ts)
                if label:
                    fetched.append(label)
            except Exception as exc:
                print(f"[dataset] Binance label failed for {slug}: {type(exc).__name__}: {str(exc)[:120]}")
            if sleep_seconds > 0 and index < len(missing):
                time.sleep(sleep_seconds)

    if fetched:
        labels = pd.concat([labels, pd.DataFrame(fetched)], ignore_index=True)
    if labels.empty:
        return labels

    labels = labels.drop_duplicates("slug", keep="last")
    labels["window_start_ts"] = pd.to_numeric(labels["window_start_ts"], errors="coerce")
    labels = labels.sort_values("window_start_ts")
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels.to_csv(labels_path, index=False)
    return labels


def add_signal_features(df: pd.DataFrame) -> pd.DataFrame:
    numeric_columns = [
        "elapsed_s",
        "yes_best_bid",
        "yes_best_ask",
        "yes_spread",
        "yes_mid",
        "yes_best_bid_size",
        "yes_best_ask_size",
        "yes_bid_depth_40_60",
        "yes_ask_depth_40_60",
        "yes_bid_notional_40_60",
        "yes_ask_notional_40_60",
        "yes_bid_depth_49_51",
        "yes_ask_depth_49_51",
        "no_best_bid",
        "no_best_ask",
        "no_spread",
        "no_mid",
        "no_best_bid_size",
        "no_best_ask_size",
        "no_bid_depth_40_60",
        "no_ask_depth_40_60",
        "no_bid_notional_40_60",
        "no_ask_notional_40_60",
        "no_bid_depth_49_51",
        "no_ask_depth_49_51",
        "opening_price",
        "latest_price",
        "latest_offset_s",
        "fair_yes",
        "fair_no",
        "raw_fair_yes",
        "raw_fair_no",
        "market_fair_yes",
        "market_blend_weight",
        "delta_usd",
        "delta_bps",
        "confidence",
        "decision_price",
        "decision_edge_probability",
        "decision_ev_per_usd",
        "closing_price",
        "price_delta",
    ]
    df = to_numeric(df, numeric_columns)
    df["sample_ts_utc"] = pd.to_datetime(df["sample_ts_utc"], utc=True, errors="coerce")
    df["window_start_ts"] = df["slug"].map(slug_start_ts)
    df["remaining_s"] = (300.0 - df["elapsed_s"]).clip(lower=0.0, upper=300.0)
    df["elapsed_fraction"] = (df["elapsed_s"] / 300.0).clip(lower=0.0, upper=1.0)
    df["remaining_fraction"] = (df["remaining_s"] / 300.0).clip(lower=0.0, upper=1.0)
    df["abs_delta_bps"] = df["delta_bps"].abs()
    elapsed_minutes = (df["elapsed_s"] / 60.0).where(df["elapsed_s"] > 0)
    df["velocity_bps_per_min"] = df["delta_bps"] / elapsed_minutes
    df["yes_no_mid_sum"] = df["yes_mid"] + df["no_mid"]
    df["yes_market_mid_share"] = df["yes_mid"] / df["yes_no_mid_sum"].replace(0, pd.NA)
    df["spread_sum"] = df["yes_spread"].fillna(0) + df["no_spread"].fillna(0)
    depth_total = df["yes_bid_depth_40_60"].fillna(0) + df["no_bid_depth_40_60"].fillna(0)
    df["bid_depth_imbalance_40_60"] = (
        (df["yes_bid_depth_40_60"].fillna(0) - df["no_bid_depth_40_60"].fillna(0))
        / depth_total.replace(0, pd.NA)
    )
    ask_total = df["yes_ask_depth_40_60"].fillna(0) + df["no_ask_depth_40_60"].fillna(0)
    df["ask_depth_imbalance_40_60"] = (
        (df["yes_ask_depth_40_60"].fillna(0) - df["no_ask_depth_40_60"].fillna(0))
        / ask_total.replace(0, pd.NA)
    )
    df["yes_won"] = pd.to_numeric(df["yes_won"], errors="coerce")
    df["no_won"] = 1 - df["yes_won"]
    df["final_delta_bps"] = (df["price_delta"] / df["opening_price"]) * 10000.0
    df["decision_side_won"] = pd.NA
    yes_mask = df["decision_side"].astype(str).str.lower() == "yes"
    no_mask = df["decision_side"].astype(str).str.lower() == "no"
    df.loc[yes_mask, "decision_side_won"] = df.loc[yes_mask, "yes_won"]
    df.loc[no_mask, "decision_side_won"] = df.loc[no_mask, "no_won"]
    df["decision_side_won"] = pd.to_numeric(df["decision_side_won"], errors="coerce")
    return add_kalman_features_to_frame(df)


def _merge_entry_kalman_features(df: pd.DataFrame, data_dir: Path) -> pd.DataFrame:
    if df.empty:
        return df
    signals = read_csv(data_dir / "fair_value_signals.csv")
    for column in KALMAN_FEATURE_COLUMNS:
        if column in df.columns:
            df = df.drop(columns=[column])
    if signals.empty or "slug" not in signals.columns or "sample_ts_utc" not in signals.columns:
        for column in KALMAN_FEATURE_COLUMNS:
            df[column] = 0.0
        return df

    signals = to_numeric(signals, ["elapsed_s", "opening_price", "latest_price"])
    signals = add_kalman_features_to_frame(signals)
    keep = ["slug", "sample_ts_utc"] + KALMAN_FEATURE_COLUMNS
    signals = signals[[col for col in keep if col in signals.columns]].copy()
    signals["sample_ts_utc"] = pd.to_datetime(signals["sample_ts_utc"], utc=True, errors="coerce")
    signals = signals.dropna(subset=["sample_ts_utc", "slug"])
    if signals.empty:
        for column in KALMAN_FEATURE_COLUMNS:
            df[column] = 0.0
        return df

    pieces: list[pd.DataFrame] = []
    df["_entry_original_order"] = range(len(df))
    for slug, left in df.groupby("slug", dropna=False, sort=False):
        left = left.sort_values("entry_ts_utc").copy()
        right = signals[signals["slug"].astype(str) == str(slug)].sort_values("sample_ts_utc").copy()
        if right.empty:
            for column in KALMAN_FEATURE_COLUMNS:
                left[column] = 0.0
            pieces.append(left)
            continue
        right = right.drop(columns=["slug"], errors="ignore")
        merged = pd.merge_asof(
            left,
            right,
            left_on="entry_ts_utc",
            right_on="sample_ts_utc",
            direction="backward",
            tolerance=pd.Timedelta(seconds=10),
        )
        for column in KALMAN_FEATURE_COLUMNS:
            if column not in merged.columns:
                merged[column] = 0.0
            merged[column] = pd.to_numeric(merged[column], errors="coerce").fillna(0.0)
        pieces.append(merged)

    result = pd.concat(pieces, ignore_index=True, sort=False)
    result = result.sort_values("_entry_original_order").drop(columns=["_entry_original_order"], errors="ignore")
    result = result.drop(columns=["sample_ts_utc"], errors="ignore")
    return result.reset_index(drop=True)


def build_signal_dataset(data_dir: Path, allow_binance: bool) -> pd.DataFrame:
    signals = read_csv(data_dir / "fair_value_signals.csv")
    outcomes = read_csv(data_dir / "fair_value_outcomes.csv")
    if signals.empty:
        return pd.DataFrame()
    labels = build_window_labels(
        signals=signals,
        outcomes=outcomes,
        labels_path=data_dir / "fair_value_window_labels.csv",
        allow_binance=allow_binance,
    )
    if labels.empty:
        return pd.DataFrame()
    keep_label_cols = [
        "slug",
        "window_start_ts",
        "opening_price",
        "closing_price",
        "price_delta",
        "yes_won",
        "label_source",
    ]
    labels = labels[[col for col in keep_label_cols if col in labels.columns]].copy()
    signals = signals.merge(labels, on="slug", how="left", suffixes=("", "_label"))
    for column in ["opening_price", "price_delta"]:
        label_column = f"{column}_label"
        if label_column in signals.columns:
            signals[column] = signals[column].where(signals[column].notna() & (signals[column] != ""), signals[label_column])
            signals = signals.drop(columns=[label_column])
    signals = signals[signals["yes_won"].notna()].copy()
    return add_signal_features(signals)


def build_entry_dataset(data_dir: Path) -> pd.DataFrame:
    entries = read_csv(data_dir / "fair_value_entries.csv")
    outcomes = read_csv(data_dir / "fair_value_outcomes.csv")
    if entries.empty or outcomes.empty:
        return pd.DataFrame()
    outcomes = outcomes.drop_duplicates(["slug", "entry_id"], keep="last")
    df = entries.merge(outcomes, on=["slug", "entry_id"], how="left", suffixes=("_entry", "_outcome"))
    df = df[df["result"].isin(["WIN", "LOSS"])].copy()
    df = to_numeric(df, [
        "elapsed_s",
        "price",
        "stake_usd_entry",
        "shares_entry",
        "fee_usd_entry",
        "fair_probability_entry",
        "edge_probability_entry",
        "ev_per_usd_entry",
        "pnl_usd",
        "opening_price",
        "closing_price",
        "price_delta",
    ])
    df["entry_ts_utc"] = pd.to_datetime(df["event_ts_utc_entry"], utc=True, errors="coerce")
    df["direction_won"] = (df["result"] == "WIN").astype(int)
    df["pnl_per_usd"] = df["pnl_usd"] / df["stake_usd_entry"].replace(0, pd.NA)
    df["final_delta_bps"] = (df["price_delta"] / df["opening_price"]) * 10000.0
    df["direction_is_yes"] = (df["direction_entry"].astype(str).str.lower() == "yes").astype(int)
    df["entry_edge_over_realized"] = df["fair_probability_entry"] - df["direction_won"]
    df = _merge_entry_kalman_features(df, data_dir)
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Fair Value research datasets from bot CSV logs.")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="Directory containing fair_value_*.csv files.")
    parser.add_argument("--signals-output", default="fair_value_training_signals.csv")
    parser.add_argument("--entries-output", default="fair_value_training_entries.csv")
    parser.add_argument("--no-binance", action="store_true", help="Do not fetch missing window labels from Binance.")
    parser.add_argument("--tail-signals", type=int, default=0, help="Optional tail row count for quick experiments.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    signals = build_signal_dataset(data_dir, allow_binance=not args.no_binance)
    if args.tail_signals and len(signals) > args.tail_signals:
        signals = signals.tail(args.tail_signals).copy()
    entries = build_entry_dataset(data_dir)

    signals_path = data_dir / args.signals_output
    entries_path = data_dir / args.entries_output
    if not signals.empty:
        signals.to_csv(signals_path, index=False)
    if not entries.empty:
        entries.to_csv(entries_path, index=False)

    closed_entries = len(entries)
    entry_winrate = float(entries["direction_won"].mean()) if closed_entries else float("nan")
    print(f"[dataset] signal_rows={len(signals)} -> {signals_path}")
    print(f"[dataset] entry_rows={len(entries)} winrate={entry_winrate:.4f} -> {entries_path}")
    if not signals.empty:
        print(
            "[dataset] labels yes_win_rate="
            f"{signals['yes_won'].mean():.4f} windows={signals['slug'].nunique()}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
