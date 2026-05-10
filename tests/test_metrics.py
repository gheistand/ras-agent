"""
Tests for hydrologic goodness-of-fit metrics.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import numpy as np
import pandas as pd
import pytest

from metrics import (
    nash_sutcliffe,
    peak_error_pct,
    percent_bias,
    rmse,
    score_run,
    time_to_peak_error_hours,
    volume_error_pct,
)


def _series(values, start="2024-07-14 00:00", freq="1h"):
    index = pd.date_range(start=start, periods=len(values), freq=freq)
    return pd.Series(values, index=index, dtype=float)


def test_perfect_fit_returns_ideal_metrics():
    obs = _series([1, 2, 4, 8, 16, 32, 24, 16, 8, 4, 2, 1])
    mod = obs.copy()

    scores = score_run(obs, mod)

    assert scores["nash_sutcliffe"] == pytest.approx(1.0)
    assert scores["peak_error_pct"] == pytest.approx(0.0)
    assert scores["time_to_peak_error_hours"] == pytest.approx(0.0)
    assert scores["volume_error_pct"] == pytest.approx(0.0)
    assert scores["rmse"] == pytest.approx(0.0)
    assert scores["percent_bias"] == pytest.approx(0.0)


def test_two_hour_late_modeled_peak_is_positive():
    obs = _series([1, 2, 4, 8, 16, 32, 24, 16, 8, 4, 2, 1, 0, 0])
    mod = pd.Series(obs.to_numpy(), index=obs.index + pd.Timedelta(hours=2))

    assert time_to_peak_error_hours(obs, mod) == pytest.approx(2.0)
    assert peak_error_pct(obs, mod) == pytest.approx(0.0)


def test_known_ten_percent_high_bias():
    obs = _series([1, 2, 4, 8, 16, 32, 24, 16, 8, 4, 2, 1])
    mod = obs * 1.1

    assert peak_error_pct(obs, mod) == pytest.approx(10.0)
    assert volume_error_pct(obs, mod) == pytest.approx(10.0)
    assert percent_bias(obs, mod) == pytest.approx(10.0)
    assert rmse(obs, mod) == pytest.approx(np.sqrt(np.mean((obs * 0.1) ** 2)))
    assert nash_sutcliffe(obs, mod) < 1.0


def test_nan_values_are_dropped_after_inner_join():
    obs = _series([1, 2, np.nan, 8, 16, 32, 24, 16, 8, 4, 2, 1, 0, 0])
    mod = _series([1, 2, 4, 8, 16, np.nan, 24, 16, 8, 4, 2, 1, 0, 0])

    scores = score_run(obs, mod)

    assert scores["nash_sutcliffe"] == pytest.approx(1.0)
    assert scores["rmse"] == pytest.approx(0.0)
    assert scores["time_to_peak_error_hours"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "obs, mod",
    [
        (_series([]), _series([])),
        (_series(range(9)), _series(range(9))),
    ],
)
def test_empty_or_short_aligned_series_raise_value_error(obs, mod):
    with pytest.raises(ValueError, match="fewer than 10 shared timestamps"):
        score_run(obs, mod)


def test_non_datetime_index_raises_value_error():
    obs = pd.Series(range(10), dtype=float)
    mod = pd.Series(range(10), dtype=float)

    with pytest.raises(ValueError, match="DatetimeIndex"):
        score_run(obs, mod)


def test_score_run_importable_and_returns_expected_keys():
    obs = _series(range(10))
    mod = obs + 1

    assert set(score_run(obs, mod)) == {
        "nash_sutcliffe",
        "peak_error_pct",
        "time_to_peak_error_hours",
        "volume_error_pct",
        "rmse",
        "percent_bias",
    }
