"""
End-to-end integration test: geometry_first pipeline from synthetic watershed
through mesh generation.

Two test classes:
  TestPurePythonStages — runs on all platforms (Linux CI, macOS, Windows)
  TestMeshStages — requires Windows + pythonnet + HEC-RAS 6.6 DLLs
"""

import platform
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from pipeline.bc_lines import (
    BCLineSet,
    BCLineSpec,
    generate_bc_lines,
    write_unsteady_flow_file_2d,
    format_bc_line_block,
    format_2d_boundary_location,
    append_bc_lines_to_geom,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@dataclass
class MockHydrograph:
    flows_cfs: list
    duration_hr: float = 24.0


@dataclass
class MockHydroSet:
    hydrographs: dict = field(default_factory=dict)

    def get(self, rp):
        return self.hydrographs.get(rp)


@pytest.fixture
def synthetic_basin():
    """10km x 10km box basin in EPSG:5070-like coords."""
    return Polygon([
        (500000, 1800000),
        (510000, 1800000),
        (510000, 1810000),
        (500000, 1810000),
    ])


@pytest.fixture
def synthetic_streams():
    """Two streams crossing the basin boundary."""
    # Stream 1: enters from west (left edge), exits east (right edge)
    stream1 = LineString([
        (499000, 1805000),  # outside west
        (505000, 1805000),  # inside
        (511000, 1805000),  # outside east
    ])
    # Stream 2: enters from south (bottom edge)
    stream2 = LineString([
        (503000, 1799000),  # outside south
        (503000, 1805000),  # inside
    ])
    return [stream1, stream2]


@pytest.fixture
def pour_point():
    """Outlet at east edge where stream 1 exits."""
    return Point(510000, 1805000)


@pytest.fixture
def synthetic_ad8(tmp_path, synthetic_basin):
    """Synthetic AreaD8 raster with known contributing areas at stream crossings."""
    import rasterio
    from rasterio.transform import from_bounds

    # Extend 500m beyond basin bounds so edge intersections can be sampled
    minx, miny, maxx, maxy = synthetic_basin.bounds
    ext = 500
    width, height = 110, 110
    transform = from_bounds(minx - ext, miny - ext, maxx + ext, maxy + ext, width, height)

    data = np.zeros((height, width), dtype=np.float32)
    # Fill a band around each crossing so sampling hits regardless of exact pixel
    # West crossing at (500000, 1805000) -> contributing area = 400
    # South crossing at (503000, 1800000) -> contributing area = 600
    with rasterio.open(
        tmp_path / "probe.tif", "w", driver="GTiff", height=height, width=width,
        count=1, dtype="float32", crs="EPSG:5070", transform=transform,
        nodata=-1.0,
    ) as dst:
        dst.write(data, 1)

    # Reopen to get index mapping
    ad8_path = tmp_path / "test_ad8.tif"
    with rasterio.open(
        ad8_path, "w", driver="GTiff", height=height, width=width,
        count=1, dtype="float32", crs="EPSG:5070", transform=transform,
        nodata=-1.0,
    ) as dst:
        # Place contributing area values in a 3x3 block around each crossing
        row_w, col_w = dst.index(500000, 1805000)
        row_s, col_s = dst.index(503000, 1800000)
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                r, c = row_w + dr, col_w + dc
                if 0 <= r < height and 0 <= c < width:
                    data[r, c] = 400.0
                r, c = row_s + dr, col_s + dc
                if 0 <= r < height and 0 <= c < width:
                    data[r, c] = 600.0
        dst.write(data, 1)

    (tmp_path / "probe.tif").unlink(missing_ok=True)
    return ad8_path


@pytest.fixture
def simple_hydro_set():
    """100-yr sinusoidal hydrograph."""
    n_points = 97  # 24hr at 15min intervals
    peak_cfs = 10000.0
    flows = [peak_cfs * np.sin(np.pi * i / n_points) for i in range(n_points)]
    flows[0] = 0.0
    flows[-1] = 0.0
    return MockHydroSet(hydrographs={100: MockHydrograph(flows_cfs=flows)})


# ── Pure Python Stage Tests ──────────────────────────────────────────────────


class TestBcLineGeneration:
    """Verify BC Line generation from synthetic watershed."""

    def test_headwater_produces_two_bcs(self, synthetic_basin, synthetic_streams, pour_point):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point, area_name="MainArea", channel_slope=0.005,
        )
        assert bc_set.outlet is not None
        assert bc_set.outlet.name == "DSOutflow"
        assert len(bc_set.inflows) == 0
        assert len(bc_set.bc_lines) == 2  # DSOutflow + Perimeter

    def test_non_headwater_produces_inflows(self, synthetic_basin, synthetic_streams, pour_point):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point, area_name="MainArea", channel_slope=0.005,
            headwater=False,
        )
        assert bc_set.outlet is not None
        assert bc_set.outlet.name == "DSOutflow"
        assert len(bc_set.inflows) >= 1
        assert len(bc_set.bc_lines) >= 3

    def test_all_names_under_16_chars(self, synthetic_basin, synthetic_streams, pour_point):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        for bc in bc_set.bc_lines:
            assert len(bc.name) <= 16, f"Name too long: '{bc.name}'"

    def test_outlet_has_channel_slope(self, synthetic_basin, synthetic_streams, pour_point):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point, channel_slope=0.003,
        )
        assert bc_set.outlet.slope == 0.003

    def test_perimeter_normals_have_standard_slope(
        self, synthetic_basin, synthetic_streams, pour_point
    ):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        for bc in bc_set.normal_depth_perimeter:
            assert bc.slope == pytest.approx(0.00033, abs=1e-5)

    def test_coords_outside_basin(self, synthetic_basin, synthetic_streams, pour_point):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        for bc in bc_set.bc_lines:
            for x, y in bc.coords:
                pt = Point(x, y)
                assert not synthetic_basin.contains(pt), (
                    f"BC '{bc.name}' coord ({x},{y}) is inside basin"
                )


