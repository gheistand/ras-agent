"""
Tests for streamstats.py regression fallback and gap reporting.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import streamstats


def test_get_peak_flows_records_gap_when_api_fails(monkeypatch):
    monkeypatch.setattr(streamstats, "delineate_streamstats_watershed", lambda *args, **kwargs: None)

    result = streamstats.get_peak_flows(
        pour_point_lon=-89.6,
        pour_point_lat=39.82,
        drainage_area_mi2=107.0,
        use_api=True,
    )

    assert result.source.startswith("regression_")
    assert result.gaps
    assert result.gaps[0]["id"] == "streamstats-service-transition"
    assert "fallback" in " ".join(result.messages).lower()


def test_get_peak_flows_without_api_does_not_record_service_gap():
    result = streamstats.get_peak_flows(
        pour_point_lon=-89.6,
        pour_point_lat=39.82,
        drainage_area_mi2=107.0,
        use_api=False,
    )

    assert result.source.startswith("regression_")
    assert result.gaps == []
