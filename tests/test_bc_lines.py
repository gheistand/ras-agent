"""Tests for pipeline.bc_lines — BC Line generation and formatting."""

import pytest
from shapely.geometry import LineString, Point, Polygon

from pipeline.bc_lines import (
    BCLineSet,
    BCLineSpec,
    _classify_outlet,
    _extract_boundary_subarc,
    _find_stream_boundary_intersections,
    _fill_normal_depth_gaps,
    _format_bc_arc_coords,
    format_2d_boundary_location,
    format_bc_line_block,
    generate_bc_lines,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def box_basin():
    """Simple box watershed 1000m × 1000m centered at origin."""
    return Polygon([
        (0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)
    ])


@pytest.fixture
def straight_stream():
    """Stream crossing box basin from left to right through center."""
    return LineString([(-100, 500), (1100, 500)])


@pytest.fixture
def two_streams():
    """Two streams: one entering left, one entering bottom."""
    return [
        LineString([(-100, 500), (500, 500)]),   # enters left side
        LineString([(500, -100), (500, 500)]),    # enters bottom
    ]


@pytest.fixture
def pour_point_right():
    """Pour point on right side of box (outlet)."""
    return Point(1000, 500)


# ── Format Tests ──────────────────────────────────────────────────────────────


class TestFormatBcArcCoords:
    def test_two_points_single_line(self):
        coords = [(1000.5, 2000.75), (3000.25, 4000.125)]
        result = _format_bc_arc_coords(coords)
        # 4 values on one line, each 16 chars
        assert len(result.splitlines()) == 1
        fields = [result[i:i+16] for i in range(0, len(result), 16)]
        assert len(fields) == 4
        assert float(fields[0]) == 1000.5
        assert float(fields[1]) == 2000.75
        assert float(fields[2]) == 3000.25
        assert float(fields[3]) == 4000.125

    def test_four_points_two_lines(self):
        coords = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0), (7.0, 8.0)]
        result = _format_bc_arc_coords(coords)
        lines = result.splitlines()
        assert len(lines) == 2
        # Each line has 4 values × 16 chars = 64 chars
        assert len(lines[0]) == 64
        assert len(lines[1]) == 64

    def test_three_points_wraps(self):
        coords = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]
        result = _format_bc_arc_coords(coords)
        lines = result.splitlines()
        # 6 values: 4 on line 1, 2 on line 2
        assert len(lines) == 2
        assert len(lines[0]) == 64
        assert len(lines[1]) == 32

    def test_values_right_justified(self):
        coords = [(1.5, 2.5)]
        result = _format_bc_arc_coords(coords)
        # Two 16-char fields
        assert len(result) == 32
        assert result[:16].strip() == "1.5"
        assert result[16:32].strip() == "2.5"


class TestFormatBcLineBlock:
    def test_basic_structure(self):
        spec = BCLineSpec(
            name="DSOutflow",
            storage_area="MainArea",
            coords=[(100.0, 200.0), (150.0, 250.0), (200.0, 300.0)],
            bc_type="normal_depth",
            slope=0.005,
        )
        block = format_bc_line_block(spec)
        lines = block.splitlines()

        assert lines[0].startswith("BC Line Name=")
        assert lines[1].startswith("BC Line Storage Area=")
        assert lines[2].startswith("BC Line Start Position=")
        assert lines[3].startswith("BC Line Middle Position=")
        assert lines[4].startswith("BC Line End Position=")
        assert lines[5].startswith("BC Line Arc= 3")

    def test_name_padded_to_32(self):
        spec = BCLineSpec(
            name="DSOutflow",
            storage_area="MainArea",
            coords=[(0, 0), (1, 1)],
            bc_type="normal_depth",
        )
        block = format_bc_line_block(spec)
        name_line = block.splitlines()[0]
        name_value = name_line.split("=", 1)[1]
        assert len(name_value) == 32

    def test_area_padded_to_16(self):
        spec = BCLineSpec(
            name="Test",
            storage_area="MainArea",
            coords=[(0, 0), (1, 1)],
            bc_type="normal_depth",
        )
        block = format_bc_line_block(spec)
        area_line = block.splitlines()[1]
        area_value = area_line.split("=", 1)[1]
        assert len(area_value) == 16

    def test_arc_count_matches_coords(self):
        coords = [(i, i * 2) for i in range(5)]
        spec = BCLineSpec(
            name="Test", storage_area="Area", coords=coords, bc_type="normal_depth"
        )
        block = format_bc_line_block(spec)
        arc_line = [l for l in block.splitlines() if l.startswith("BC Line Arc=")][0]
        assert "5" in arc_line

    def test_start_end_middle_positions(self):
        coords = [(10, 20), (30, 40), (50, 60), (70, 80), (90, 100)]
        spec = BCLineSpec(
            name="X", storage_area="A", coords=coords, bc_type="normal_depth"
        )
        block = format_bc_line_block(spec)
        lines = block.splitlines()
        assert "10" in lines[2] and "20" in lines[2]    # start
        assert "50" in lines[3] and "60" in lines[3]    # middle (index 2)
        assert "90" in lines[4] and "100" in lines[4]   # end


