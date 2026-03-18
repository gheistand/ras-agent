---
description: Path handling conventions for pipeline modules
globs: pipeline/**
---

# Path Handling

## Accept Both, Use pathlib Internally

All public functions that accept file or directory paths must accept both `str` and `Path`:

```python
from pathlib import Path
from typing import Union

def load_dem(dem_path: Union[str, Path]) -> np.ndarray:
    dem_path = Path(dem_path)   # normalize at the top of the function
    ...
```

## Rules
- Always normalize to `pathlib.Path` at the top of the function body
- Never pass raw strings to functions expecting paths internally — use `Path` objects throughout
- Use `/` operator for joining: `output_dir / "terrain" / "dem.tif"` not `os.path.join(...)`
- Use `path.stem`, `path.suffix`, `path.parent` etc. — never slice strings to extract path components
- `exist_ok=True` on `mkdir` calls: `output_dir.mkdir(parents=True, exist_ok=True)`

## Output Path Construction Pattern

```python
def run_stage(output_dir: Union[str, Path]) -> Path:
    output_dir = Path(output_dir)
    terrain_dir = output_dir / "terrain"
    terrain_dir.mkdir(parents=True, exist_ok=True)
    result_path = terrain_dir / "dem_5070.tif"
    ...
    return result_path
```

## What Not To Do

```python
# Bad — string slicing for path components
basename = path.split("/")[-1].split(".")[0]

# Bad — os.path.join when pathlib is available
out = os.path.join(str(output_dir), "terrain", "dem.tif")

# Bad — hardcoded separators
path = output_dir + "/terrain/dem.tif"
```
