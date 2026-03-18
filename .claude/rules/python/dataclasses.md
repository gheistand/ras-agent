---
description: Data type conventions for pipeline result objects
globs: pipeline/**
---

# Dataclasses and Data Types

## Result Types
- Use `@dataclass` for all result types (`WatershedResult`, `PeakFlowEstimates`, `HydrographSet`, `HecRasProject`, etc.)
- Type hints on all public functions and dataclass fields
- Include units in field names where ambiguous: `drainage_area_mi2`, `tc_hr`, `peak_flow_cfs`

## Example Pattern

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class WatershedResult:
    """Result of watershed delineation stage.

    Attributes:
        drainage_area_mi2: Drainage area in square miles.
        pour_point_lon: Pour point longitude (WGS84).
        pour_point_lat: Pour point latitude (WGS84).
        tc_hr: Time of concentration in hours (Kirpich method).
        main_channel_length_ft: Longest flow path in feet.
        main_channel_slope_ftft: Main channel slope in ft/ft (10–85% method).
        polygon_path: Path to watershed polygon GeoPackage (EPSG:5070).
    """
    drainage_area_mi2: float
    pour_point_lon: float
    pour_point_lat: float
    tc_hr: float
    main_channel_length_ft: float
    main_channel_slope_ftft: float
    polygon_path: Optional[str] = None
```

## Type Hints
- All public function signatures must have full type hints (parameters and return type)
- Use `Optional[X]` or `X | None` for nullable fields (prefer `Optional[X]` for consistency with existing code)
- Use `list[X]` and `dict[K, V]` (lowercase, Python 3.9+) for generic collections
