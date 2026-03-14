# Expert Liaison — Questions Queue

**Last updated:** 2026-03-14
**Status:** 0 blocking | 0 high | 0 informational

_No open questions. Queue is clear._

---

## Blocking
_(none)_

## High Priority
_(none)_

## Informational
_(none)_

---

## Answered / Archived

### A1: Perimeter update approach
- **From:** model_builder.py design review (2026-03-13)
- **Question:** Can the 2D flow area perimeter be updated via ASCII .g## file without touching HDF5 directly?
- **Response:** Yes — confirmed by Bill Katzenmeyer (CLB Engineering, 2026-03-13). HEC-RAS regenerates geometry HDF on next save/open. Mesh regeneration after perimeter change requires RASMapper (Ajith Sundarraj, CLB Engineering, building automation).
- **Status:** Answered — implemented in `model_builder.py:_write_perimeter_to_geometry_file()`

### A2: Tc method for IL agricultural watersheds
- **From:** hydrograph.py implementation
- **Question:** Which Tc method for Illinois ungauged agricultural watersheds 10–500 mi²?
- **Response:** Kirpich formula confirmed by Glenn. Valid for rural IL ag watersheds.
- **Status:** Answered — implemented in `hydrograph.py`

### A3: Peak rate factor default
- **From:** hydrograph.py implementation
- **Question:** 484 (standard) vs. 300 (flat terrain)?
- **Response:** 484 as default for Illinois. Use 300 only for watersheds with mean slope < 0.5% (flat prairie/deltaic).
- **Status:** Answered — implemented in `hydrograph.py`
