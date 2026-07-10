from __future__ import annotations

import csv
from pathlib import Path

from src.shadow_action_analysis import ShadowActionAnalysisConfig, analyze_shadow_action_history


def test_analyze_shadow_action_history_groups_and_sorts(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    _write_csv(
        history,
        [
            {
                "run_ts_utc": "2026-07-08T00:00:00+00:00",
                "action": "watch_wti_vol_sensitive",
                "market_slug": "wti",
                "side": "VOL_SENSITIVITY",
                "family": "wti_daily",
                "confidence": "low",
                "action_priority_score": "10",
                "fair_value_edge_proxy": "0.10",
            },
            {
                "run_ts_utc": "2026-07-08T00:10:00+00:00",
                "action": "watch_wti_vol_sensitive",
                "market_slug": "wti",
                "side": "VOL_SENSITIVITY",
                "family": "wti_daily",
                "confidence": "low",
                "action_priority_score": "12",
                "fair_value_edge_proxy": "0.12",
            },
            {
                "run_ts_utc": "2026-07-08T00:00:00+00:00",
                "action": "paper_maker_quote",
                "market_slug": "maker",
                "side": "BOTH_SCOREABLE_SIDES",
                "family": "fed_rates",
                "confidence": "medium",
                "action_priority_score": "5",
            },
        ],
    )

    report = analyze_shadow_action_history(
        ShadowActionAnalysisConfig(history_csv=history, output_dir=tmp_path / "out")
    )

    assert report["groups"] == 2
    assert report["top_stable_actions"][0]["market_slug"] == "wti"
    assert Path(report["outputs"]["summary"]).exists()


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