class TestFormat2dBoundaryLocation:
    def test_normal_depth_format(self):
        result = format_2d_boundary_location(
            "BaldEagleCr", "DSNormalDepth", "normal_depth", slope=0.0003
        )
        assert "Boundary Location=" in result
        assert "BaldEagleCr" in result
        assert "DSNormalDepth" in result
        assert "Friction Slope=0.0003,0" in result

    def test_flow_hydrograph_format(self):
        result = format_2d_boundary_location(
            "MainArea", "USInflow1", "flow_hydrograph",
            flow_count=97, interval="15MIN"
        )
        assert "Boundary Location=" in result
        assert "MainArea" in result
        assert "USInflow1" in result
        assert "Interval=15MIN" in result
        assert "Flow Hydrograph= 97" in result

    def test_field_widths(self):
        result = format_2d_boundary_location(
            "MainArea", "DSOutflow", "normal_depth", slope=0.001
        )
        header = result.splitlines()[0]
        # After "Boundary Location="
        fields_str = header.split("Boundary Location=")[1]
        fields = fields_str.split(",")
        assert len(fields) == 9
        # Fields 1-2: 16 chars each
        assert len(fields[0]) == 16
        assert len(fields[1]) == 16
        # Fields 3-4: 8 chars each
        assert len(fields[2]) == 8
        assert len(fields[3]) == 8
        # Field 5: 16 chars
        assert len(fields[4]) == 16
        # Field 6: area name 16 chars
        assert len(fields[5]) == 16
        # Field 7: 16 chars
        assert len(fields[6]) == 16
        # Field 8: BC name 32 chars
        assert len(fields[7]) == 32
        # Field 9: trailing 32-char blank (required v6.x+)
        assert len(fields[8]) == 32

    def test_area_name_in_field_6(self):
        result = format_2d_boundary_location(
            "TestArea", "TestBC", "normal_depth", slope=0.001
        )
        header = result.splitlines()[0]
        fields = header.split("Boundary Location=")[1].split(",")
        assert fields[5].strip() == "TestArea"

    def test_bc_name_in_field_8(self):
        result = format_2d_boundary_location(
            "Area", "MyBCLine", "normal_depth", slope=0.001
        )
        header = result.splitlines()[0]
        fields = header.split("Boundary Location=")[1].split(",")
        assert fields[7].strip() == "MyBCLine"


# ── Algorithm Tests ───────────────────────────────────────────────────────────


