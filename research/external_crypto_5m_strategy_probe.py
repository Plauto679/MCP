from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.late_continuation_execution import taker_pnl_per_usd  # noqa: E402


DEFAULT_DATA_DIR = ROOT / "data" / "external_polymarket" / "kachoio_5m"
DEFAULT_OUTPUT_DIR = DEFAULT_DATA_DIR / "strategy_probe"


@dataclass(frozen=True)
class ExternalStrategyProbeConfig:
    markets_path: Path
    ticks_path: Path
    output_dir: Path
    entry_elapsed_s: tuple[float, ...] = (210.0, 225.0, 240.0, 255.0, 270.0)
    taker_delays_s: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 5.0)
    max_prices: tuple[float, ...] = (0.94, 0.95, 0.96, 0.97, 0.98, 0.99)
    min_mid_edges: tuple[float, ...] = (0.0, 0.05, 0.10, 0.20, 0.30)
    min_depth_imbalances: tuple[float, ...] = (0.0, 0.10, 0.25)
    max_spreads: tuple[float, ...] = (0.01, 0.02, 0.03, 0.05)
    max_entry_lag_s: float = 2.0
    max_execution_elapsed_s: float = 295.0
    train_fraction: float = 0.70
    fee_rate: float = 0.07
    min_top_shares: float = 5.0
    max_windows: int = 0
    write_candidates: bool = False


def _float_tuple(text: str) -> tuple[float, ...]:
    return tuple(float(item.strip()) for item in text.split(",") if item.strip())


def _key(value: float) -> str:
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text.replace(".", "p")


def scenario_id(
    entry_s: float,
    delay_s: float,
    max_price: float,
    min_mid_edge: float,
    min_depth_imbalance: float,
    max_spread: float,
) -> str:
    return (
        f"ext_t{_key(entry_s)}_d{_key(delay_s)}_px{_key(max_price)}_"
        f"edge{_key(min_mid_edge)}_dimb{_key(min_depth_imbalance)}_spr{_key(max_spread)}"
    )


