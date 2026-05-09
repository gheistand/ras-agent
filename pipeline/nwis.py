"""
nwis.py - USGS NWIS gauge data retrieval and parsing helpers.

This module provides a small direct web-services surface for gauge records used
by calibration and validation workflows. It intentionally keeps raw service
responses cached so reviewers can inspect the source data behind parsed series.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

NWIS_WATER_SERVICES_BASE = os.getenv(
    "NWIS_WATER_SERVICES_BASE",
    "https://waterservices.usgs.gov/nwis",
).rstrip("/")
NWIS_LEGACY_BASE = os.getenv(
    "NWIS_LEGACY_BASE",
    "https://nwis.waterdata.usgs.gov/nwis",
).rstrip("/")
NLDI_BASE = os.getenv(
    "NLDI_BASE",
    "https://api.water.usgs.gov/nldi",
).rstrip("/")

DEFAULT_CACHE_DIR = Path("workspace/.cache/nwis")
DEFAULT_SITE_STATUS = "all"
DEFAULT_DAILY_STATISTIC = "00003"  # mean

PARAMETER_CODES = {
    "discharge": "00060",
    "flow": "00060",
    "streamflow": "00060",
    "stage": "00065",
    "gage_height": "00065",
    "gauge_height": "00065",
    "precipitation": "00045",
    "precip": "00045",
    "rainfall": "00045",
}

PARAMETER_NAMES = {
    "00060": "discharge",
    "00065": "stage",
    "00045": "precipitation",
}

QUALITY_FLAG_DESCRIPTIONS = {
    "A": "Approved for publication -- processing and review completed.",
    "P": "Provisional data subject to revision.",
    "e": "Value has been estimated.",
    "R": "Revised.",
}

PEAK_CODE_DESCRIPTIONS = {
    "1": "Discharge is a Maximum Daily Average.",
    "2": "Discharge is an Estimate.",
    "3": "Discharge affected by Dam Failure.",
    "4": "Discharge less than indicated value which is Minimum Recordable Discharge at this site.",
    "5": "Discharge affected to unknown degree by Regulation or Diversion.",
    "6": "Discharge affected by Regulation or Diversion.",
    "7": "Discharge is an Historic Peak.",
    "8": "Discharge actually greater than indicated value.",
    "9": "Discharge due to Snowmelt, Hurricane, Ice-Jam or Debris Dam breakup.",
    "A": "Year of occurrence is unknown or not exact.",
    "Bd": "Day of occurrence is unknown or not exact.",
    "Bm": "Month of occurrence is unknown or not exact.",
    "C": "All or part of the record affected by basin changes.",
    "D": "Base Discharge changed during this year.",
    "E": "Only Annual Maximum Peak available for this year.",
    "F": "Peak supplied by another agency.",
    "O": "Opportunistic value not from systematic data collection.",
    "R": "Revised.",
}

GAGE_HEIGHT_CODE_DESCRIPTIONS = {
    "1": "Gage height affected by backwater.",
    "2": "Gage height not the maximum for the year.",
    "3": "Gage height was at a different site and/or datum.",
    "4": "Gage height below minimum recordable elevation.",
    "5": "Gage height is an estimate.",
    "6": "Gage height datum changed during this year.",
    "7": "Debris, mud, or hyper-concentrated flow.",
    "8": "Gage height tidally affected.",
    "Bd": "Day of occurrence is unknown or not exact.",
    "Bm": "Month of occurrence is unknown or not exact.",
    "F": "Peak supplied by another agency.",
    "R": "Revised.",
}


@dataclass
class NwisCacheInfo:
    """Metadata about a raw NWIS/NLDI response used to create parsed results."""

    body_path: Optional[str]
    metadata_path: Optional[str]
    url: str
    retrieved_at_utc: Optional[str] = None
    expires_at_utc: Optional[str] = None
    max_age_hours: Optional[float] = None
    fresh: bool = False
    from_cache: bool = False
    stale_fallback: bool = False
    error: Optional[str] = None


@dataclass
class NwisTimeSeries:
    """Parsed WaterML JSON time series for one site/parameter/statistic."""

    service: str
    site_no: str
    site_name: Optional[str]
    parameter_code: str
    parameter_name: Optional[str]
    statistic_code: Optional[str]
    units: Optional[str]
    records: list[dict[str, Any]]
    qualifier_definitions: dict[str, str] = field(default_factory=dict)


@dataclass
class NwisTimeSeriesCollection:
    """Collection returned by a daily or instantaneous values query."""

    service: str
    sites: list[str]
    parameter_codes: list[str]
    series: list[NwisTimeSeries]
    request_params: dict[str, str]
    source_url: str
    cache: Optional[NwisCacheInfo] = None
    retrieved_at_utc: str = field(default_factory=lambda: _utc_timestamp())

    @property
    def records(self) -> list[dict[str, Any]]:
        """Return all records with site and parameter fields included."""
        rows: list[dict[str, Any]] = []
        for series in self.series:
            for record in series.records:
                row = dict(record)
                row.update(
                    {
                        "service": series.service,
                        "site_no": series.site_no,
                        "parameter_code": series.parameter_code,
                        "parameter_name": series.parameter_name,
                        "statistic_code": series.statistic_code,
                        "units": series.units,
                    }
                )
                rows.append(row)
        return rows


@dataclass
class NwisPeakSeries:
    """Parsed annual peak streamflow records for one USGS site."""

    site_no: str
    records: list[dict[str, Any]]
    source_url: str
    cache: Optional[NwisCacheInfo] = None
    retrieved_at_utc: str = field(default_factory=lambda: _utc_timestamp())


@dataclass
class NwisSite:
    """USGS site discovered from NLDI upstream network navigation."""

    site_no: str
    name: Optional[str] = None
    uri: Optional[str] = None
    source: Optional[str] = None
    longitude: Optional[float] = None
    latitude: Optional[float] = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class NwisBasinResult:
    """Basin-wide NWIS result for sites upstream of an outlet gauge."""

    outlet_site_no: str
    navigation: str
    distance_km: float
    sites: list[NwisSite]
    collections: list[NwisTimeSeriesCollection] = field(default_factory=list)
    peak_series: list[NwisPeakSeries] = field(default_factory=list)

    @property
    def series(self) -> list[NwisTimeSeries]:
        return [
            series
            for collection in self.collections
            for series in collection.series
        ]

    @property
    def records(self) -> list[dict[str, Any]]:
        return [
            record
            for collection in self.collections
            for record in collection.records
        ]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp(value: Optional[datetime] = None) -> str:
    dt = value or _utc_now()
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _request_url(url: str, params: Mapping[str, Any]) -> str:
    query = urlencode(
        sorted((key, str(value)) for key, value in params.items() if value is not None),
        doseq=False,
    )
    return f"{url}?{query}" if query else url


def _cache_paths(
    cache_dir: Path,
    *,
    prefix: str,
    url: str,
    params: Mapping[str, Any],
    suffix: str,
) -> tuple[Path, Path]:
    key = hashlib.sha256(_request_url(url, params).encode("utf-8")).hexdigest()[:20]
    body_path = cache_dir / f"{prefix}_{key}{suffix}"
    metadata_path = cache_dir / f"{prefix}_{key}.metadata.json"
    return body_path, metadata_path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _download_text(url: str, params: Mapping[str, str], *, timeout: int = 60) -> str:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.text


def _cached_get_text(
    url: str,
    params: Mapping[str, str],
    *,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 24.0,
    force: bool = False,
    timeout: int = 60,
    prefix: str = "nwis",
    suffix: str = ".json",
) -> tuple[str, NwisCacheInfo]:
    request_url = _request_url(url, params)
    if cache_dir is None:
        text = _download_text(url, params, timeout=timeout)
        return text, NwisCacheInfo(
            body_path=None,
            metadata_path=None,
            url=request_url,
            retrieved_at_utc=_utc_timestamp(),
            max_age_hours=max_age_hours,
            fresh=True,
        )

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    body_path, metadata_path = _cache_paths(
        cache_path,
        prefix=prefix,
        url=url,
        params=params,
        suffix=suffix,
    )
    metadata = _read_json(metadata_path)
    expires_at = _parse_utc_timestamp(metadata.get("expires_at_utc"))
    now = _utc_now()
    is_fresh = bool(body_path.exists() and expires_at and now <= expires_at)

    if is_fresh and not force:
        return body_path.read_text(encoding="utf-8"), NwisCacheInfo(
            body_path=str(body_path),
            metadata_path=str(metadata_path),
            url=metadata.get("url", request_url),
            retrieved_at_utc=metadata.get("retrieved_at_utc"),
            expires_at_utc=metadata.get("expires_at_utc"),
            max_age_hours=metadata.get("max_age_hours", max_age_hours),
            fresh=True,
            from_cache=True,
        )

    try:
        text = _download_text(url, params, timeout=timeout)
    except httpx.HTTPError as exc:
        if body_path.exists():
            return body_path.read_text(encoding="utf-8"), NwisCacheInfo(
                body_path=str(body_path),
                metadata_path=str(metadata_path),
                url=metadata.get("url", request_url),
                retrieved_at_utc=metadata.get("retrieved_at_utc"),
                expires_at_utc=metadata.get("expires_at_utc"),
                max_age_hours=metadata.get("max_age_hours", max_age_hours),
                fresh=False,
                from_cache=True,
                stale_fallback=True,
                error=str(exc),
            )
        raise

    retrieved_at = now
    expires_at = retrieved_at + timedelta(hours=max_age_hours)
    body_path.write_text(text, encoding="utf-8")
    _write_json(
        metadata_path,
        {
            "url": request_url,
            "retrieved_at_utc": _utc_timestamp(retrieved_at),
            "expires_at_utc": _utc_timestamp(expires_at),
            "max_age_hours": max_age_hours,
            "params": dict(params),
        },
    )
    return text, NwisCacheInfo(
        body_path=str(body_path),
        metadata_path=str(metadata_path),
        url=request_url,
        retrieved_at_utc=_utc_timestamp(retrieved_at),
        expires_at_utc=_utc_timestamp(expires_at),
        max_age_hours=max_age_hours,
        fresh=True,
        from_cache=False,
    )


def normalize_site_no(site: str) -> str:
    """Return a plain USGS site number from common identifier forms."""
    text = str(site).strip()
    if not text:
        raise ValueError("site number cannot be empty")
    for prefix in ("USGS-", "USGS:", "nwissite/USGS-"):
        if prefix in text:
            text = text.split(prefix, 1)[1]
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    return text


def _normalize_sites(sites: str | Sequence[str]) -> list[str]:
    if isinstance(sites, str):
        candidates = re.split(r"[,;\s]+", sites.strip())
    else:
        candidates = list(sites)
    normalized = [normalize_site_no(site) for site in candidates if str(site).strip()]
    if not normalized:
        raise ValueError("at least one site number is required")
    return list(dict.fromkeys(normalized))


def resolve_parameter_codes(parameters: str | Sequence[str]) -> list[str]:
    """Resolve parameter aliases such as 'discharge' to USGS parameter codes."""
    if isinstance(parameters, str):
        candidates = re.split(r"[,;\s]+", parameters.strip())
    else:
        candidates = list(parameters)
    codes: list[str] = []
    for item in candidates:
        text = str(item).strip()
        if not text:
            continue
        code = PARAMETER_CODES.get(text.lower(), text)
        if not re.fullmatch(r"\d{5}", code):
            raise ValueError(f"Unsupported NWIS parameter '{item}'. Use a 5-digit code or known alias.")
        codes.append(code)
    if not codes:
        raise ValueError("at least one NWIS parameter is required")
    return list(dict.fromkeys(codes))


_PEAK_CODE_RE = re.compile(r"Bd|Bm|[A-Za-z]+|\d+")


def _split_peak_codes(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return _PEAK_CODE_RE.findall(text)


def _normalize_qualifiers(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part for part in re.split(r"[,;\s]+", text) if part]


def _decode_flags(
    codes: Iterable[str],
    descriptions: Mapping[str, str],
    *,
    field: str,
) -> list[dict[str, str]]:
    flags = []
    for code in codes:
        flags.append(
            {
                "field": field,
                "code": code,
                "description": descriptions.get(code, "Unknown USGS qualification code."),
            }
        )
    return flags


def _water_year(date_text: Any) -> Optional[int]:
    text = str(date_text or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text[:10])
    except ValueError:
        return None
    return dt.year + 1 if dt.month >= 10 else dt.year


def parse_annual_peaks_rdb(text: str) -> list[dict[str, Any]]:
    """
    Parse USGS annual peak streamflow RDB text into normalized records.

    The first non-comment row is treated as the header, the second as the RDB
    field-width/type row, and subsequent rows are annual peak records.
    """
    data_lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(data_lines) < 2:
        return []

    header = data_lines[0].split("\t")
    records: list[dict[str, Any]] = []
    for values in csv.reader(data_lines[2:], delimiter="\t"):
        if not values or all(not value.strip() for value in values):
            continue
        padded = values + [""] * max(0, len(header) - len(values))
        row = dict(zip(header, padded[: len(header)]))
        peak_codes = _split_peak_codes(row.get("peak_cd"))
        gage_codes = _split_peak_codes(row.get("gage_ht_cd"))
        peak_value = _float_or_none(row.get("peak_va"))
        gage_height = _float_or_none(row.get("gage_ht"))
        peak_flags = _decode_flags(peak_codes, PEAK_CODE_DESCRIPTIONS, field="peak_cd")
        gage_flags = _decode_flags(gage_codes, GAGE_HEIGHT_CODE_DESCRIPTIONS, field="gage_ht_cd")
        records.append(
            {
                "agency_cd": row.get("agency_cd") or None,
                "site_no": row.get("site_no") or None,
                "peak_dt": row.get("peak_dt") or None,
                "peak_tm": row.get("peak_tm") or None,
                "water_year": _water_year(row.get("peak_dt")),
                "peak_va": peak_value,
                "peak_cfs": peak_value,
                "peak_cd": peak_codes,
                "peak_flags": peak_flags,
                "gage_ht": gage_height,
                "gage_height_ft": gage_height,
                "gage_ht_cd": gage_codes,
                "gage_height_flags": gage_flags,
                "qualification_flags": peak_flags + gage_flags,
                "raw": row,
            }
        )
    return records


def _first_nested_value(record: Mapping[str, Any], keys: Sequence[str]) -> Any:
    value: Any = record
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _first_list_value(value: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(value, list) and value:
        item = value[0]
        return item if isinstance(item, Mapping) else None
    if isinstance(value, Mapping):
        return value
    return None


def _variable_code(variable: Mapping[str, Any]) -> Optional[str]:
    entry = _first_list_value(variable.get("variableCode"))
    if entry:
        return str(entry.get("value") or "").strip() or None
    return None


def _unit_code(variable: Mapping[str, Any]) -> Optional[str]:
    unit = variable.get("unit")
    if isinstance(unit, Mapping):
        return (
            str(unit.get("unitCode") or unit.get("unitAbbreviation") or "").strip()
            or None
        )
    return None


def _statistic_code(variable: Mapping[str, Any], name: Optional[str]) -> Optional[str]:
    options = _first_nested_value(variable, ("options", "option"))
    if isinstance(options, list):
        for option in options:
            if isinstance(option, Mapping) and option.get("name") == "Statistic":
                return str(option.get("optionCode") or "").strip() or None
    elif isinstance(options, Mapping) and options.get("name") == "Statistic":
        return str(options.get("optionCode") or "").strip() or None

    if name:
        parts = name.split(":")
        if len(parts) >= 4 and re.fullmatch(r"\d{5}", parts[3]):
            return parts[3]
    return None


def _site_code(source_info: Mapping[str, Any]) -> Optional[str]:
    entry = _first_list_value(source_info.get("siteCode"))
    if entry:
        value = entry.get("value")
        return normalize_site_no(str(value)) if value else None
    return None


def _qualifier_definitions(values_group: Mapping[str, Any]) -> dict[str, str]:
    definitions = dict(QUALITY_FLAG_DESCRIPTIONS)
    for qualifier in values_group.get("qualifier", []) or []:
        if not isinstance(qualifier, Mapping):
            continue
        code = str(qualifier.get("qualifierCode") or "").strip()
        description = str(qualifier.get("qualifierDescription") or "").strip()
        if code and description:
            definitions[code] = description
    return definitions


def _decode_quality_flags(
    qualifiers: Iterable[str],
    definitions: Mapping[str, str],
) -> list[dict[str, str]]:
    return _decode_flags(qualifiers, definitions, field="qualifiers")


def parse_waterml_json(data: str | Mapping[str, Any], *, service: str) -> list[NwisTimeSeries]:
    """Parse USGS WaterML JSON from the daily-values or IV service."""
    if isinstance(data, str):
        payload = json.loads(data)
    else:
        payload = dict(data)

    time_series = _first_nested_value(payload, ("value", "timeSeries")) or []
    parsed: list[NwisTimeSeries] = []
    for item in time_series:
        if not isinstance(item, Mapping):
            continue
        source_info = item.get("sourceInfo") or {}
        variable = item.get("variable") or {}
        name = item.get("name")
        parameter_code = _variable_code(variable)
        site_no = _site_code(source_info)
        if not parameter_code or not site_no:
            continue

        no_data_value = _float_or_none(variable.get("noDataValue"))
        qualifier_definitions: dict[str, str] = dict(QUALITY_FLAG_DESCRIPTIONS)
        records: list[dict[str, Any]] = []
        for values_group in item.get("values", []) or []:
            if not isinstance(values_group, Mapping):
                continue
            qualifier_definitions.update(_qualifier_definitions(values_group))
            for value_record in values_group.get("value", []) or []:
                if not isinstance(value_record, Mapping):
                    continue
                numeric_value = _float_or_none(value_record.get("value"))
                if (
                    numeric_value is not None
                    and no_data_value is not None
                    and numeric_value == no_data_value
                ):
                    numeric_value = None
                qualifiers = _normalize_qualifiers(value_record.get("qualifiers"))
                records.append(
                    {
                        "datetime": value_record.get("dateTime"),
                        "value": numeric_value,
                        "qualifiers": qualifiers,
                        "quality_flags": _decode_quality_flags(
                            qualifiers,
                            qualifier_definitions,
                        ),
                    }
                )

        parsed.append(
            NwisTimeSeries(
                service=service,
                site_no=site_no,
                site_name=source_info.get("siteName"),
                parameter_code=parameter_code,
                parameter_name=variable.get("variableDescription")
                or variable.get("variableName")
                or PARAMETER_NAMES.get(parameter_code),
                statistic_code=_statistic_code(variable, name if isinstance(name, str) else None),
                units=_unit_code(variable),
                records=records,
                qualifier_definitions=qualifier_definitions,
            )
        )
    return parsed


def _validate_time_filters(
    *,
    start_date: Optional[str],
    end_date: Optional[str],
    period: Optional[str],
) -> None:
    if period and (start_date or end_date):
        raise ValueError("Use either period or start/end dates, not both")
    if end_date and not start_date:
        raise ValueError("end_date requires start_date")


def _date_params(
    *,
    start_date: Optional[str],
    end_date: Optional[str],
    period: Optional[str],
) -> dict[str, str]:
    _validate_time_filters(start_date=start_date, end_date=end_date, period=period)
    params: dict[str, str] = {}
    if period:
        params["period"] = period
    if start_date:
        params["startDT"] = start_date
    if end_date:
        params["endDT"] = end_date
    return params


def _values_query(
    *,
    service: str,
    sites: str | Sequence[str],
    parameters: str | Sequence[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    period: Optional[str] = None,
    statistic_code: Optional[str] = None,
    site_status: str = DEFAULT_SITE_STATUS,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 24.0,
    force: bool = False,
    timeout: int = 60,
) -> NwisTimeSeriesCollection:
    site_numbers = _normalize_sites(sites)
    parameter_codes = resolve_parameter_codes(parameters)
    params = {
        "format": "json",
        "sites": ",".join(site_numbers),
        "parameterCd": ",".join(parameter_codes),
        "siteStatus": site_status,
    }
    params.update(_date_params(start_date=start_date, end_date=end_date, period=period))
    if statistic_code:
        params["statCd"] = statistic_code

    url = f"{NWIS_WATER_SERVICES_BASE}/{service}/"
    text, cache = _cached_get_text(
        url,
        params,
        cache_dir=cache_dir,
        max_age_hours=max_age_hours,
        force=force,
        timeout=timeout,
        prefix=f"nwis_{service}",
        suffix=".json",
    )
    series = parse_waterml_json(text, service=service)
    return NwisTimeSeriesCollection(
        service=service,
        sites=site_numbers,
        parameter_codes=parameter_codes,
        series=series,
        request_params=params,
        source_url=cache.url,
        cache=cache,
    )


def get_daily_values(
    sites: str | Sequence[str],
    parameters: str | Sequence[str] = ("discharge", "stage"),
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    period: Optional[str] = None,
    statistic_code: str = DEFAULT_DAILY_STATISTIC,
    site_status: str = DEFAULT_SITE_STATUS,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 24.0,
    force: bool = False,
    timeout: int = 60,
) -> NwisTimeSeriesCollection:
    """Retrieve daily values from the USGS NWIS daily-values web service."""
    return _values_query(
        service="dv",
        sites=sites,
        parameters=parameters,
        start_date=start_date,
        end_date=end_date,
        period=period,
        statistic_code=statistic_code,
        site_status=site_status,
        cache_dir=cache_dir,
        max_age_hours=max_age_hours,
        force=force,
        timeout=timeout,
    )


def get_instantaneous_values(
    sites: str | Sequence[str],
    parameters: str | Sequence[str] = ("discharge", "stage"),
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    period: Optional[str] = None,
    site_status: str = DEFAULT_SITE_STATUS,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 1.0,
    force: bool = False,
    timeout: int = 60,
) -> NwisTimeSeriesCollection:
    """Retrieve instantaneous/unit values from the USGS NWIS IV web service."""
    return _values_query(
        service="iv",
        sites=sites,
        parameters=parameters,
        start_date=start_date,
        end_date=end_date,
        period=period,
        statistic_code=None,
        site_status=site_status,
        cache_dir=cache_dir,
        max_age_hours=max_age_hours,
        force=force,
        timeout=timeout,
    )


def get_annual_peaks(
    site: str,
    *,
    begin_date: Optional[str] = None,
    end_date: Optional[str] = None,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 24.0 * 30,
    force: bool = False,
    timeout: int = 60,
) -> NwisPeakSeries:
    """Retrieve and parse annual peak streamflow RDB records for a USGS site."""
    site_no = normalize_site_no(site)
    params = {
        "site_no": site_no,
        "agency_cd": "USGS",
        "format": "rdb",
    }
    if begin_date:
        params["begin_date"] = begin_date
    if end_date:
        params["end_date"] = end_date

    url = f"{NWIS_LEGACY_BASE}/peak"
    text, cache = _cached_get_text(
        url,
        params,
        cache_dir=cache_dir,
        max_age_hours=max_age_hours,
        force=force,
        timeout=timeout,
        prefix="nwis_peak",
        suffix=".rdb",
    )
    records = parse_annual_peaks_rdb(text)
    return NwisPeakSeries(
        site_no=site_no,
        records=records,
        source_url=cache.url,
        cache=cache,
    )


def parse_nldi_sites(data: str | Mapping[str, Any]) -> list[NwisSite]:
    """Parse NLDI GeoJSON features into upstream NWIS site records."""
    payload = json.loads(data) if isinstance(data, str) else dict(data)
    sites: list[NwisSite] = []
    for feature in payload.get("features", []) or []:
        if not isinstance(feature, Mapping):
            continue
        props = feature.get("properties") or {}
        site_no = _nldi_site_no_from_properties(props)
        if not site_no:
            continue
        coords = feature.get("geometry", {}).get("coordinates")
        lon = lat = None
        if isinstance(coords, list) and len(coords) >= 2:
            lon = _float_or_none(coords[0])
            lat = _float_or_none(coords[1])
        sites.append(
            NwisSite(
                site_no=site_no,
                name=props.get("name"),
                uri=props.get("uri"),
                source=props.get("source"),
                longitude=lon,
                latitude=lat,
                properties=dict(props),
            )
        )
    deduped: dict[str, NwisSite] = {}
    for site in sites:
        deduped.setdefault(site.site_no, site)
    return list(deduped.values())


def _nldi_site_no_from_properties(props: Mapping[str, Any]) -> Optional[str]:
    for key in ("identifier", "uri", "sourceName", "name"):
        value = props.get(key)
        if not value:
            continue
        site_no = normalize_site_no(str(value))
        if re.fullmatch(r"\d{5,15}", site_no):
            return site_no
    return None


def get_upstream_nwis_sites(
    site: str,
    *,
    navigation: str = "UT",
    distance_km: float = 100.0,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 24.0 * 7,
    force: bool = False,
    timeout: int = 60,
) -> list[NwisSite]:
    """
    Discover NWIS sites upstream of an outlet gauge using USGS NLDI navigation.

    The default navigation code, UT, follows upstream tributaries. UM can be used
    for upstream mainstem searches.
    """
    site_no = normalize_site_no(site)
    navigation_code = navigation.upper()
    params = {"distance": str(distance_km)}
    url = (
        f"{NLDI_BASE}/linked-data/nwissite/USGS-{site_no}"
        f"/navigation/{navigation_code}/nwissite"
    )
    text, _cache = _cached_get_text(
        url,
        params,
        cache_dir=cache_dir,
        max_age_hours=max_age_hours,
        force=force,
        timeout=timeout,
        prefix="nldi_nwissite",
        suffix=".json",
    )
    return parse_nldi_sites(text)


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(values), size):
        yield list(values[idx : idx + size])


def _basin_site_numbers(
    outlet_site_no: str,
    upstream_sites: Sequence[NwisSite],
    *,
    include_outlet: bool,
) -> list[str]:
    outlet = normalize_site_no(outlet_site_no)
    site_numbers = [site.site_no for site in upstream_sites]
    if include_outlet and outlet not in site_numbers:
        site_numbers.insert(0, outlet)
    return list(dict.fromkeys(site_numbers))


def get_basin_daily_values(
    outlet_site: str,
    parameters: str | Sequence[str] = ("discharge", "stage"),
    *,
    navigation: str = "UT",
    distance_km: float = 100.0,
    include_outlet: bool = True,
    chunk_size: int = 50,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    period: Optional[str] = None,
    statistic_code: str = DEFAULT_DAILY_STATISTIC,
    site_status: str = DEFAULT_SITE_STATUS,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 24.0,
    force: bool = False,
    timeout: int = 60,
) -> NwisBasinResult:
    """Retrieve daily values for NWIS sites upstream of an outlet gauge."""
    upstream_sites = get_upstream_nwis_sites(
        outlet_site,
        navigation=navigation,
        distance_km=distance_km,
        cache_dir=cache_dir,
        force=force,
        timeout=timeout,
    )
    site_numbers = _basin_site_numbers(
        outlet_site,
        upstream_sites,
        include_outlet=include_outlet,
    )
    collections = [
        get_daily_values(
            chunk,
            parameters,
            start_date=start_date,
            end_date=end_date,
            period=period,
            statistic_code=statistic_code,
            site_status=site_status,
            cache_dir=cache_dir,
            max_age_hours=max_age_hours,
            force=force,
            timeout=timeout,
        )
        for chunk in _chunks(site_numbers, chunk_size)
    ]
    return NwisBasinResult(
        outlet_site_no=normalize_site_no(outlet_site),
        navigation=navigation.upper(),
        distance_km=distance_km,
        sites=list(upstream_sites),
        collections=collections,
    )


def get_basin_instantaneous_values(
    outlet_site: str,
    parameters: str | Sequence[str] = ("discharge", "stage"),
    *,
    navigation: str = "UT",
    distance_km: float = 100.0,
    include_outlet: bool = True,
    chunk_size: int = 50,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    period: Optional[str] = None,
    site_status: str = DEFAULT_SITE_STATUS,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 1.0,
    force: bool = False,
    timeout: int = 60,
) -> NwisBasinResult:
    """Retrieve IV/unit values for NWIS sites upstream of an outlet gauge."""
    upstream_sites = get_upstream_nwis_sites(
        outlet_site,
        navigation=navigation,
        distance_km=distance_km,
        cache_dir=cache_dir,
        force=force,
        timeout=timeout,
    )
    site_numbers = _basin_site_numbers(
        outlet_site,
        upstream_sites,
        include_outlet=include_outlet,
    )
    collections = [
        get_instantaneous_values(
            chunk,
            parameters,
            start_date=start_date,
            end_date=end_date,
            period=period,
            site_status=site_status,
            cache_dir=cache_dir,
            max_age_hours=max_age_hours,
            force=force,
            timeout=timeout,
        )
        for chunk in _chunks(site_numbers, chunk_size)
    ]
    return NwisBasinResult(
        outlet_site_no=normalize_site_no(outlet_site),
        navigation=navigation.upper(),
        distance_km=distance_km,
        sites=list(upstream_sites),
        collections=collections,
    )


def get_basin_annual_peaks(
    outlet_site: str,
    *,
    navigation: str = "UT",
    distance_km: float = 100.0,
    include_outlet: bool = True,
    begin_date: Optional[str] = None,
    end_date: Optional[str] = None,
    cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
    max_age_hours: float = 24.0 * 30,
    force: bool = False,
    timeout: int = 60,
) -> NwisBasinResult:
    """Retrieve annual peak records for NWIS sites upstream of an outlet gauge."""
    upstream_sites = get_upstream_nwis_sites(
        outlet_site,
        navigation=navigation,
        distance_km=distance_km,
        cache_dir=cache_dir,
        force=force,
        timeout=timeout,
    )
    site_numbers = _basin_site_numbers(
        outlet_site,
        upstream_sites,
        include_outlet=include_outlet,
    )
    peak_series = [
        get_annual_peaks(
            site_no,
            begin_date=begin_date,
            end_date=end_date,
            cache_dir=cache_dir,
            max_age_hours=max_age_hours,
            force=force,
            timeout=timeout,
        )
        for site_no in site_numbers
    ]
    return NwisBasinResult(
        outlet_site_no=normalize_site_no(outlet_site),
        navigation=navigation.upper(),
        distance_km=distance_km,
        sites=list(upstream_sites),
        peak_series=peak_series,
    )
