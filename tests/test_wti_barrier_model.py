from __future__ import annotations

from src.wti_barrier_model import (
    WtiProbabilityInput,
    infer_wti_contract_type,
    wti_probability_from_row,
    wti_threshold_probability,
)


def test_infer_wti_contract_type_detects_touch_vs_terminal() -> None:
    assert infer_wti_contract_type("Will WTI hit (HIGH) $80 in July?") == "touch"
    assert infer_wti_contract_type("WTI closes above $80 today?") == "terminal"


def test_touch_probability_exceeds_terminal_probability_for_upper_barrier() -> None:
    touch = wti_threshold_probability(
        WtiProbabilityInput(
            spot=74,
            threshold=80,
            direction="above",
            hours_to_end=24 * 14,
            annual_volatility=0.35,
            contract_type="touch",
        )
    )
    terminal = wti_threshold_probability(
        WtiProbabilityInput(
            spot=74,
            threshold=80,
            direction="above",
            hours_to_end=24 * 14,
            annual_volatility=0.35,
            contract_type="terminal",
        )
    )

    assert 0 < terminal < touch < 1


def test_wti_probability_from_row_uses_question_contract_type() -> None:
    row = {
        "question": "Will WTI Crude Oil (WTI) hit (HIGH) $80 in July?",
        "external_value": "74",
        "threshold": "80",
        "threshold_direction": "above",
        "hours_to_end": "336",
    }

    assert wti_probability_from_row(row, annual_volatility=0.35) > 0
