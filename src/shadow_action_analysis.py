from __future__ import annotations

import csv
import datetime as dt
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .market_ws_recorder import safe_float


@dataclass(frozen=True)
class ShadowActionAnalysisConfig:
    history_csv: Path
    output_dir: Path


def analyze_shadow_action_history(config: ShadowActionAnalysisConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    rows = _read_csv(config.history_csv)
    summaries = _summarize(rows)
    summary_path = config.output_dir / "shadow_action_summary.csv"
    report_path = config.output_dir / "report.json"
    _write_csv(summary_path, summaries, append=False)
    report = {
        "run_ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "dry_research_only",
        "history_csv": str(config.history_csv),
        "rows_read": len(rows),
        "groups": len(summaries),
        "top_stable_actions": summaries[:30],
        "outputs": {
            "summary": str(summary_path),
            "report": str(report_path),
        },
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("action") or ""),
            str(row.get("market_slug") or ""),
            str(row.get("side") or ""),
        )
        if key[0] and key[1]:
            grouped[key].append(row)

    summaries: list[dict[str, Any]] = []
    for (action, market_slug, side), group in grouped.items():
        group.sort(key=lambda row: str(row.get("run_ts_utc") or ""))
        priorities = [safe_float(row.get("action_priority_score"), math.nan) for row in group]
        priorities = [value for value in priorities if math.isfinite(value)]
        edges = [safe_float(row.get("fair_value_edge_proxy"), math.nan) for row in group]
        edges = [value for value in edges if math.isfinite(value)]
        latest = group[-1]
        avg_priority = sum(priorities) / len(priorities) if priorities else 0.0
        summaries.append(
            {
                "action": action,
                "market_slug": market_slug,
                "side": side,
                "family": str(latest.get("family") or ""),
                "confidence": str(latest.get("confidence") or ""),
                "observations": len(group),
                "first_ts_utc": str(group[0].get("run_ts_utc") or ""),
                "last_ts_utc": str(latest.get("run_ts_utc") or ""),
                "avg_priority_score": round(avg_priority, 6),
                "max_priority_score": round(max(priorities), 6) if priorities else "",
                "avg_fair_value_edge": round(sum(edges) / len(edges), 6) if edges else "",
                "min_fair_value_edge": round(min(edges), 6) if edges else "",
                "max_fair_value_edge": round(max(edges), 6) if edges else "",
                "latest_net_ev_pessimistic_usd": latest.get("net_ev_pessimistic_usd", ""),
                "latest_net_ev_base_usd": latest.get("net_ev_base_usd", ""),
                "latest_history_fill_rate": latest.get("history_fill_rate", ""),
                "latest_reason": latest.get("reason", ""),
                "stability_score": round(len(group) * avg_priority, 6),
            }
        )
    summaries.sort(key=lambda row: safe_float(row.get("stability_score"), -math.inf), reverse=True)
    return summaries


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, rows: list[dict[str, Any]], append: bool) -> None:
    if not rows and not append:
        path.write_text("", encoding="utf-8")
        return
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not append or not path.exists() or path.stat().st_size == 0
    with path.open("a" if append else "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
