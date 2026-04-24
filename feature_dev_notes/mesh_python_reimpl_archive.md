# Archived: Python Reimplementations of RasMapperLib Mesh Algorithms

**Date**: 2026-04-21 (updated 2026-04-22)
**Context**: During headless mesh generation work, we reimplemented several RasMapperLib.dll algorithms in Python. These were replaced by direct .NET calls via pythonnet. Code moved here from `ras_commander/geom/GeomMesh.py`.

## Why These Were Removed

RasMapperLib.dll already exposes:
- `PointGenerator.EnforceBreaklines()` — 5-step breakline seed pipeline
- `PointGenerator.GeneratePoints()` — base grid seed generation
- `MeshFV2D.TryAutoFix()` — duplicate removal + MaxFaces midpoints + perimeter dedup
- `MeshPerimeterEditor.TryToFixMeshes()` — "Fix All Meshes" button logic

Python reimplementations introduced bugs:
- Perpendicular offset seeds placed <1m from perimeter at breakline endpoints
- Snap-based dedup with escalating radius destroyed 74% of seeds
- Missing C# TryAutoFix priority logic (cell changes > perimeter changes)
- `_compute_filter_tolerance` returned 0.03m, too tight for MeshFV2D's internal threshold

## Functions Archived

| Function | Purpose | Replaced By |
|----------|---------|-------------|
| `_shift_breaklines()` | Coordinate-shift breaklines for float32 | Never used — shift handled in `_generate_seeds_safe()` |
| `_parse_geom_text()` | Parse perimeter/breaklines from .g01 text | `RASGeometry(hdf)` + `d2fa.Geometry` |
| `_build_polygon_from_coords()` | Python coords → .NET Polygon | `d2fa.Geometry.MeshPerimeters.Polygon(fid)` |
| `_build_breaklines_from_text()` | Python coords → .NET PolylineFeatureLayer | `_build_breaklines(d2fa, ns)` from .NET layers |
| `_generate_breakline_seeds()` | Python EnforceBreaklines reimplementation | `_generate_seeds_via_net()` (.NET RegenerateMeshPoints) |
| `_compute_filter_tolerance()` | Python GetFilterTolerance reimplementation | Not needed — .NET handles dedup internally |
| `_find_duplicate_seeds()` | Python RemoveDuplicatePoints reimplementation | Not needed — .NET handles dedup internally |
| `_remove_seeds_by_index()` | Remove seeds by index from PointMs | Not needed — .NET handles dedup internally |
| `_add_seed()` | Append single PointM to PointMs | Inline where needed |
| `_seeds_from_multipoint()` | Read seeds from Geometry.MeshPoints | Not needed — seeds come from RegenerateMeshPoints |

## ILSpy References

- `ilspy_pointgenerator.txt:1600` — EnforceBreaklines (5-step pipeline)
- `ilspy_pointgenerator.txt:2064` — ComputeOffsetSpacing
- `ilspy_pointgenerator.txt:2127` — DeletePointsAroundBreaklines_Step4
- `ilspy_pointgenerator.txt:2353` — GeneratePointsAroundBreaklines_Step5
- `ilspy_meshfv2d.txt:4768` — TryAutoFix
- `ilspy_meshperimetereditor.txt:379` — TryToFixMeshes

## Lessons Learned
- Don't reimplement .NET algorithms in Python when the DLL is loaded
- ILSpy decompilation is for UNDERSTANDING the API, not for porting code
- The coordinate shift for float32 overflow IS needed (PointGenerator uses System.Single)
- Text file I/O (parsing/patching .g01) IS needed (no .NET alternative)
- Breakline spacing must be synced to HDF before .NET loads it (stale HDF bug)

---

## Archived Code

