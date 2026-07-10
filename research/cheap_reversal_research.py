from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cheap_reversal import CheapReversalConfig, run_cheap_reversal_research


def parse_optional_utc(value: str | None, timezone: str) -> pd.Timestamp | None:
    if not value:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone)
    return timestamp.tz_convert("UTC")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and walk-forward-test a dedicated cheap reversal model.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "cheap_reversal"))
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--timezone", default="Europe/Madrid")
    parser.add_argument("--min-price", type=float, default=0.01)
    parser.add_argument("--max-price", type=float, default=0.42)
    parser.add_argument("--min-abs-delta-bps", type=float, default=0.5)
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--max-signal-rows", type=int, default=0)
    parser.add_argument("--iterations", type=int, default=900)
    args = parser.parse_args()

    config = CheapReversalConfig(
        data_dir=Path(args.data_dir),
        output_dir=Path(args.output_dir),
        start_utc=parse_optional_utc(args.start, args.timezone),
        end_utc=parse_optional_utc(args.end, args.timezone),
        min_price=args.min_price,
        max_price=args.max_price,
        min_abs_delta_bps=args.min_abs_delta_bps,
        fee_rate=args.fee_rate,
        folds=args.folds,
        max_signal_rows=args.max_signal_rows,
        iterations=args.iterations,
    )
    report = run_cheap_reversal_research(config)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
