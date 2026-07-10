from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market_feature_recorder import FeatureRecorder, FeatureRecorderConfig


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Continuously record lightweight Polymarket crypto Up/Down market microstructure features. Research only; places no orders."
    )
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "market_features"))
    parser.add_argument("--slug-prefix", default="btc-updown-5m", help="Example: btc-updown-5m or btc-updown-15m.")
    parser.add_argument("--window-seconds", type=int, default=300, help="300 for 5m, 900 for 15m.")
    parser.add_argument("--sample-interval-ms", type=float, default=500.0)
    parser.add_argument("--max-runtime-seconds", type=float, default=9 * 60 * 60)
    parser.add_argument("--max-output-mb", type=float, default=500.0)
    parser.add_argument("--window-grace-seconds", type=float, default=3.0)
    parser.add_argument("--retry-seconds", type=float, default=5.0)
    parser.add_argument("--max-windows", type=int, default=0, help="0 means limited only by max runtime.")
    args = parser.parse_args()

    config = FeatureRecorderConfig(
        output_dir=Path(args.output_dir),
        slug_prefix=str(args.slug_prefix),
        window_seconds=int(args.window_seconds),
        sample_interval_ms=args.sample_interval_ms,
        max_runtime_seconds=args.max_runtime_seconds,
        max_output_mb=args.max_output_mb,
        window_grace_seconds=args.window_grace_seconds,
        retry_seconds=args.retry_seconds,
        max_windows=args.max_windows,
    )
    reports = asyncio.run(FeatureRecorder(config).run())
    print(json.dumps({"event": "feature_recorder_complete", "windows": len(reports), "reports": reports}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