### `_shift_breaklines()`
```python
def _shift_breaklines(breaklines, dx: float, dy: float, ns: dict):
    """Create shifted copy of breakline PolylineFeatureLayer."""
    from RasMapperLib import Polyline as _Polyline  # type: ignore
    from RasMapperLib import PolylineFeatureLayer as _PLFL  # type: ignore
    from RasMapperLib import PointMs as _PointMs  # type: ignore
    from RasMapperLib import PointM as _PointM  # type: ignore

    out = _PLFL("bl")
    try:
        for bl in breaklines.Polylines():
            n = bl.Count
            pts = _PointMs()
            for i in range(n):
                p = bl.PointM(i)
                pts.Add(_PointM(float(p.X) + dx, float(p.Y) + dy))
            new_pl = _Polyline(pts)
            if _Polyline.IsValidPolyline(new_pl):
                out.AddFeature(new_pl)
    except Exception:
        return breaklines
    return out
```

### `_parse_geom_text()`
```python
def _parse_geom_text(geom_text_path: str, area_name: Optional[str] = None) -> dict:
    """Parse perimeter and breakline coordinates from .g01 text file."""
    text = Path(geom_text_path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    result = {"perimeter": [], "breaklines": [], "area_name": area_name or ""}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Storage Area=") and not result["area_name"]:
            result["area_name"] = line.split("=", 1)[1].split(",")[0].strip()
        if line.startswith("Storage Area Surface Line="):
            count = int(line.split("=")[1].strip())
            coords = []
            i += 1
            while i < len(lines) and len(coords) < count * 2:
                cline = lines[i]
                for j in range(0, len(cline), 16):
                    chunk = cline[j:j + 16].strip()
                    if chunk:
                        try:
                            coords.append(float(chunk))
                        except ValueError:
                            break
                i += 1
            result["perimeter"] = [
                (coords[k], coords[k + 1]) for k in range(0, len(coords), 2)
            ]
            continue
        if line.startswith("BreakLine Name="):
            bl_name = line.split("=", 1)[1].strip()
            bl_coords = []
            i += 1
            while i < len(lines) and not lines[i].startswith("BreakLine Polyline="):
                i += 1
            if i < len(lines) and lines[i].startswith("BreakLine Polyline="):
                bl_count = int(lines[i].split("=")[1].strip())
                i += 1
                while i < len(lines) and len(bl_coords) < bl_count * 2:
                    cline = lines[i]
                    for j in range(0, len(cline), 16):
                        chunk = cline[j:j + 16].strip()
                        if chunk:
                            try:
                                bl_coords.append(float(chunk))
                            except ValueError:
                                break
                    i += 1
                result["breaklines"].append({
                    "name": bl_name,
                    "coords": [
                        (bl_coords[k], bl_coords[k + 1])
                        for k in range(0, len(bl_coords), 2)
                    ],
                })
            continue
        i += 1
    return result
```

### `_build_polygon_from_coords()`
```python
def _build_polygon_from_coords(coords: list, ns: dict):
    """Create .NET Polygon from Python coordinate list."""
    from RasMapperLib import PointMs as _PointMs, Polygon as _Polygon  # type: ignore
    from RasMapperLib import PointM as _PointM  # type: ignore
    pt_list = _PointMs()
    for x, y in coords:
        pt_list.Add(_PointM(float(x), float(y)))
    return _Polygon(pt_list)
```

### `_build_breaklines_from_text()`
```python
def _build_breaklines_from_text(bl_defs: list, ns: dict):
    """Create .NET breakline PolylineFeatureLayer from parsed text data."""
    from RasMapperLib import (  # type: ignore
        PolylineFeatureLayer as _PLFL,
        Polyline as _Polyline,
        PointMs as _PointMs,
        PointM as _PointM,
    )
    combined = _PLFL("bl")
    n = 0
    for bl in bl_defs:
        pts = _PointMs()
        for x, y in bl["coords"]:
            pts.Add(_PointM(float(x), float(y)))
        pl = _Polyline(pts)
        if _Polyline.IsValidPolyline(pl):
            combined.AddFeature(pl)
            n += 1
    if n == 0:
        return None
    return combined.CopyToMultiPartPolyline()
```

