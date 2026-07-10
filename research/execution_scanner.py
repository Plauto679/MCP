from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.execution_scanner import ExecutionScannerConfig, run_execution_scanner


def parse_optional_utc(value: str | None, timezone: str) -> pd.Timestamp | None:
    if not value:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone)
    return timestamp.tz_convert("UTC")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure whether recorded Fair Value edges were actually capturable after maker/taker latency."
    )
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "execution_scanner"))
    parser.add_argument("--start", default="", help="Optional local/UTC start timestamp, e.g. 2026-07-05 10:06")
    parser.add_argument("--end", default="", help="Optional local/UTC end timestamp")
    parser.add_argument("--timezone", default="Europe/Madrid")
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--min-bucket-attempts", type=int, default=120)
    parser.add_argument("--min-bucket-windows", type=int, default=20)
    parser.add_argument("--max-signal-rows", type=int, default=0, help="Debug speed limit; 0 means all rows.")
    parser.add_argument("--write-dataset", action="store_true", help="Write the full execution scenario dataset.")
    args = parser.parse_args()

    config = ExecutionScannerConfig(
        data_dir=Path(args.data_dir),
        output_dir=Path(args.output_dir),
        start_utc=parse_optional_utc(args.start, args.timezone),
        end_utc=parse_optional_utc(args.end, args.timezone),
        fee_rate=args.fee_rate,
        min_bucket_attempts=args.min_bucket_attempts,
        min_bucket_windows=args.min_bucket_windows,
        max_signal_rows=args.max_signal_rows,
        write_dataset=args.write_dataset,
    )
    report = run_execution_scanner(config)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
