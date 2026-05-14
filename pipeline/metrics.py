"""
metrics.py - Hydrologic goodness-of-fit metrics.

Compares paired observed and modeled time series after aligning on shared
timestamps and dropping NaN values.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd


MIN_SHARED_TIMESTAMPS = 10

_trapz: Callable[..., float] = getattr(np, "trapezoid", getattr(np, "trapz", None))


def _align_series(obs: pd.Series, mod: pd.Series) -> pd.DataFrame:
    """Validate and align observed/modeled series on shared timestamps."""
    if not isinstance(obs, pd.Series) or not isinstance(mod, pd.Series):
        raise ValueError("obs and mod must both be pandas Series")

    if not isinstance(obs.index, pd.DatetimeIndex):
        raise ValueError("obs must have a DatetimeIndex")
    if not isinstance(mod.index, pd.DatetimeIndex):
        raise ValueError("mod must have a DatetimeIndex")

    if not pd.api.types.is_numeric_dtype(obs):
        raise ValueError("obs values must be numeric")
    if not pd.api.types.is_numeric_dtype(mod):
        raise ValueError("mod values must be numeric")

    aligned = pd.concat(
        [obs.rename("obs"), mod.rename("mod")],
        axis=1,
        join="inner",
    ).dropna()
    aligned = aligned.sort_index()

    if len(aligned) < MIN_SHARED_TIMESTAMPS:
        raise ValueError(
            f"fewer than {MIN_SHARED_TIMESTAMPS} shared timestamps after alignment"
        )

    return aligned.astype(float)


def _pct_error(modeled: float, observed: float) -> float:
    if observed == 0:
        return float("nan")
    return float(100.0 * (modeled - observed) / observed)


def _volumes_by_trapezoid(aligned: pd.DataFrame) -> tuple[float, float]:
    elapsed_hours = (
        (aligned.index - aligned.index[0]).total_seconds().astype(float) / 3600.0
    )
    obs_volume = float(_trapz(aligned["obs"].to_numpy(), x=elapsed_hours))
    mod_volume = float(_trapz(aligned["mod"].to_numpy(), x=elapsed_hours))
    return obs_volume, mod_volume


def nash_sutcliffe(obs: pd.Series, mod: pd.Series) -> float:
    """
    Nash-Sutcliffe Efficiency.

    Returns 1.0 for a perfect fit, 0.0 for mean-observation prediction, and
    negative values for worse-than-mean modeled series.
    """
    aligned = _align_series(obs, mod)
    observed = aligned["obs"].to_numpy()
    modeled = aligned["mod"].to_numpy()

    denominator = float(np.sum((observed - np.mean(observed)) ** 2))
    if denominator == 0:
        return float("nan")

    numerator = float(np.sum((modeled - observed) ** 2))
    return float(1.0 - numerator / denominator)


def peak_error_pct(obs: pd.Series, mod: pd.Series) -> float:
    """Percent error of peak modeled value relative to peak observed value."""
    aligned = _align_series(obs, mod)
    observed_peak = float(aligned["obs"].max())
    modeled_peak = float(aligned["mod"].max())
    return _pct_error(modeled_peak, observed_peak)


def time_to_peak_error_hours(obs: pd.Series, mod: pd.Series) -> float:
    """
    Difference in hours between modeled and observed peak times.

    Positive values mean the modeled peak is later than the observed peak.
    """
    aligned = _align_series(obs, mod)
    observed_peak_time = aligned["obs"].idxmax()
    modeled_peak_time = aligned["mod"].idxmax()
    delta = modeled_peak_time - observed_peak_time
    return float(delta.total_seconds() / 3600.0)


def volume_error_pct(obs: pd.Series, mod: pd.Series) -> float:
    """Percent error of total volume using trapezoidal integration over time."""
    aligned = _align_series(obs, mod)
    obs_volume, mod_volume = _volumes_by_trapezoid(aligned)
    return _pct_error(mod_volume, obs_volume)


def rmse(obs: pd.Series, mod: pd.Series) -> float:
    """Root mean square error."""
    aligned = _align_series(obs, mod)
    error = aligned["mod"].to_numpy() - aligned["obs"].to_numpy()
    return float(np.sqrt(np.mean(error**2)))


def percent_bias(obs: pd.Series, mod: pd.Series) -> float:
    """Mean modeled bias as a percentage of the observed mean."""
    aligned = _align_series(obs, mod)
    observed_mean = float(aligned["obs"].mean())
    modeled_mean = float(aligned["mod"].mean())
    return _pct_error(modeled_mean, observed_mean)


def score_run(obs: pd.Series, mod: pd.Series) -> dict[str, float]:
    """Return all standard hydrologic GOF metrics for one modeled run."""
    return {
        "nash_sutcliffe": nash_sutcliffe(obs, mod),
        "peak_error_pct": peak_error_pct(obs, mod),
        "time_to_peak_error_hours": time_to_peak_error_hours(obs, mod),
        "volume_error_pct": volume_error_pct(obs, mod),
        "rmse": rmse(obs, mod),
        "percent_bias": percent_bias(obs, mod),
    }
