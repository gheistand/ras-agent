"""
Tests for USGS NWIS gauge retrieval, parsing, and cache behavior.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import nwis


SPRING_CREEK_PEAKS_RDB = """#
# U.S. Geological Survey
agency_cd	site_no	peak_dt	peak_tm	peak_va	peak_cd	gage_ht	gage_ht_cd
5s	15s	10d	6s	8s	33s	8s	27s
USGS	05577500	1948-07-26		1050		9.05
USGS	05577500	1960-10-03	0515	1234	2,7	15.20	1
"""


def _waterml_json() -> str:
    return json.dumps(
        {
            "value": {
                "timeSeries": [
                    {
                        "name": "USGS:05577500:00060:00003",
                        "sourceInfo": {
                            "siteName": "SPRING CREEK AT SPRINGFIELD, IL",
                            "siteCode": [{"value": "05577500", "agencyCode": "USGS"}],
                        },
                        "variable": {
                            "variableCode": [{"value": "00060"}],
                            "variableDescription": "Discharge, cubic feet per second",
                            "unit": {"unitCode": "ft3/s"},
                            "options": {
                                "option": [
                                    {
                                        "name": "Statistic",
                                        "optionCode": "00003",
                                        "value": "Mean",
                                    }
                                ]
                            },
                            "noDataValue": -999999.0,
                        },
                        "values": [
                            {
                                "qualifier": [
                                    {
                                        "qualifierCode": "A",
                                        "qualifierDescription": (
                                            "Approved for publication -- Processing and review completed."
                                        ),
                                    },
                                    {
                                        "qualifierCode": "P",
                                        "qualifierDescription": "Provisional data subject to revision.",
                                    },
                                    {
                                        "qualifierCode": "e",
                                        "qualifierDescription": "Value has been estimated.",
                                    },
                                ],
                                "value": [
                                    {
                                        "value": "4.60",
                                        "qualifiers": ["A"],
                                        "dateTime": "1948-01-21T00:00:00.000",
                                    },
                                    {
                                        "value": "-999999.0",
                                        "qualifiers": ["P", "e"],
                                        "dateTime": "1948-01-22T00:00:00.000",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "name": "USGS:05577500:00065:00003",
                        "sourceInfo": {
                            "siteName": "SPRING CREEK AT SPRINGFIELD, IL",
                            "siteCode": [{"value": "05577500", "agencyCode": "USGS"}],
                        },
                        "variable": {
                            "variableCode": [{"value": "00065"}],
                            "variableDescription": "Gage height, feet",
                            "unit": {"unitCode": "ft"},
                            "options": {
                                "option": [
                                    {
                                        "name": "Statistic",
                                        "optionCode": "00003",
                                        "value": "Mean",
                                    }
                                ]
                            },
                            "noDataValue": -999999.0,
                        },
                        "values": [
                            {
                                "qualifier": [
                                    {
                                        "qualifierCode": "A",
                                        "qualifierDescription": (
                                            "Approved for publication -- Processing and review completed."
                                        ),
                                    }
                                ],
                                "value": [
                                    {
                                        "value": "4.72",
                                        "qualifiers": ["A"],
                                        "dateTime": "1993-10-01T00:00:00.000",
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        }
    )


def test_parse_annual_peaks_rdb_decodes_known_spring_creek_records():
    records = nwis.parse_annual_peaks_rdb(SPRING_CREEK_PEAKS_RDB)

    assert len(records) == 2
    assert records[0]["site_no"] == "05577500"
    assert records[0]["water_year"] == 1948
    assert records[0]["peak_cfs"] == 1050.0
    assert records[0]["gage_height_ft"] == 9.05
    assert records[0]["qualification_flags"] == []

    coded = records[1]
    assert coded["water_year"] == 1961
    assert coded["peak_cd"] == ["2", "7"]
    assert coded["gage_ht_cd"] == ["1"]
    assert "Estimate" in coded["peak_flags"][0]["description"]
    assert "backwater" in coded["gage_height_flags"][0]["description"]


def test_parse_waterml_json_extracts_parameters_values_and_quality_flags():
    series = nwis.parse_waterml_json(_waterml_json(), service="dv")

    assert len(series) == 2
    flow = series[0]
    stage = series[1]

    assert flow.site_no == "05577500"
    assert flow.parameter_code == "00060"
    assert flow.parameter_name == "Discharge, cubic feet per second"
    assert flow.statistic_code == "00003"
    assert flow.units == "ft3/s"
    assert flow.records[0]["value"] == 4.60
    assert flow.records[0]["quality_flags"][0]["code"] == "A"
    assert flow.records[1]["value"] is None
    assert flow.records[1]["qualifiers"] == ["P", "e"]
    assert "Provisional" in flow.records[1]["quality_flags"][0]["description"]
    assert "estimated" in flow.records[1]["quality_flags"][1]["description"]

    assert stage.parameter_code == "00065"
    assert stage.units == "ft"
    assert stage.records[0]["datetime"] == "1993-10-01T00:00:00.000"


def test_get_daily_values_uses_fresh_cache(monkeypatch, tmp_path: Path):
    calls = []

    def fake_download(url, params, *, timeout=60):
        calls.append((url, dict(params), timeout))
        return _waterml_json()

    monkeypatch.setattr(nwis, "_download_text", fake_download)

    first = nwis.get_daily_values(
        "USGS-05577500",
        ["discharge", "stage"],
        start_date="1948-01-21",
        end_date="1948-01-22",
        cache_dir=tmp_path,
    )
    second = nwis.get_daily_values(
        "05577500",
        ["00060", "00065"],
        start_date="1948-01-21",
        end_date="1948-01-22",
        cache_dir=tmp_path,
    )

    assert len(calls) == 1
    assert calls[0][0].endswith("/dv/")
    assert calls[0][1]["sites"] == "05577500"
    assert calls[0][1]["parameterCd"] == "00060,00065"
    assert calls[0][1]["statCd"] == "00003"
    assert first.cache.from_cache is False
    assert second.cache.from_cache is True
    assert second.cache.fresh is True
    assert Path(second.cache.body_path).exists()
    assert Path(second.cache.metadata_path).exists()
    assert len(second.records) == 3


def test_get_instantaneous_values_handles_precipitation_parameter(monkeypatch):
    calls = []

    def fake_download(url, params, *, timeout=60):
        calls.append((url, dict(params)))
        return _waterml_json()

    monkeypatch.setattr(nwis, "_download_text", fake_download)

    result = nwis.get_instantaneous_values(
        ["05577500"],
        ["precipitation", "stage"],
        period="P1D",
        cache_dir=None,
    )

    assert calls[0][0].endswith("/iv/")
    assert calls[0][1]["parameterCd"] == "00045,00065"
    assert "statCd" not in calls[0][1]
    assert result.request_params["period"] == "P1D"


def test_parse_nldi_sites_normalizes_upstream_gauges():
    payload = {
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "identifier": "USGS-05577500",
                    "name": "SPRING CREEK AT SPRINGFIELD, IL",
                    "uri": "https://labs.waterdata.usgs.gov/api/nldi/linked-data/nwissite/USGS-05577500",
                    "source": "nwissite",
                },
                "geometry": {"type": "Point", "coordinates": [-89.6994167, 39.81541667]},
            },
            {
                "type": "Feature",
                "properties": {
                    "uri": "https://labs.waterdata.usgs.gov/api/nldi/linked-data/nwissite/USGS-05577600",
                    "name": "UPSTREAM TEST GAGE",
                    "sourceName": "NWIS Surface Water Sites",
                    "source": "nwissite",
                },
                "geometry": {"type": "Point", "coordinates": [-89.6, 39.9]},
            },
        ]
    }

    sites = nwis.parse_nldi_sites(payload)

    assert [site.site_no for site in sites] == ["05577500", "05577600"]
    assert sites[0].latitude == 39.81541667
    assert sites[0].longitude == -89.6994167
    assert sites[1].name == "UPSTREAM TEST GAGE"


def test_get_basin_daily_values_discovers_upstream_sites_and_chunks(monkeypatch):
    monkeypatch.setattr(
        nwis,
        "get_upstream_nwis_sites",
        lambda *args, **kwargs: [
            nwis.NwisSite(site_no="05577600", name="Upstream 1"),
            nwis.NwisSite(site_no="05577700", name="Upstream 2"),
        ],
    )
    calls = []

    def fake_daily_values(sites, parameters, **kwargs):
        calls.append((list(sites), parameters, kwargs))
        return nwis.NwisTimeSeriesCollection(
            service="dv",
            sites=list(sites),
            parameter_codes=nwis.resolve_parameter_codes(parameters),
            series=[],
            request_params={},
            source_url="https://example.test/nwis/dv",
        )

    monkeypatch.setattr(nwis, "get_daily_values", fake_daily_values)

    result = nwis.get_basin_daily_values(
        "05577500",
        parameters=["discharge"],
        distance_km=25.0,
        chunk_size=2,
        period="P7D",
    )

    assert result.outlet_site_no == "05577500"
    assert result.navigation == "UT"
    assert [site.site_no for site in result.sites] == ["05577600", "05577700"]
    assert [call[0] for call in calls] == [["05577500", "05577600"], ["05577700"]]
    assert calls[0][1] == ["discharge"]
    assert calls[0][2]["period"] == "P7D"
