from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "external_polymarket" / "kachoio_5m"

KACHO_DATASET = "kachoio/polymarket-5-minute-crypto-up-down-markets"
HF_DATASET_URL = f"https://huggingface.co/datasets/{KACHO_DATASET}"
HF_DATASET_API = f"https://huggingface.co/api/datasets/{KACHO_DATASET}"
HF_DATASETS_SERVER = "https://datasets-server.huggingface.co"

STATIC_SOURCES = [
    {
        "name": "kachoio/polymarket-5-minute-crypto-up-down-markets",
        "url": HF_DATASET_URL,
        "role": "Primary fixed historical Polymarket crypto 5m order-book dataset.",
        "notes": "Second-by-second top-of-book, BTC/ETH/SOL/XRP/DOGE/HYPE/BNB, Mar-May 2026.",
    },
    {
        "name": "aliplayer1/polymarket-crypto-updown",
        "url": "https://huggingface.co/datasets/aliplayer1/polymarket-crypto-updown",
        "role": "Large continuously updated dataset for 5m/15m/1h/4h crypto markets.",
        "notes": "Useful later, but several GB for BTC orderbook slices.",
    },
    {
        "name": "BrockMisner/polymarket-crypto-5m-15m",
        "url": "https://huggingface.co/datasets/BrockMisner/polymarket-crypto-5m-15m",
        "role": "Smaller historical 5m/15m dataset with orderbooks/trades/price history.",
        "notes": "Good fallback if we want day-sized parquet files instead of one BTC tick file.",
    },
    {
        "name": "Polymarket/poly-market-maker",
        "url": "https://github.com/Polymarket/poly-market-maker",
        "role": "Reference implementation for CLOB market making mechanics.",
        "notes": "Architecture reference only; do not connect live trading from this project.",
    },
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any] | list[Any]:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, (dict, list)):
        raise RuntimeError(f"Unexpected JSON payload from {url}: {type(payload)!r}")
    return payload


