from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.btc_window_microstructure import (  # noqa: E402
    DatasetSpec,
    WindowMicrostructureConfig,
    compare_window_microstructure,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare BTC 5m vs 15m maker-only microstructure. Research only; no orders."
    )
    parser.add_argument("--btc5-dir", default=str(ROOT / "data" / "market_features"))
    parser.add_argument("--btc15-dir", default=str(ROOT / "data" / "market_features_btc_15m"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "btc_window_microstructure_compare"))
    parser.add_argument("--horizons-s", default="5,20,60")
    parser.add_argument("--sample-step-s", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    horizons = tuple(float(part) for part in str(args.horizons_s).split(",") if part.strip())
    report = compare_window_microstructure(
        WindowMicrostructureConfig(
            datasets=(
                DatasetSpec("btc_5m", Path(args.btc5_dir), 300),
                DatasetSpec("btc_15m", Path(args.btc15_dir), 900),
            ),
            output_dir=Path(args.output_dir),
            horizons_s=horizons,
            sample_step_s=float(args.sample_step_s),
        )
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
