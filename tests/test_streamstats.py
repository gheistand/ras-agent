"""
Tests for streamstats.py — USGS StreamStats API integration and LP3 peak flows.

Covers the 2026 ss-delineate API endpoint, flow statistics fallback,
regression fallback gap reporting, and gauge-based Log-Pearson III frequency analysis.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import streamstats
from streamstats import (
    PeakFlowEstimates,
    delineate_streamstats_watershed,
    get_flow_statistics,
    get_peak_flows,
    get_peak_flows_from_rdb,
)

# Path to the real USGS annual peaks RDB file used in Spring Creek pilot
_RDB_PATH = Path(
    "/Users/glennheistand/Projects/ras-agent/workspace/"
    "spring_creek/01_gauge/peaks/USGS_05577500_annual_peaks.rdb"
)


# ── Tests: regression fallback and gap reporting (pr-6-review) ───────────────

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


# ── Tests: 2026 ss-delineate API endpoint (main) ─────────────────────────────

class TestDelineateUsesNewEndpoint:
    def test_delineate_uses_new_endpoint(self):
        """Delineation must call the ss-delineate API, not the legacy endpoint."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "bcrequest": {
                "wsresp": {
                    "workspaceID": "N/A",
                    "featurecollection": [
                        {"name": "globalwatershed", "feature": {}},
                    ],
                }
            }
        }

        captured_urls = []

        def fake_get(url, **kwargs):
            captured_urls.append(url)
            return mock_resp

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = fake_get

        with patch("streamstats.httpx.Client", return_value=mock_client):
            result = delineate_streamstats_watershed(lon=-89.65, lat=39.80, region="IL")

        assert len(captured_urls) == 1
        url = captured_urls[0]
        assert "/ss-delineate/v1/delineate/sshydro/" in url
        assert "streamstatsservices" not in url
        assert result is not None


class TestGetFlowStatisticsReturnsNone:
    def test_get_flow_statistics_returns_none(self):
        """get_flow_statistics must return None without making any HTTP calls."""
        with patch("streamstats.httpx.Client") as mock_client_cls:
            result = get_flow_statistics("IL", "ws123")

        assert result is None
        mock_client_cls.assert_not_called()


class TestGetPeakFlowsFromRdb:
    @pytest.mark.skipif(
        not _RDB_PATH.exists(),
        reason=f"RDB file not present: {_RDB_PATH}",
    )
    def test_get_peak_flows_from_rdb(self):
        """LP3 fit on Spring Creek gauge data must produce monotonic, positive flows."""
        result = get_peak_flows_from_rdb(_RDB_PATH)

        assert isinstance(result, PeakFlowEstimates)
        assert result.source == "gauge_lp3"

        # All 7 return periods must be present
        for label in ("Q2", "Q5", "Q10", "Q25", "Q50", "Q100", "Q500"):
            val = getattr(result, label)
            assert val is not None, f"{label} is None"
            assert val > 0, f"{label} = {val} is not positive"

        # Flows must be monotonically increasing with return period
        assert result.Q2 < result.Q5
        assert result.Q5 < result.Q10
        assert result.Q10 < result.Q25
        assert result.Q25 < result.Q50
        assert result.Q50 < result.Q100
        assert result.Q100 < result.Q500

    def test_get_peak_flows_from_rdb_insufficient_data(self, tmp_path):
        """LP3 fit must raise ValueError when fewer than 10 valid records exist."""
        rdb = tmp_path / "peaks.rdb"
        # Header + format row + 8 valid data rows
        lines = [
            "# USGS test file\n",
            "agency_cd\tsite_no\tpeak_dt\tpeak_va\tpeak_cd\n",
            "5s\t15s\t10d\t8s\t33s\n",
        ]
        for i in range(8):
            lines.append(f"USGS\t12345678\t199{i}-06-01\t{1000 + i * 100}\t\n")
        rdb.write_text("".join(lines))

        with pytest.raises(ValueError, match="Insufficient data"):
            get_peak_flows_from_rdb(rdb)