### `_generate_breakline_seeds()`
```python
def _generate_breakline_seeds(
    perim, breakline_defs: list, cell_size: float,
    bl_spacing: float, near_repeats: int, ns: dict,
):
    """DEPRECATED: Python reimplementation of EnforceBreaklines.

    Produces ~5,000 excess breakline-corridor points vs RAS Mapper.
    Use _generate_seeds_via_net() instead, which calls .NET
    PointGenerator.RegenerateMeshPoints with the full 5-step pipeline.
    """
    import math
    from shapely.geometry import LineString, Point, Polygon as ShapelyPolygon
    from shapely.ops import unary_union
    from shapely.prepared import prep as _prep

    base_seeds = _generate_seeds_safe(perim, cell_size, ns)
    base_list = [(float(base_seeds[i].X), float(base_seeds[i].Y))
                 for i in range(base_seeds.Count)]
    n = perim.Count
    perim_coords = [(float(perim.PointM(i).X), float(perim.PointM(i).Y))
                    for i in range(n)]
    if perim_coords and perim_coords[0] != perim_coords[-1]:
        perim_coords.append(perim_coords[0])
    basin = ShapelyPolygon(perim_coords)
    edge_buffer = bl_spacing * 0.25
    basin_inner = basin.buffer(-edge_buffer)
    prep_basin = _prep(basin_inner)
    offsets = [bl_spacing * 0.5 + k * bl_spacing for k in range(near_repeats + 1)]
    last_offset = offsets[-1]
    deletion_dist = last_offset + bl_spacing * 0.75
    bl_lines = []
    for bl_def in breakline_defs:
        coords = bl_def["coords"]
        if len(coords) < 2:
            continue
        line = LineString(coords)
        if line.length < bl_spacing:
            continue
        bl_lines.append(line)
    if bl_lines:
        deletion_polys = [ln.buffer(deletion_dist, cap_style=2) for ln in bl_lines]
        combined_zone = unary_union(deletion_polys)
        prep_zone = _prep(combined_zone)
        filtered_base = [(x, y) for x, y in base_list
                         if not prep_zone.contains(Point(x, y))]
    else:
        filtered_base = base_list
    bl_seeds = set()
    for line in bl_lines:
        n_pts = max(2, int(line.length / bl_spacing) + 1)
        discretized = []
        for j in range(n_pts):
            d = min(j * bl_spacing, line.length)
            pt = line.interpolate(d)
            discretized.append((pt.x, pt.y))
        end_pt = line.interpolate(line.length)
        if discretized[-1] != (end_pt.x, end_pt.y):
            discretized.append((end_pt.x, end_pt.y))
        for seg_idx in range(len(discretized) - 1):
            p0 = discretized[seg_idx]
            p1 = discretized[seg_idx + 1]
            mid_x = (p0[0] + p1[0]) * 0.5
            mid_y = (p0[1] + p1[1]) * 0.5
            dx = p1[0] - p0[0]
            dy = p1[1] - p0[1]
            seg_len = math.sqrt(dx * dx + dy * dy)
            if seg_len < 1e-6:
                continue
            px = -dy / seg_len
            py = dx / seg_len
            for offset in offsets:
                for sign in (1, -1):
                    sx = mid_x + sign * offset * px
                    sy = mid_y + sign * offset * py
                    if prep_basin.contains(Point(sx, sy)):
                        bl_seeds.add((round(sx, 6), round(sy, 6)))
    perim_ring = basin.exterior
    min_perim_dist = edge_buffer
    bl_seeds_filtered = set()
    for sx, sy in bl_seeds:
        if perim_ring.distance(Point(sx, sy)) >= min_perim_dist:
            bl_seeds_filtered.add((sx, sy))
    snap = max(bl_spacing * 0.25, 1.0)
    seen = set()
    deduped = []
    for x, y in filtered_base + list(bl_seeds_filtered):
        key = (round(x / snap), round(y / snap))
        if key not in seen:
            seen.add(key)
            deduped.append((x, y))
    from RasMapperLib import PointMs as _PointMs, PointM as _PointM  # type: ignore
    pm = _PointMs()
    for x, y in deduped:
        pm.Add(_PointM(float(x), float(y)))
    return pm
```

