import math
from typing import Dict, Iterable, Optional


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        if not math.isfinite(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _empty_features() -> Dict[str, float]:
    return {
        "kalman_delta_bps": 0.0,
        "kalman_velocity_bps_per_min": 0.0,
        "kalman_projected_delta_bps": 0.0,
        "kalman_residual_bps": 0.0,
        "kalman_abs_residual_bps": 0.0,
        "kalman_uncertainty_bps": 0.0,
        "kalman_trend_agreement": 0.0,
    }


def kalman_step(
    previous: Optional[Dict],
    observation_price: float,
    observation_ts: float,
    opening_price: float,
    window_start_ts: float,
) -> Dict:
    """Constant-velocity Kalman filter for a 5-minute BTC window.

    The state is price level + price velocity. It is intentionally lightweight:
    enough to smooth noise and estimate trend, not a magic predictor.
    """
    obs = _safe_float(observation_price)
    ts = _safe_float(observation_ts)
    opening = _safe_float(opening_price)
    start = _safe_float(window_start_ts)
    if obs <= 0 or ts <= 0 or opening <= 0 or start <= 0:
        return {"features": _empty_features()}

    if not previous or str(previous.get("window_start_ts") or "") != str(int(start)):
        state = {
            "window_start_ts": int(start),
            "last_ts": ts,
            "level": obs,
            "velocity": 0.0,
            "p00": max((opening * 0.0008) ** 2, 1e-6),
            "p01": 0.0,
            "p10": 0.0,
            "p11": max((opening * 0.00003) ** 2, 1e-8),
        }
    else:
        state = dict(previous)
        last_ts = _safe_float(state.get("last_ts"), ts)
        dt = min(max(ts - last_ts, 0.05), 15.0)
        level = _safe_float(state.get("level"), obs)
        velocity = _safe_float(state.get("velocity"), 0.0)
        p00 = _safe_float(state.get("p00"), 1.0)
        p01 = _safe_float(state.get("p01"), 0.0)
        p10 = _safe_float(state.get("p10"), 0.0)
        p11 = _safe_float(state.get("p11"), 1.0)

        # Predict: F = [[1, dt], [0, 1]]
        pred_level = level + velocity * dt
        pred_velocity = velocity
        process_level_var = max((opening * 0.000015 * max(dt, 1.0)) ** 2, 1e-8)
        process_velocity_var = max((opening * 0.0000025 * max(dt, 1.0)) ** 2, 1e-10)
        pp00 = p00 + dt * (p10 + p01) + dt * dt * p11 + process_level_var
        pp01 = p01 + dt * p11
        pp10 = p10 + dt * p11
        pp11 = p11 + process_velocity_var

        # Update with price observation. R is intentionally larger than the
        # process variance so one tick does not whip the state around.
        measurement_var = max((opening * 0.00020) ** 2, 1e-8)
        innovation = obs - pred_level
        s = pp00 + measurement_var
        if abs(s) < 1e-12:
            k0 = 0.0
            k1 = 0.0
        else:
            k0 = pp00 / s
            k1 = pp10 / s
        level = pred_level + k0 * innovation
        velocity = pred_velocity + k1 * innovation
        p00 = (1.0 - k0) * pp00
        p01 = (1.0 - k0) * pp01
        p10 = pp10 - k1 * pp00
        p11 = pp11 - k1 * pp01
        state = {
            "window_start_ts": int(start),
            "last_ts": ts,
            "level": level,
            "velocity": velocity,
            "p00": max(p00, 1e-10),
            "p01": p01,
            "p10": p10,
            "p11": max(p11, 1e-12),
        }

    level = _safe_float(state.get("level"), obs)
    velocity = _safe_float(state.get("velocity"), 0.0)
    remaining = max(start + 300.0 - ts, 0.0)
    delta_bps = ((level - opening) / opening) * 10000.0
    velocity_bps_per_min = (velocity / opening) * 10000.0 * 60.0
    projected_level = level + velocity * remaining
    projected_delta_bps = ((projected_level - opening) / opening) * 10000.0
    residual_bps = ((obs - level) / opening) * 10000.0
    uncertainty_bps = (math.sqrt(max(_safe_float(state.get("p00"), 0.0), 0.0)) / opening) * 10000.0
    trend_agreement = 1.0 if delta_bps == 0.0 or velocity_bps_per_min == 0.0 else (
        1.0 if (delta_bps > 0) == (velocity_bps_per_min > 0) else -1.0
    )
    state["features"] = {
        "kalman_delta_bps": round(delta_bps, 6),
        "kalman_velocity_bps_per_min": round(velocity_bps_per_min, 6),
        "kalman_projected_delta_bps": round(projected_delta_bps, 6),
        "kalman_residual_bps": round(residual_bps, 6),
        "kalman_abs_residual_bps": round(abs(residual_bps), 6),
        "kalman_uncertainty_bps": round(uncertainty_bps, 6),
        "kalman_trend_agreement": trend_agreement,
    }
    return state


def add_kalman_features_to_frame(df, ts_col: str = "sample_ts_utc"):
    """Add Kalman features to a pandas DataFrame grouped by slug.

    Kept in src so runtime and research use the same implementation.
    """
    import pandas as pd

    if df.empty:
        for key in _empty_features():
            df[key] = []
        return df
    result = df.copy()
    result[ts_col] = pd.to_datetime(result[ts_col], utc=True, errors="coerce")
    for key in _empty_features():
        result[key] = 0.0
    if "slug" not in result.columns:
        return result

    for _, group in result.sort_values(["slug", ts_col]).groupby("slug", sort=False):
        state = None
        for idx, row in group.iterrows():
            observation = _safe_float(row.get("latest_price"), 0.0)
            opening = _safe_float(row.get("opening_price"), 0.0)
            elapsed = _safe_float(row.get("elapsed_s"), 0.0)
            ts = row.get(ts_col)
            if pd.isna(ts):
                continue
            window_start_ts = ts.timestamp() - elapsed
            state = kalman_step(state, observation, ts.timestamp(), opening, window_start_ts)
            for key, value in (state.get("features") or _empty_features()).items():
                result.at[idx, key] = value
    return result