def normalise_parquet_inventory(payload: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in payload.get("parquet_files", []):
        rows.append(
            {
                "dataset": item.get("dataset"),
                "config": item.get("config"),
                "split": item.get("split"),
                "filename": item.get("filename"),
                "size": int(item.get("size") or 0),
                "size_mb": round((int(item.get("size") or 0)) / 1_000_000, 3),
                "url": item.get("url"),
            }
        )
    return pd.DataFrame(rows)


def rows_payload_to_frame(payload: dict[str, Any]) -> pd.DataFrame:
    rows = [item.get("row", {}) for item in payload.get("rows", [])]
    return pd.DataFrame(rows)


def fetch_rows_sample(
    session: requests.Session,
    *,
    config: str,
    split: str,
    length: int,
    offset: int,
    timeout: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    payload = request_json(
        session,
        f"{HF_DATASETS_SERVER}/rows",
        params={
            "dataset": KACHO_DATASET,
            "config": config,
            "split": split,
            "offset": offset,
            "length": length,
        },
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected rows response.")
    frame = rows_payload_to_frame(payload)
    meta = {
        "config": config,
        "split": split,
        "offset": offset,
        "requested_rows": length,
        "returned_rows": int(len(frame)),
        "num_rows_total": int(payload.get("num_rows_total") or 0),
        "features": [feature.get("name") for feature in payload.get("features", [])],
    }
    return frame, meta


def download_file(
    session: requests.Session,
    *,
    url: str,
    output_path: Path,
    expected_size: int,
    max_download_mb: float,
    timeout: float,
) -> dict[str, Any]:
    max_bytes = int(max_download_mb * 1_000_000)
    if expected_size > max_bytes:
        return {
            "path": str(output_path),
            "downloaded": False,
            "reason": f"expected_size {expected_size} exceeds max_download_mb {max_download_mb}",
            "size": expected_size,
        }
    if output_path.exists() and output_path.stat().st_size == expected_size:
        return {
            "path": str(output_path),
            "downloaded": False,
            "reason": "already_cached",
            "size": expected_size,
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with output_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

    actual_size = output_path.stat().st_size
    return {
        "path": str(output_path),
        "downloaded": True,
        "reason": "ok",
        "size": actual_size,
    }


def summarize_markets(markets: pd.DataFrame, coin: str) -> dict[str, Any]:
    if markets.empty:
        return {"coin": coin, "markets": 0}

    df = markets.copy()
    for column in ["market_start", "market_end", "recorded_at"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    for column in ["volume", "liquidity", "n_ticks"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    outcome_counts = df.get("outcome", pd.Series(dtype=str)).fillna("unknown").value_counts()
    n_ticks = df.get("n_ticks", pd.Series(dtype=float))
    up_count = int(outcome_counts.get("Up", 0))
    down_count = int(outcome_counts.get("Down", 0))
    markets_count = int(len(df))
    labeled_count = up_count + down_count
    full_tick_pct = float((n_ticks >= 300).mean()) if not n_ticks.empty else None

    return {
        "coin": coin,
        "markets": markets_count,
        "labeled_markets": labeled_count,
        "start_utc": df["market_start"].min().isoformat() if "market_start" in df else None,
        "end_utc": df["market_end"].max().isoformat() if "market_end" in df else None,
        "outcome_counts": {str(key): int(value) for key, value in outcome_counts.to_dict().items()},
        "up_count": up_count,
        "down_count": down_count,
        "up_rate_all_markets": round(up_count / markets_count, 6) if markets_count else None,
        "up_rate_labeled": round(up_count / labeled_count, 6) if labeled_count else None,
        "n_ticks_sum": int(n_ticks.sum()) if not n_ticks.empty else None,
        "n_ticks_median": float(n_ticks.median()) if not n_ticks.empty else None,
        "full_300_tick_pct": round(full_tick_pct, 6) if full_tick_pct is not None else None,
        "volume_sum": float(df["volume"].sum()) if "volume" in df else None,
        "volume_median": float(df["volume"].median()) if "volume" in df else None,
        "liquidity_median": float(df["liquidity"].median()) if "liquidity" in df else None,
    }


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    splits_payload = request_json(
        session,
        f"{HF_DATASETS_SERVER}/splits",
        params={"dataset": KACHO_DATASET},
        timeout=args.timeout,
    )
    parquet_payload = request_json(
        session,
        f"{HF_DATASETS_SERVER}/parquet",
        params={"dataset": KACHO_DATASET},
        timeout=args.timeout,
    )
    if not isinstance(splits_payload, dict) or not isinstance(parquet_payload, dict):
        raise RuntimeError("Unexpected Hugging Face dataset metadata response.")

    inventory = normalise_parquet_inventory(parquet_payload)
    inventory_path = output_dir / "dataset_inventory.csv"
    inventory.to_csv(inventory_path, index=False)

    markets_sample, markets_sample_meta = fetch_rows_sample(
        session,
        config="markets",
        split=args.coin,
        length=args.sample_rows,
        offset=args.sample_offset,
        timeout=args.timeout,
    )
    ticks_sample, ticks_sample_meta = fetch_rows_sample(
        session,
        config="ticks",
        split=args.coin,
        length=args.sample_rows,
        offset=args.sample_offset,
        timeout=args.timeout,
    )
    markets_sample_path = output_dir / f"{args.coin}_markets_sample.csv"
    ticks_sample_path = output_dir / f"{args.coin}_ticks_sample.csv"
    markets_sample.to_csv(markets_sample_path, index=False)
    ticks_sample.to_csv(ticks_sample_path, index=False)

    downloads: list[dict[str, Any]] = []
    market_summary: dict[str, Any] | None = None
    selected = inventory[(inventory["config"] == "markets") & (inventory["split"] == args.coin)]
    if args.download_markets and not selected.empty:
        item = selected.iloc[0].to_dict()
        local_path = output_dir / f"{args.coin}_markets.parquet"
        downloads.append(
            download_file(
                session,
                url=str(item["url"]),
                output_path=local_path,
                expected_size=int(item["size"]),
                max_download_mb=args.max_download_mb,
                timeout=args.timeout,
            )
        )
        if local_path.exists():
            market_summary = summarize_markets(pd.read_parquet(local_path), args.coin)

    tick_download = None
    selected_ticks = inventory[(inventory["config"] == "ticks") & (inventory["split"] == args.coin)]
    if args.download_ticks and not selected_ticks.empty:
        item = selected_ticks.iloc[0].to_dict()
        local_path = output_dir / f"{args.coin}_ticks.parquet"
        tick_download = download_file(
            session,
            url=str(item["url"]),
            output_path=local_path,
            expected_size=int(item["size"]),
            max_download_mb=args.max_download_mb,
            timeout=args.timeout,
        )
        downloads.append(tick_download)

    report = {
        "run_ts_utc": utc_now_iso(),
        "mode": "research_only_no_auth_no_orders",
        "dataset": KACHO_DATASET,
        "dataset_url": HF_DATASET_URL,
        "coin": args.coin,
        "sources": STATIC_SOURCES,
        "splits": splits_payload.get("splits", []),
        "inventory": {
            "rows": int(len(inventory)),
            "total_size_mb": round(float(inventory["size"].sum()) / 1_000_000, 3) if not inventory.empty else 0.0,
            "by_config": inventory.groupby("config")["size_mb"].sum().round(3).to_dict()
            if not inventory.empty
            else {},
        },
        "sample_meta": {
            "markets": markets_sample_meta,
            "ticks": ticks_sample_meta,
        },
        "market_summary": market_summary,
        "downloads": downloads,
        "next_actions": [
            "Use the BTC markets summary to size the historical test set before downloading ticks.",
            "Only download btc_ticks.parquet when max_download_mb is explicitly raised above its size.",
            "Backtest frozen t270/latency/imbalance rules on external ticks with the same fee and execution assumptions.",
            "Compare Polymarket mid/implied probability to Binance movement; reject anything that only wins at stale prices.",
        ],
        "outputs": {
            "inventory": str(inventory_path),
            "markets_sample": str(markets_sample_path),
            "ticks_sample": str(ticks_sample_path),
            "report": str(output_dir / "report.json"),
        },
    }

    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe external Polymarket crypto 5m datasets without downloading large tick files by default."
    )
    parser.add_argument("--coin", default="btc", choices=["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"])
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--sample-rows", type=int, default=100)
    parser.add_argument("--sample-offset", type=int, default=0)
    parser.add_argument("--download-markets", action="store_true", help="Download the small markets parquet for the coin.")
    parser.add_argument("--download-ticks", action="store_true", help="Download the large tick parquet for the coin.")
    parser.add_argument("--max-download-mb", type=float, default=25.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    started = time.time()
    report = run_probe(args)
    print(json.dumps({
        "run_ts_utc": report["run_ts_utc"],
        "coin": report["coin"],
        "inventory_total_size_mb": report["inventory"]["total_size_mb"],
        "market_summary": report["market_summary"],
        "outputs": report["outputs"],
        "elapsed_s": round(time.time() - started, 3),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
