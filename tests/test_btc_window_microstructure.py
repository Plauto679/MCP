from __future__ import annotations

import csv
import datetime as dt
import gzip
from pathlib import Path

from src.btc_window_microstructure import (
    DatasetSpec,
    WindowMicrostructureConfig,
    compare_window_microstructure,
)


def test_compare_window_microstructure_counts_adverse_bid_touch(tmp_path: Path) -> None:
    features_dir = tmp_path / "features"
    features_dir.mkdir()
    rows = [
        _row("btc-updown-5m-1000", 10, yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51),
        _row("btc-updown-5m-1000", 20, yes_bid=0.45, yes_ask=0.48, no_bid=0.52, no_ask=0.55),
    ]
    _write_features(features_dir / "sample.features.csv.gz", rows)

    report = compare_window_microstructure(
        WindowMicrostructureConfig(
            datasets=(DatasetSpec("btc_5m", features_dir, 300),),
            output_dir=tmp_path / "out",
            horizons_s=(5.0,),
            sample_step_s=1.0,
        )
    )

    yes_bid = [
        row
        for row in report["maker_touch_summary"]
        if row["outcome"] == "yes" and row["quote_side"] == "bid"
    ][0]
    assert yes_bid["quote_events"] == 1
    assert yes_bid["touched"] == 1
    assert yes_bid["avg_pnl_per_touched_share"] == -0.025
    assert (tmp_path / "out" / "report.json").exists()


def _row(
    slug: str,
    elapsed_s: int,
    *,
    yes_bid: float,
    yes_ask: float,
    no_bid: float,
    no_ask: float,
) -> dict[str, object]:
    start_ts = int(slug.rsplit("-", 1)[-1])
    sample_ts = dt.datetime.fromtimestamp(start_ts + elapsed_s, tz=dt.timezone.utc).isoformat()
    return {
        "sample_ts_utc": sample_ts,
        "slug": slug,
        "window_start_ts": start_ts,
        "elapsed_s": elapsed_s,
        "yes_best_bid": yes_bid,
        "yes_best_ask": yes_ask,
        "yes_spread": round(yes_ask - yes_bid, 6),
        "yes_mid": round((yes_bid + yes_ask) / 2.0, 6),
        "no_best_bid": no_bid,
        "no_best_ask": no_ask,
        "no_spread": round(no_ask - no_bid, 6),
        "no_mid": round((no_bid + no_ask) / 2.0, 6),
        "maker_pair_edge": round(1.0 - yes_bid - no_bid, 6),
    }


def _write_features(path: Path, rows: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
