from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market_microstructure_scanner import MicrostructureScannerConfig, scan_microstructure


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan recorded Polymarket market websocket summaries for paired maker and imbalance candidates."
    )
    parser.add_argument("input_csv", help="CSV written by research/record_market_ws.py")
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "market_microstructure"))
    parser.add_argument("--maker-pair-max-cost", type=float, default=0.98)
    parser.add_argument("--taker-pair-max-cost", type=float, default=0.995)
    parser.add_argument("--imbalance-threshold", type=float, default=0.35)
    parser.add_argument("--segment-gap-seconds", type=float, default=1.5)
    args = parser.parse_args()

    config = MicrostructureScannerConfig(
        input_csv=Path(args.input_csv),
        output_dir=Path(args.output_dir),
        maker_pair_max_cost=args.maker_pair_max_cost,
        taker_pair_max_cost=args.taker_pair_max_cost,
        imbalance_threshold=args.imbalance_threshold,
        segment_gap_seconds=args.segment_gap_seconds,
    )
    report = scan_microstructure(config)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