### `_compute_filter_tolerance()`
```python
def _compute_filter_tolerance(perim) -> float:
    """Match C# GetFilterTolerance: min(0.03, extent_diag * 0.0001)."""
    import math
    default = 0.03
    try:
        n = perim.Count
        xs = [float(perim.PointM(i).X) for i in range(n)]
        ys = [float(perim.PointM(i).Y) for i in range(n)]
        dx = max(xs) - min(xs)
        dy = max(ys) - min(ys)
        diag = math.sqrt(dx * dx + dy * dy)
        if diag <= 0 or math.isnan(diag):
            return default
        return min(default, diag * 0.0001)
    except Exception:
        return default
```

### `_find_duplicate_seeds()`
```python
def _find_duplicate_seeds(seeds_pm, filter_tol: float) -> list:
    """Find duplicate seeds within filter_tol distance."""
    import numpy as np
    n = seeds_pm.Count
    if n < 2:
        return []
    coords = np.empty((n, 2), dtype=np.float64)
    for i in range(n):
        coords[i, 0] = float(seeds_pm[i].X)
        coords[i, 1] = float(seeds_pm[i].Y)
    remove_set = set()
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(coords)
        pairs = tree.query_pairs(r=filter_tol, output_type='ndarray')
        for pair in pairs:
            remove_set.add(int(max(pair[0], pair[1])))
    except ImportError:
        tol_sq = filter_tol * filter_tol
        cell = max(filter_tol, 1e-9)
        grid: dict = {}
        for i in range(n):
            gx = int(coords[i, 0] / cell)
            gy = int(coords[i, 1] / cell)
            found = False
            for dxo in (-1, 0, 1):
                if found:
                    break
                for dyo in (-1, 0, 1):
                    key = (gx + dxo, gy + dyo)
                    for j in grid.get(key, []):
                        if j in remove_set:
                            continue
                        ddx = coords[i, 0] - coords[j, 0]
                        ddy = coords[i, 1] - coords[j, 1]
                        if ddx * ddx + ddy * ddy < tol_sq:
                            remove_set.add(i)
                            found = True
                            break
                    if found:
                        break
            if i not in remove_set:
                grid.setdefault((gx, gy), []).append(i)
    return sorted(remove_set)
```

### `_remove_seeds_by_index()`
```python
def _remove_seeds_by_index(seeds_pm, remove_indices: list, ns: dict):
    """Return new PointMs with specified indices removed."""
    if not remove_indices:
        return seeds_pm
    remove_set = set(remove_indices)
    from RasMapperLib import PointMs as _PointMs  # type: ignore
    new_pm = _PointMs()
    for i in range(seeds_pm.Count):
        if i not in remove_set:
            new_pm.Add(seeds_pm[i])
    return new_pm
```

### `_add_seed()`
```python
def _add_seed(seeds_pm, point_m, ns):
    """Append a single PointM to a PointMs collection (returns new copy)."""
    from RasMapperLib import PointMs as _PointMs  # type: ignore
    new_pm = _PointMs()
    for i in range(seeds_pm.Count):
        new_pm.Add(seeds_pm[i])
    new_pm.Add(point_m)
    return new_pm
```

### `_seeds_from_multipoint()`
```python
def _seeds_from_multipoint(d2fa, ns: dict):
    """Read existing seeds from Geometry.MeshPoints."""
    pts = []
    try:
        for mp in d2fa.Geometry.MeshPoints.Points():
            count = mp.Count
            for i in range(count):
                pts.append(mp.PointM(i))
    except Exception:
        pass
    return pts
```