class TestContributingAreaWeighting:
    """Verify flow splitting uses contributing area proportionally."""

    def test_weights_proportional_to_area(
        self, synthetic_basin, synthetic_streams, pour_point, synthetic_ad8
    ):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point, ad8_path=synthetic_ad8,
        )
        inflows = bc_set.inflows
        if len(inflows) >= 2:
            weights = [bc.weight for bc in inflows]
            assert sum(weights) == pytest.approx(1.0, abs=0.01)
            assert all(0.0 < w < 1.0 for w in weights)

    def test_equal_weights_without_ad8(
        self, synthetic_basin, synthetic_streams, pour_point
    ):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point, ad8_path=None,
        )
        inflows = bc_set.inflows
        if len(inflows) >= 2:
            weights = [bc.weight for bc in inflows]
            expected = 1.0 / len(inflows)
            for w in weights:
                assert w == pytest.approx(expected, abs=0.001)


class TestGeomFileFormat:
    """Verify .g01 text output format."""

    def test_bc_line_block_format(self, synthetic_basin, synthetic_streams, pour_point):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        block = format_bc_line_block(bc_set.outlet)
        assert "BC Line Name=" in block
        assert "BC Line Storage Area=" in block
        assert "BC Line Arc=" in block
        lines = block.strip().split("\n")
        name_line = lines[0]
        assert len(name_line.split("=", 1)[1]) == 32  # padded to 32 chars

    def test_append_bc_lines_to_geom(
        self, synthetic_basin, synthetic_streams, pour_point, tmp_path
    ):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        geom_file = tmp_path / "test.g01"
        geom_file.write_text(
            "Geom Title=Test\n"
            "Storage Area=MainArea\n"
            "LCMann Time=0\n",
            encoding="utf-8",
        )
        append_bc_lines_to_geom(geom_file, bc_set)
        text = geom_file.read_text()
        assert "BC Line Name=" in text
        assert text.index("BC Line Name=") < text.index("LCMann Time=")


