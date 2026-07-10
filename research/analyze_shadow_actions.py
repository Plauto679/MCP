from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shadow_action_analysis import (  # noqa: E402
    ShadowActionAnalysisConfig,
    analyze_shadow_action_history,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize dry shadow action history into stability rankings."
    )
    parser.add_argument(
        "--history-csv",
        default=str(ROOT / "data" / "reward_fair_value_evaluator_live" / "shadow_action_history.csv"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "data" / "shadow_action_analysis_live"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = ShadowActionAnalysisConfig(
        history_csv=Path(args.history_csv),
        output_dir=Path(args.output_dir),
    )
    print(json.dumps(analyze_shadow_action_history(config), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
