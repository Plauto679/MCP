from __future__ import annotations

from research.t270_frozen_validator import (
    FROZEN_ENTRY_ELAPSED_S,
    FROZEN_MAX_PRICES,
    FROZEN_MIN_MID_EDGES,
)


def test_t270_validator_keeps_family_frozen() -> None:
    assert FROZEN_ENTRY_ELAPSED_S == (270.0,)
    assert FROZEN_MAX_PRICES == (0.94, 0.95)
    assert FROZEN_MIN_MID_EDGES == (0.0, 0.05)