def load_markets(path: Path, max_windows: int = 0) -> pd.DataFrame:
    markets = pd.read_parquet(
        path,
        columns=["condition_id", "slug", "market_start", "market_end", "outcome", "n_ticks"],
    )
    markets = markets[markets["outcome"].isin(["Up", "Down"])].copy()
    markets["market_start"] = pd.to_datetime(markets["market_start"], utc=True, errors="coerce")
    markets["market_end"] = pd.to_datetime(markets["market_end"], utc=True, errors="coerce")
    markets = markets.dropna(subset=["market_start"]).sort_values("market_start")
    if max_windows > 0:
        markets = markets.tail(int(max_windows)).copy()
    markets["market_start_s"] = (markets["market_start"].astype("int64") // 1_000_000_000).astype("int64")
    return markets.reset_index(drop=True)


def load_ticks(path: Path, condition_ids: set[str]) -> pd.DataFrame:
    columns = ["condition_id", "t", "bu", "au", "bd", "ad", "su", "sd", "sau", "sad", "du", "dd"]
    if condition_ids and len(condition_ids) <= 10_000:
        try:
            return pd.read_parquet(path, columns=columns, filters=[("condition_id", "in", list(condition_ids))])
        except Exception:
            pass
    ticks = pd.read_parquet(path, columns=columns)
    if condition_ids:
        ticks = ticks[ticks["condition_id"].isin(condition_ids)].copy()
    return ticks


def build_entry_base(markets: pd.DataFrame, ticks: pd.DataFrame, config: ExternalStrategyProbeConfig) -> pd.DataFrame:
    if markets.empty or ticks.empty:
        return pd.DataFrame()

    label_cols = ["condition_id", "slug", "market_start", "market_start_s", "outcome"]
    merged = ticks.merge(markets[label_cols], on="condition_id", how="inner")
    if merged.empty:
        return pd.DataFrame()
    numeric_cols = ["t", "bu", "au", "bd", "ad", "su", "sd", "sau", "sad", "du", "dd", "market_start_s"]
    for column in numeric_cols:
        merged[column] = pd.to_numeric(merged[column], errors="coerce")
    merged = merged.dropna(subset=["t", "bu", "au", "bd", "ad", "market_start_s"])
    merged = merged[(merged["bu"] <= merged["au"]) & (merged["bd"] <= merged["ad"])].copy()
    merged["elapsed_s"] = merged["t"] - merged["market_start_s"]
    merged = merged[(merged["elapsed_s"] >= 0) & (merged["elapsed_s"] <= 305)].copy()

    merged["mid_up"] = (merged["bu"] + merged["au"]) / 2.0
    merged["mid_down"] = (merged["bd"] + merged["ad"]) / 2.0
    merged["selected_side"] = np.where(merged["mid_up"] >= merged["mid_down"], "Up", "Down")
    merged["selected_mid"] = np.where(merged["selected_side"] == "Up", merged["mid_up"], merged["mid_down"])
    merged["decision_price"] = np.where(merged["selected_side"] == "Up", merged["au"], merged["ad"])
    merged["decision_size"] = np.where(merged["selected_side"] == "Up", merged["sau"], merged["sad"])
    merged["decision_spread"] = np.where(
        merged["selected_side"] == "Up",
        merged["au"] - merged["bu"],
        merged["ad"] - merged["bd"],
    )
    depth_total = merged["du"].fillna(0.0) + merged["dd"].fillna(0.0)
    merged["depth_imbalance"] = np.where(depth_total > 0, (merged["du"].fillna(0.0) - merged["dd"].fillna(0.0)) / depth_total, 0.0)
    merged["signed_depth_imbalance"] = np.where(
        merged["selected_side"] == "Up",
        merged["depth_imbalance"],
        -merged["depth_imbalance"],
    )
    merged["side_won"] = (merged["selected_side"] == merged["outcome"]).astype(int)

    split_index = max(1, min(len(markets) - 1, int(len(markets) * config.train_fraction)))
    train_ids = set(markets.iloc[:split_index]["condition_id"])

    entries: list[pd.DataFrame] = []
    execution_lookup = merged[["condition_id", "t", "au", "ad", "sau", "sad", "elapsed_s"]].rename(
        columns={
            "t": "execution_t",
            "au": "execution_ask_up",
            "ad": "execution_ask_down",
            "sau": "execution_size_up",
            "sad": "execution_size_down",
            "elapsed_s": "execution_elapsed_s",
        }
    )
    for entry_s in config.entry_elapsed_s:
        decision = merged[
            (merged["elapsed_s"] >= entry_s)
            & (merged["elapsed_s"] <= entry_s + config.max_entry_lag_s)
        ].copy()
        if decision.empty:
            continue
        decision = decision.sort_values(["condition_id", "elapsed_s"]).groupby("condition_id", as_index=False).head(1)
        decision["entry_elapsed_s"] = float(entry_s)
        decision["split"] = np.where(decision["condition_id"].isin(train_ids), "train", "test")
        for delay_s in config.taker_delays_s:
            frame = decision.copy()
            frame["delay_s"] = float(delay_s)
            frame["execution_t"] = frame["t"] + int(round(float(delay_s)))
            frame = frame.merge(execution_lookup, on=["condition_id", "execution_t"], how="left")
            frame["execution_price"] = np.where(
                frame["selected_side"] == "Up",
                frame["execution_ask_up"],
                frame["execution_ask_down"],
            )
            frame["execution_size"] = np.where(
                frame["selected_side"] == "Up",
                frame["execution_size_up"],
                frame["execution_size_down"],
            )
            entries.append(frame)

    if not entries:
        return pd.DataFrame()
    base = pd.concat(entries, ignore_index=True, sort=False)
    keep_cols = [
        "condition_id",
        "slug",
        "market_start",
        "split",
        "entry_elapsed_s",
        "delay_s",
        "elapsed_s",
        "execution_elapsed_s",
        "selected_side",
        "outcome",
        "side_won",
        "selected_mid",
        "decision_price",
        "decision_size",
        "execution_price",
        "execution_size",
        "decision_spread",
        "signed_depth_imbalance",
    ]
    return base[keep_cols].copy()


def summarize_late_continuation(base: pd.DataFrame, config: ExternalStrategyProbeConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    candidates: list[pd.DataFrame] = []
    if base.empty:
        return pd.DataFrame(), pd.DataFrame()

    for max_price, min_mid_edge, min_depth_imbalance, max_spread in product(
        config.max_prices,
        config.min_mid_edges,
        config.min_depth_imbalances,
        config.max_spreads,
    ):
        scenario_mask = (
            (base["decision_price"] <= max_price)
            & (base["decision_size"] >= config.min_top_shares)
            & ((base["selected_mid"] - 0.5) >= min_mid_edge)
            & (base["decision_spread"] <= max_spread)
            & (base["signed_depth_imbalance"] >= min_depth_imbalance)
        )
        attempted = base[scenario_mask].copy()
        if attempted.empty:
            continue
        executed_mask = (
            attempted["execution_price"].notna()
            & (attempted["execution_price"] > 0)
            & (attempted["execution_price"] <= max_price)
            & (attempted["execution_size"] >= config.min_top_shares)
            & (attempted["execution_elapsed_s"] <= config.max_execution_elapsed_s)
        )
        attempted["executed"] = executed_mask.astype(int)
        attempted["pnl_per_attempt_usd"] = 0.0
        if executed_mask.any():
            attempted.loc[executed_mask, "pnl_per_attempt_usd"] = [
                taker_pnl_per_usd(bool(won), float(price), config.fee_rate)
                for won, price in zip(
                    attempted.loc[executed_mask, "side_won"],
                    attempted.loc[executed_mask, "execution_price"],
                )
            ]
        attempted["scenario_id"] = [
            scenario_id(entry_s, delay_s, max_price, min_mid_edge, min_depth_imbalance, max_spread)
            for entry_s, delay_s in zip(attempted["entry_elapsed_s"], attempted["delay_s"])
        ]
        candidates.append(attempted)

    if not candidates:
        return pd.DataFrame(), pd.DataFrame()
    candidate_frame = pd.concat(candidates, ignore_index=True, sort=False)
    group_cols = [
        "split",
        "scenario_id",
        "entry_elapsed_s",
        "delay_s",
        "selected_side",
    ]
    summary = (
        candidate_frame.groupby(group_cols, dropna=False)
        .agg(
            attempts=("condition_id", "size"),
            windows=("condition_id", "nunique"),
            executed=("executed", "sum"),
            execution_rate=("executed", "mean"),
            accuracy_attempts=("side_won", "mean"),
            accuracy_executed=("side_won", lambda values: float(values[candidate_frame.loc[values.index, "executed"] == 1].mean()) if (candidate_frame.loc[values.index, "executed"] == 1).any() else np.nan),
            avg_decision_price=("decision_price", "mean"),
            avg_execution_price=("execution_price", "mean"),
            avg_pnl_per_attempt_usd=("pnl_per_attempt_usd", "mean"),
        )
        .reset_index()
    )
    all_summary = (
        candidate_frame.assign(split="all")
        .groupby(group_cols, dropna=False)
        .agg(
            attempts=("condition_id", "size"),
            windows=("condition_id", "nunique"),
            executed=("executed", "sum"),
            execution_rate=("executed", "mean"),
            accuracy_attempts=("side_won", "mean"),
            accuracy_executed=("side_won", lambda values: float(values[candidate_frame.loc[values.index, "executed"] == 1].mean()) if (candidate_frame.loc[values.index, "executed"] == 1).any() else np.nan),
            avg_decision_price=("decision_price", "mean"),
            avg_execution_price=("execution_price", "mean"),
            avg_pnl_per_attempt_usd=("pnl_per_attempt_usd", "mean"),
        )
        .reset_index()
    )
    summary = pd.concat([summary, all_summary], ignore_index=True, sort=False)
    summary = summary.sort_values(["split", "avg_pnl_per_attempt_usd", "attempts"], ascending=[True, False, False])
    return summary, candidate_frame


def summarize_stability(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty or "market_start" not in candidates.columns:
        return pd.DataFrame()
    df = candidates.copy()
    df["market_start"] = pd.to_datetime(df["market_start"], utc=True, errors="coerce")
    df = df.dropna(subset=["market_start"])
    if df.empty:
        return pd.DataFrame()
    df["period_week"] = df["market_start"].dt.to_period("W").astype(str)
    df["executed"] = pd.to_numeric(df["executed"], errors="coerce").fillna(0).astype(int)
    df["side_won"] = pd.to_numeric(df["side_won"], errors="coerce")
    df["pnl_per_attempt_usd"] = pd.to_numeric(df["pnl_per_attempt_usd"], errors="coerce").fillna(0.0)
    df["execution_price"] = pd.to_numeric(df["execution_price"], errors="coerce")
    rows: list[dict[str, Any]] = []
    group_cols = ["period_week", "scenario_id", "selected_side"]
    for key, group in df.groupby(group_cols, dropna=False):
        executed = group[group["executed"] == 1]
        rows.append({
            **dict(zip(group_cols, key)),
            "attempts": int(len(group)),
            "windows": int(group["condition_id"].nunique()),
            "executed": int(group["executed"].sum()),
            "execution_rate": float(group["executed"].mean()) if len(group) else 0.0,
            "accuracy_attempts": float(group["side_won"].mean()) if len(group) else np.nan,
            "accuracy_executed": float(executed["side_won"].mean()) if not executed.empty else np.nan,
            "avg_execution_price": float(executed["execution_price"].mean()) if not executed.empty else np.nan,
            "avg_pnl_per_attempt_usd": float(group["pnl_per_attempt_usd"].mean()) if len(group) else 0.0,
            "total_pnl_one_usd_attempts": float(group["pnl_per_attempt_usd"].sum()),
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.sort_values(
        ["period_week", "avg_pnl_per_attempt_usd", "attempts"],
        ascending=[True, False, False],
    )


def build_train_test(summary: pd.DataFrame, min_train_attempts: int = 50, min_test_attempts: int = 25) -> tuple[pd.DataFrame, pd.DataFrame]:
    if summary.empty:
        return pd.DataFrame(), pd.DataFrame()
    keys = ["scenario_id", "entry_elapsed_s", "delay_s", "selected_side"]
    train = summary[summary["split"] == "train"]
    test = summary[summary["split"] == "test"]
    merged = train.merge(test, on=keys, how="inner", suffixes=("_train", "_test"))
    selected = merged[
        (merged["attempts_train"] >= min_train_attempts)
        & (merged["attempts_test"] >= min_test_attempts)
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
    return merged, selected


def structural_pair_summary(ticks: pd.DataFrame, fee_rate: float, min_top_shares: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    if ticks.empty:
        return pd.DataFrame(), pd.DataFrame()
    book = ticks[["condition_id", "t", "bu", "au", "bd", "ad", "su", "sd", "sau", "sad"]].copy()
    for column in ["bu", "au", "bd", "ad", "su", "sd", "sau", "sad"]:
        book[column] = pd.to_numeric(book[column], errors="coerce")
    book = book.dropna(subset=["bu", "au", "bd", "ad"])
    book = book[(book["bu"] <= book["au"]) & (book["bd"] <= book["ad"])].copy()
    if book.empty:
        return pd.DataFrame(), pd.DataFrame()

    fee_buy = fee_rate * np.minimum(book["au"].clip(0, 1), (1.0 - book["au"]).clip(0, 1))
    fee_buy += fee_rate * np.minimum(book["ad"].clip(0, 1), (1.0 - book["ad"]).clip(0, 1))
    fee_sell = fee_rate * np.minimum(book["bu"].clip(0, 1), (1.0 - book["bu"]).clip(0, 1))
    fee_sell += fee_rate * np.minimum(book["bd"].clip(0, 1), (1.0 - book["bd"]).clip(0, 1))
    book["buy_sum"] = book["au"] + book["ad"]
    book["sell_sum"] = book["bu"] + book["bd"]
    book["buy_net_edge"] = 1.0 - book["buy_sum"] - fee_buy
    book["sell_net_edge"] = book["sell_sum"] - 1.0 - fee_sell
    book["buy_top_shares"] = np.minimum(book["sau"].fillna(0.0), book["sad"].fillna(0.0))
    book["sell_top_shares"] = np.minimum(book["su"].fillna(0.0), book["sd"].fillna(0.0))
    buy_executable = (book["buy_net_edge"] > 0) & (book["buy_top_shares"] >= min_top_shares)
    sell_executable = (book["sell_net_edge"] > 0) & (book["sell_top_shares"] >= min_top_shares)
    summary = pd.DataFrame(
        [
            {
                "ticks": int(len(book)),
                "markets": int(book["condition_id"].nunique()),
                "buy_positive_ticks": int((book["buy_net_edge"] > 0).sum()),
                "sell_positive_ticks": int((book["sell_net_edge"] > 0).sum()),
                "buy_executable_positive_ticks": int(buy_executable.sum()),
                "sell_executable_positive_ticks": int(sell_executable.sum()),
                "buy_positive_markets": int(book.loc[book["buy_net_edge"] > 0, "condition_id"].nunique()),
                "sell_positive_markets": int(book.loc[book["sell_net_edge"] > 0, "condition_id"].nunique()),
                "buy_executable_positive_markets": int(book.loc[buy_executable, "condition_id"].nunique()),
                "sell_executable_positive_markets": int(book.loc[sell_executable, "condition_id"].nunique()),
                "max_buy_net_edge": float(book["buy_net_edge"].max()),
                "max_sell_net_edge": float(book["sell_net_edge"].max()),
                "max_buy_executable_net_edge": float(book.loc[book["buy_top_shares"] >= min_top_shares, "buy_net_edge"].max()),
                "max_sell_executable_net_edge": float(book.loc[book["sell_top_shares"] >= min_top_shares, "sell_net_edge"].max()),
                "p99_buy_net_edge": float(book["buy_net_edge"].quantile(0.99)),
                "p99_sell_net_edge": float(book["sell_net_edge"].quantile(0.99)),
                "min_buy_sum": float(book["buy_sum"].min()),
                "max_sell_sum": float(book["sell_sum"].max()),
            }
        ]
    )
    buy_near = book.nlargest(50, "buy_net_edge").assign(opportunity_type="buy_complete_set")
    sell_near = book.nlargest(50, "sell_net_edge").assign(opportunity_type="split_sell_complete_set")
    near = pd.concat([buy_near, sell_near], ignore_index=True, sort=False)
    return summary, near


def run_external_strategy_probe(config: ExternalStrategyProbeConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "mode": "research_only_no_auth_no_orders",
        "markets_path": str(config.markets_path),
        "ticks_path": str(config.ticks_path),
        "ticks_file_exists": config.ticks_path.exists(),
        "outputs": {
            "split_summary": str(config.output_dir / "split_summary.csv"),
            "train_test": str(config.output_dir / "train_test.csv"),
            "selected_train": str(config.output_dir / "selected_train.csv"),
            "stability_summary": str(config.output_dir / "stability_summary.csv"),
            "paired_summary": str(config.output_dir / "paired_summary.csv"),
            "paired_near_misses": str(config.output_dir / "paired_near_misses.csv"),
            "report": str(config.output_dir / "report.json"),
        },
    }
    if not config.markets_path.exists():
        report["status"] = "missing_markets_parquet"
        (config.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    if not config.ticks_path.exists():
        report["status"] = "missing_ticks_parquet"
        report["next_command"] = (
            ".\\.venv\\Scripts\\python.exe research\\external_polymarket_dataset_probe.py "
            "--coin btc --download-ticks --max-download-mb 200"
        )
        (config.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    markets = load_markets(config.markets_path, max_windows=config.max_windows)
    ticks = load_ticks(config.ticks_path, set(markets["condition_id"]))
    base = build_entry_base(markets, ticks, config)
    split_summary, candidates = summarize_late_continuation(base, config)
    train_test, selected_train = build_train_test(split_summary)
    stability_summary = summarize_stability(candidates)
    paired_summary, paired_near_misses = structural_pair_summary(ticks, config.fee_rate, config.min_top_shares)

    split_summary.to_csv(config.output_dir / "split_summary.csv", index=False)
    train_test.to_csv(config.output_dir / "train_test.csv", index=False)
    selected_train.to_csv(config.output_dir / "selected_train.csv", index=False)
    stability_summary.to_csv(config.output_dir / "stability_summary.csv", index=False)
    paired_summary.to_csv(config.output_dir / "paired_summary.csv", index=False)
    paired_near_misses.to_csv(config.output_dir / "paired_near_misses.csv", index=False)
    if config.write_candidates and not candidates.empty:
        candidates.to_csv(config.output_dir / "candidate_trades.csv", index=False)

    report.update(
        {
            "status": "ok",
            "markets": int(len(markets)),
            "ticks": int(len(ticks)),
            "entry_base_rows": int(len(base)),
            "candidate_rows": int(len(candidates)),
            "selected_scenarios": int(len(selected_train)),
            "stability_rows": int(len(stability_summary)),
            "paired_positive_buy_ticks": int(paired_summary.iloc[0]["buy_positive_ticks"]) if not paired_summary.empty else 0,
            "paired_positive_sell_ticks": int(paired_summary.iloc[0]["sell_positive_ticks"]) if not paired_summary.empty else 0,
            "paired_executable_positive_buy_ticks": int(paired_summary.iloc[0]["buy_executable_positive_ticks"]) if not paired_summary.empty else 0,
            "paired_executable_positive_sell_ticks": int(paired_summary.iloc[0]["sell_executable_positive_ticks"]) if not paired_summary.empty else 0,
        }
    )
    (config.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline strategy probe for external Polymarket crypto 5m parquet data.")
    parser.add_argument("--markets-path", default=str(DEFAULT_DATA_DIR / "btc_markets.parquet"))
    parser.add_argument("--ticks-path", default=str(DEFAULT_DATA_DIR / "btc_ticks.parquet"))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--entry-elapsed-s", default="210,225,240,255,270")
    parser.add_argument("--taker-delays-s", default="0,1,2,3,5")
    parser.add_argument("--max-prices", default="0.94,0.95,0.96,0.97,0.98,0.99")
    parser.add_argument("--min-mid-edges", default="0,0.05,0.10,0.20,0.30")
    parser.add_argument("--min-depth-imbalances", default="0,0.10,0.25")
    parser.add_argument("--max-spreads", default="0.01,0.02,0.03,0.05")
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--min-top-shares", type=float, default=5.0)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--max-windows", type=int, default=0, help="Optional tail-window cap for smoke tests.")
    parser.add_argument("--write-candidates", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = ExternalStrategyProbeConfig(
        markets_path=Path(args.markets_path),
        ticks_path=Path(args.ticks_path),
        output_dir=Path(args.output_dir),
        entry_elapsed_s=_float_tuple(args.entry_elapsed_s),
        taker_delays_s=_float_tuple(args.taker_delays_s),
        max_prices=_float_tuple(args.max_prices),
        min_mid_edges=_float_tuple(args.min_mid_edges),
        min_depth_imbalances=_float_tuple(args.min_depth_imbalances),
        max_spreads=_float_tuple(args.max_spreads),
        fee_rate=float(args.fee_rate),
        min_top_shares=float(args.min_top_shares),
        train_fraction=float(args.train_fraction),
        max_windows=int(args.max_windows),
        write_candidates=bool(args.write_candidates),
    )
    report = run_external_strategy_probe(config)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