class TestFindStreamBoundaryIntersections:
    def test_straight_stream_two_intersections(self, box_basin, straight_stream):
        intersections = _find_stream_boundary_intersections([straight_stream], box_basin)
        assert len(intersections) == 2

    def test_two_streams_multiple_intersections(self, box_basin, two_streams):
        intersections = _find_stream_boundary_intersections(two_streams, box_basin)
        # Each stream enters once (starts outside, ends inside) = 1 intersection each
        # But they also exit... actually they end at (500,500) which is inside
        # So left stream: enters at (0, 500) = 1 intersection
        # Bottom stream: enters at (500, 0) = 1 intersection
        assert len(intersections) >= 2

    def test_sorted_by_boundary_position(self, box_basin, two_streams):
        intersections = _find_stream_boundary_intersections(two_streams, box_basin)
        ts = [i["t"] for i in intersections]
        assert ts == sorted(ts)

    def test_empty_streams_no_intersections(self, box_basin):
        intersections = _find_stream_boundary_intersections([], box_basin)
        assert len(intersections) == 0

    def test_stream_not_touching_boundary(self):
        big_box = Polygon([(0, 0), (10000, 0), (10000, 10000), (0, 10000)])
        interior_stream = LineString([(4000, 4000), (6000, 6000)])
        intersections = _find_stream_boundary_intersections([interior_stream], big_box)
        assert len(intersections) == 0


class TestClassifyOutlet:
    def test_closest_to_pour_point_is_outlet(self):
        intersections = [
            {"point": Point(0, 500), "t": 0.25, "stream_index": 0},
            {"point": Point(1000, 500), "t": 0.75, "stream_index": 0},
        ]
        pour_point = Point(1000, 500)
        outlet, inflows = _classify_outlet(intersections, pour_point)
        assert outlet is not None
        assert outlet["t"] == 0.75
        assert len(inflows) == 1

    def test_no_outlet_if_too_far(self):
        intersections = [
            {"point": Point(0, 0), "t": 0.0, "stream_index": 0},
        ]
        pour_point = Point(5000, 5000)  # Very far away
        outlet, inflows = _classify_outlet(intersections, pour_point, max_dist_m=100)
        assert outlet is None
        assert len(inflows) == 1

    def test_empty_intersections(self):
        outlet, inflows = _classify_outlet([], Point(0, 0))
        assert outlet is None
        assert len(inflows) == 0