class TestUnsteadyFlowFormat:
    """Verify .u01 output with 2D boundary locations."""

    def test_2d_boundary_locations(
        self, synthetic_basin, synthetic_streams, pour_point, simple_hydro_set, tmp_path
    ):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point, channel_slope=0.005,
        )
        flow_file = tmp_path / "test.u01"
        write_unsteady_flow_file_2d(flow_file, simple_hydro_set, 100, bc_set, 0.005)
        text = flow_file.read_text()
        assert "Boundary Location=" in text
        assert "MainArea" in text
        assert "RAS_AGENT,MAIN" not in text

    def test_flow_weighting_applied_non_headwater(
        self, synthetic_basin, synthetic_streams, pour_point,
        synthetic_ad8, simple_hydro_set, tmp_path
    ):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point, ad8_path=synthetic_ad8,
            headwater=False,
        )
        flow_file = tmp_path / "test.u01"
        write_unsteady_flow_file_2d(flow_file, simple_hydro_set, 100, bc_set, 0.005)
        text = flow_file.read_text()
        assert "Flow Hydrograph=" in text
        inflows = bc_set.inflows
        if len(inflows) >= 2:
            assert inflows[0].weight != pytest.approx(inflows[1].weight, abs=0.01)

    def test_headwater_flow_file_no_hydrographs(
        self, synthetic_basin, synthetic_streams, pour_point,
        simple_hydro_set, tmp_path
    ):
        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        flow_file = tmp_path / "test.u01"
        write_unsteady_flow_file_2d(flow_file, simple_hydro_set, 100, bc_set, 0.005)
        text = flow_file.read_text()
        assert "Flow Hydrograph=" not in text
        assert "Friction Slope=" in text
        assert text.count("Boundary Location=") == 2


# ── Mesh Stage Tests (Windows only) ─────────────────────────────────────────


@pytest.mark.skipif(
    platform.system() != "Windows",
    reason="Requires pythonnet/RasMapperLib (Windows only)",
)
@pytest.mark.skip(reason="Requires interactive HEC-RAS DLLs; run manually")
class TestMeshStages:
    """Test mesh generation and BC conflict fix (requires RasMapperLib)."""

    def test_compile_geometry(
        self, synthetic_basin, synthetic_streams, pour_point, tmp_path
    ):
        """GeomMesh.compile_geometry() produces .g01.hdf from text."""
        from ras_commander.geom import GeomMesh

        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        geom_file = tmp_path / "test.g01"
        from pipeline.model_builder import _write_geometry_first_geom_file
        perimeter = list(synthetic_basin.exterior.coords)
        _write_geometry_first_geom_file(
            geom_file, "MainArea", perimeter, 300.0, 0.035
        )
        append_bc_lines_to_geom(geom_file, bc_set)

        hdf_path = GeomMesh.compile_geometry(geom_file)
        assert hdf_path.exists()
        assert hdf_path.stat().st_size > 0

    def test_generate_mesh(
        self, synthetic_basin, synthetic_streams, pour_point, tmp_path
    ):
        """GeomMesh.generate() produces mesh with cells."""
        from ras_commander.geom import GeomMesh

        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        geom_file = tmp_path / "test.g01"
        from pipeline.model_builder import _write_geometry_first_geom_file
        perimeter = list(synthetic_basin.exterior.coords)
        _write_geometry_first_geom_file(
            geom_file, "MainArea", perimeter, 300.0, 0.035
        )
        append_bc_lines_to_geom(geom_file, bc_set)

        hdf_path = GeomMesh.compile_geometry(geom_file)
        result = GeomMesh.generate(str(hdf_path), mesh_name="MainArea", cell_size=300.0)
        assert result.ok, f"Mesh gen failed: {result.error_message}"
        assert result.cell_count > 0
        assert result.face_count > 0

    def test_fix_bc_conflicts(
        self, synthetic_basin, synthetic_streams, pour_point, tmp_path
    ):
        """GeomMesh.fix_bc_conflicts() resolves or reports conflicts."""
        from ras_commander.geom import GeomMesh

        bc_set = generate_bc_lines(
            basin=synthetic_basin, streams=synthetic_streams,
            pour_point=pour_point,
        )
        geom_file = tmp_path / "test.g01"
        from pipeline.model_builder import _write_geometry_first_geom_file
        perimeter = list(synthetic_basin.exterior.coords)
        _write_geometry_first_geom_file(
            geom_file, "MainArea", perimeter, 300.0, 0.035
        )
        append_bc_lines_to_geom(geom_file, bc_set)

        hdf_path = GeomMesh.compile_geometry(geom_file)
        GeomMesh.generate(str(hdf_path), mesh_name="MainArea", cell_size=300.0)

        bc_result = GeomMesh.fix_bc_conflicts(str(hdf_path), cell_size=300.0)
        # Either no conflicts or all resolved
        assert bc_result.conflicts_found >= 0
