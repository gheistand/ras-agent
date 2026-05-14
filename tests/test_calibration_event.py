"""Tests for Spring Creek calibration event packaging helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

from pathlib import Path

import pandas as pd

import calibration_event
import nwis


def _series(parameter_code: str, values: list[float], times: pd.DatetimeIndex) -> nwis.NwisTimeSeries:
    return nwis.NwisTimeSeries(
        service="iv",
        site_no="05577500",
        site_name="SPRING CREEK AT SPRINGFIELD, IL",
        parameter_code=parameter_code,
        parameter_name=parameter_code,
        statistic_code=None,
        units="ft3/s" if parameter_code == "00060" else "ft",
        records=[
            {
                "datetime": ts.isoformat(),
                "value": value,
                "qualifiers": ["A"],
                "quality_flags": [],
            }
            for ts, value in zip(times, values)
        ],
    )


def _collection(
    times: pd.DatetimeIndex,
    flows: list[float],
    stages: list[float] | None = None,
) -> nwis.NwisTimeSeriesCollection:
    series = [_series("00060", flows, times)]
    if stages is not None:
        series.append(_series("00065", stages, times))
    return nwis.NwisTimeSeriesCollection(
        service="iv",
        sites=["05577500"],
        parameter_codes=["00060", "00065"],
        series=series,
        request_params={},
        source_url="https://example.test/nwis/iv",
    )


def _triangular_event() -> tuple[pd.DatetimeIndex, list[float], list[float]]:
    times = pd.date_range("2011-06-15T00:00:00Z", periods=9 * 96, freq="15min")
    event_start = pd.Timestamp("2011-06-18T18:00:00Z")
    peak_time = pd.Timestamp("2011-06-19T05:45:00Z")
    recession_end = pd.Timestamp("2011-06-21T18:00:00Z")
    flows = []
    stages = []
    for timestamp in times:
        if timestamp < event_start:
            fraction = 0.0
        elif timestamp <= peak_time:
            fraction = (timestamp - event_start) / (peak_time - event_start)
        elif timestamp <= recession_end:
            fraction = 1.0 - (timestamp - peak_time) / (recession_end - peak_time)
        else:
            fraction = 0.0
        flows.append(100.0 + float(fraction) * 5110.0)
        stages.append(3.0 + float(fraction) * 10.34)
    return times, flows, stages


def test_observed_collection_to_frame_pivots_flow_and_stage():
    times = pd.date_range("2011-06-19T05:30:00Z", periods=2, freq="15min")
    frame = calibration_event.observed_collection_to_frame(
        _collection(times, [5000.0, 5210.0], [13.2, 13.34])
    )

    assert frame.index.name == "datetime_utc"
    assert list(frame["flow_cfs"]) == [5000.0, 5210.0]
    assert list(frame["stage_ft"]) == [13.2, 13.34]
    assert set(frame["flow_qualifiers"]) == {"A"}


def test_hydrograph_diagnostics_identifies_clear_event_shape():
    times, flows, _stages = _triangular_event()
    frame = pd.Series(flows, index=times)
    diagnostics = calibration_event._hydrograph_diagnostics(frame)

    assert diagnostics is not None
    assert diagnostics.clear_shape is True
    assert diagnostics.peak_flow_cfs == 5210.0
    assert diagnostics.peak_timestamp == pd.Timestamp("2011-06-19T05:45:00Z")
    assert diagnostics.event_start > pd.Timestamp("2011-06-18T17:00:00Z")
    assert diagnostics.recession_hours > 48.0


def test_select_event_prefers_2011_when_2002_lacks_stage(monkeypatch, tmp_path: Path):
    times, flows, stages = _triangular_event()

    def fake_get_annual_peaks(*_args, **_kwargs):
        return nwis.NwisPeakSeries(
            site_no="05577500",
            source_url="https://example.test/nwis/peak",
            records=[
                {
                    "site_no": "05577500",
                    "peak_dt": "2002-05-12",
                    "peak_cfs": 5860.0,
                    "peak_cd": [],
                    "gage_height_ft": 14.01,
                },
                {
                    "site_no": "05577500",
                    "peak_dt": "2011-06-18",
                    "peak_cfs": 5210.0,
                    "peak_cd": [],
                    "gage_height_ft": 13.34,
                },
            ],
        )

    def fake_get_instantaneous_values(*_args, start_date=None, **_kwargs):
        if str(start_date).startswith("2002"):
            return _collection(times, flows, stages=None)
        return _collection(times, flows, stages=stages)

    monkeypatch.setattr(calibration_event.nwis, "get_annual_peaks", fake_get_annual_peaks)
    monkeypatch.setattr(
        calibration_event.nwis,
        "get_instantaneous_values",
        fake_get_instantaneous_values,
    )

    selection = calibration_event.select_event("05577500", tmp_path)

    assert selection.event_name == "June 2011 Spring Creek flood"
    assert selection.peak_observed_flow_cfs == 5210.0
    assert selection.peak_observed_stage_ft == 13.34
    rejected_2002 = selection.candidate_table.query("peak_date == '2002-05-12'").iloc[0]
    assert bool(rejected_2002["eligible"]) is False
    assert "missing IV stage" in rejected_2002["rejection_reason"]