class TestGenerateBcLines:
    def test_basic_generation(self, box_basin, pour_point_right):
        stream = LineString([(-100, 500), (1100, 500)])
        bc_set = generate_bc_lines(
            basin=box_basin,
            streams=[stream],
            pour_point=pour_point_right,
            dem_path=None,
            area_name="MainArea",
            channel_slope=0.005,
        )
        assert isinstance(bc_set, BCLineSet)
        assert len(bc_set.bc_lines) >= 2  # At least outlet + inflow or normal depth

    def test_outlet_exists(self, box_basin, pour_point_right):
        stream = LineString([(-100, 500), (1100, 500)])
        bc_set = generate_bc_lines(
            basin=box_basin,
            streams=[stream],
            pour_point=pour_point_right,
            dem_path=None,
        )
        assert bc_set.outlet is not None
        assert bc_set.outlet.name == "DSOutflow"
        assert bc_set.outlet.bc_type == "normal_depth"

    def test_inflows_named_correctly(self, box_basin):
        # Two streams entering from left and bottom, pour point on right
        streams = [
            LineString([(-100, 500), (1100, 500)]),   # crosses left and right
        ]
        pour_point = Point(1000, 500)
        bc_set = generate_bc_lines(
            basin=box_basin, streams=streams, pour_point=pour_point, dem_path=None,
        )
        for inflow in bc_set.inflows:
            assert inflow.name.startswith("USInflow")
            assert inflow.bc_type == "flow_hydrograph"

    def test_non_headwater_inflows_keep_stream_index(self, box_basin, pour_point_right):
        stream = LineString([(-100, 500), (1100, 500)])
        bc_set = generate_bc_lines(
            basin=box_basin,
            streams=[stream],
            pour_point=pour_point_right,
            dem_path=None,
            headwater=False,
        )
        assert len(bc_set.inflows) == 1
        assert bc_set.inflows[0].stream_index == 0

    def test_all_names_max_16_chars(self, box_basin, pour_point_right):
        stream = LineString([(-100, 500), (1100, 500)])
        bc_set = generate_bc_lines(
            basin=box_basin, streams=[stream], pour_point=pour_point_right,
            dem_path=None,
        )
        for bc in bc_set.bc_lines:
            assert len(bc.name) <= 16, f"Name too long: {bc.name}"

    def test_fallback_no_streams(self, box_basin, pour_point_right):
        bc_set = generate_bc_lines(
            basin=box_basin, streams=[], pour_point=pour_point_right, dem_path=None,
        )
        assert len(bc_set.bc_lines) >= 2
        assert bc_set.outlet is not None

    def test_headwater_no_stream_intersections_does_not_create_inflow(
        self, box_basin, pour_point_right
    ):
        bc_set = generate_bc_lines(
            basin=box_basin,
            streams=[],
            pour_point=pour_point_right,
            dem_path=None,
            headwater=True,
        )
        assert bc_set.outlet is not None
        assert bc_set.inflows == []
        assert {bc.bc_type for bc in bc_set.bc_lines} == {"normal_depth"}

    def test_non_headwater_no_stream_intersections_keeps_inflow_fallback(
        self, box_basin, pour_point_right
    ):
        bc_set = generate_bc_lines(
            basin=box_basin,
            streams=[],
            pour_point=pour_point_right,
            dem_path=None,
            headwater=False,
        )
        assert len(bc_set.inflows) == 1
        assert bc_set.inflows[0].name == "USInflow1"

    def test_coords_outside_basin(self, box_basin, pour_point_right):
        stream = LineString([(-100, 500), (1100, 500)])
        bc_set = generate_bc_lines(
            basin=box_basin, streams=[stream], pour_point=pour_point_right,
            dem_path=None, offset_ft=500,
        )
        for bc in bc_set.bc_lines:
            line = LineString(bc.coords)
            # BC line should be outside or touching the basin, not fully inside
            assert not box_basin.contains(line), f"{bc.name} is inside basin"

    def test_perimeter_slope_is_standard_low(self, box_basin, pour_point_right):
        stream = LineString([(-100, 500), (1100, 500)])
        bc_set = generate_bc_lines(
            basin=box_basin, streams=[stream], pour_point=pour_point_right,
            dem_path=None, perimeter_slope=0.00033,
        )
        for bc in bc_set.normal_depth_perimeter:
            assert bc.slope == 0.00033


class TestBcLineSet:
    def test_outlet_property(self):
        bc_set = BCLineSet(bc_lines=[
            BCLineSpec(name="DSOutflow", storage_area="A", coords=[(0,0),(1,1)],
                      bc_type="normal_depth", slope=0.005),
            BCLineSpec(name="USInflow1", storage_area="A", coords=[(0,0),(1,1)],
                      bc_type="flow_hydrograph"),
        ])
        assert bc_set.outlet.name == "DSOutflow"

    def test_inflows_property(self):
        bc_set = BCLineSet(bc_lines=[
            BCLineSpec(name="USInflow1", storage_area="A", coords=[(0,0),(1,1)],
                      bc_type="flow_hydrograph"),
            BCLineSpec(name="USInflow2", storage_area="A", coords=[(0,0),(1,1)],
                      bc_type="flow_hydrograph"),
            BCLineSpec(name="DSOutflow", storage_area="A", coords=[(0,0),(1,1)],
                      bc_type="normal_depth"),
        ])
        assert len(bc_set.inflows) == 2

    def test_normal_depth_perimeter_property(self):
        bc_set = BCLineSet(bc_lines=[
            BCLineSpec(name="NormDepth1", storage_area="A", coords=[(0,0),(1,1)],
                      bc_type="normal_depth", slope=0.00033),
            BCLineSpec(name="NormDepth2", storage_area="A", coords=[(0,0),(1,1)],
                      bc_type="normal_depth", slope=0.00033),
        ])
        assert len(bc_set.normal_depth_perimeter) == 2
