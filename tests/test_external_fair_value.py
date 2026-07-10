from __future__ import annotations

from pathlib import Path

from src.external_fair_value import (
    ExternalFairValueConfig,
    classify_market,
    evaluate_external_market,
    parse_threshold,
)


def test_classify_market_prioritizes_wti_as_modelable() -> None:
    family = classify_market("WTI Crude Oil closes above $69 on July 8?")

    assert family.family == "wti_daily"
    assert family.modelability_score == 5


def test_parse_threshold_reads_direction_and_price() -> None:
    above = parse_threshold("WTI closes above $69.50 today?")
    below = parse_threshold("Will temperature be below 92 degrees?")
    high = parse_threshold("Will WTI Crude Oil (WTI) hit (HIGH) $95 in July?")
    low = parse_threshold("Will WTI Crude Oil (WTI) hit (LOW) $65 in July?")

    assert above is not None
    assert above.direction == "above"
    assert above.threshold == 69.5
    assert below is not None
    assert below.direction == "below"
    assert below.threshold == 92.0
    assert high is not None
    assert high.direction == "above"
    assert high.threshold == 95.0
    assert low is not None
    assert low.direction == "below"
    assert low.threshold == 65.0


def test_classify_market_does_not_match_weather_inside_ukraine() -> None:
    family = classify_market("Will Ukraine recapture Crimean territory by December 31, 2026?")

    assert family.family == "other"


def test_evaluate_external_market_accepts_clean_wti_candidate(tmp_path: Path) -> None:
    preliminary = {
        "scan_ts_utc": "2026-07-08T08:00:00+00:00",
        "family": "wti_daily",
        "modelability_score": 5,
        "market_slug": "wti-close-above-69-july-8",
        "question": "WTI Crude Oil closes above $69 on July 8?",
        "end_date": "2026-07-08 21:00:00+00",
        "hours_to_end": 13.0,
        "volume24hr": 1000.0,
        "yes_token_id": "yes-token",
        "no_token_id": "no-token",
        "threshold_direction": "above",
        "threshold": 69.0,
        "external_value": 70.25,
        "external_distance_to_threshold": 1.25,
    }
    books = {
        "yes-token": {
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.60", "size": "100"}],
        }
    }
    config = ExternalFairValueConfig(output_dir=tmp_path, min_volume_24hr=250.0, max_yes_spread=0.12)

    row = evaluate_external_market(preliminary, books, config)

    assert row["candidate_ok"] is True
    assert row["yes_mid"] == 0.575
    assert row["research_score"] > 0


def test_evaluate_external_market_flags_missing_external_proxy(tmp_path: Path) -> None:
    preliminary = {
        "family": "wti_daily",
        "modelability_score": 5,
        "hours_to_end": 13.0,
        "volume24hr": 1000.0,
        "yes_token_id": "yes-token",
        "threshold_direction": "above",
        "external_value": "",
    }
    books = {
        "yes-token": {
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.60", "size": "100"}],
        }
    }
    config = ExternalFairValueConfig(output_dir=tmp_path)

    row = evaluate_external_market(preliminary, books, config)

    assert row["candidate_ok"] is False
    assert "missing_external_proxy" in row["candidate_flags"]
