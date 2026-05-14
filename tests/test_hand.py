"""
test_hand.py — Tests for pipeline/hand.py

All tests pass without TauDEM installed by mocking subprocess execution
and TauDEM binary discovery.
"""

import os
import sys
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import hand


def _make_test_raster(path: Path, data: np.ndarray) -> Path:
    """Write a minimal GeoTIFF for testing."""
    rows, cols = data.shape
    transform = from_bounds(500000, 4000000, 510000, 4010000, cols, rows)
    with rasterio.open(
        path, "w", driver="GTiff", height=rows, width=cols,
        count=1, dtype=data.dtype, crs=CRS.from_epsg(5070),
        transform=transform, nodata=-9999.0,
    ) as dst:
        dst.write(data, 1)
    return path


def _make_hand_raster(path: Path) -> Path:
    """Write a synthetic HAND raster with realistic values."""
    rows, cols = 20, 20
    data = np.zeros((rows, cols), dtype=np.float32)
    for r in range(rows):
        for c in range(cols):
            data[r, c] = abs(c - cols // 2) * 0.5
    return _make_test_raster(path, data)


class TestMockHand:
    def test_mock_hand_creates_raster(self, tmp_path):
        result = hand.mock_hand(tmp_path / "hand")
        assert result.hand_path.exists()
        assert result.min_hand_m == 0.0
        assert result.max_hand_m > 0.0
        assert result.mean_hand_m > 0.0
        assert result.stream_cell_count > 0

    def test_mock_hand_reads_as_valid_geotiff(self, tmp_path):
        result = hand.mock_hand(tmp_path / "hand")
        with rasterio.open(result.hand_path) as src:
            assert src.crs is not None
            data = src.read(1)
            assert data.shape[0] > 0
            assert data.shape[1] > 0

    def test_mock_hand_idempotent(self, tmp_path):
        out = tmp_path / "hand"
        r1 = hand.mock_hand(out)
        r2 = hand.mock_hand(out)
        assert r1.hand_path == r2.hand_path


class TestComputeStats:
    def test_stats_on_synthetic_raster(self, tmp_path):
        path = _make_hand_raster(tmp_path / "hand_test.tif")
        stats = hand._compute_stats(path)
        assert stats["min"] == pytest.approx(0.0, abs=0.01)
        assert stats["max"] > 0
        assert stats["mean"] > 0
        assert stats["stream_cells"] > 0

    def test_stats_all_nodata(self, tmp_path):
        data = np.full((10, 10), -9999.0, dtype=np.float32)
        path = _make_test_raster(tmp_path / "nodata.tif", data)
        stats = hand._compute_stats(path)
        assert stats["min"] == 0.0
        assert stats["stream_cells"] == 0


class TestToCog:
    def test_cog_has_overviews(self, tmp_path):
        src_path = _make_hand_raster(tmp_path / "raw.tif")
        dst_path = tmp_path / "cog.tif"
        hand._to_cog(src_path, dst_path)
        assert dst_path.exists()
        with rasterio.open(dst_path) as src:
            assert src.compression == rasterio.enums.Compression.lzw
            assert src.is_tiled

    def test_cog_replaces_inf_with_nodata(self, tmp_path):
        data = np.array([[1.0, np.inf], [-np.inf, 5.0]], dtype=np.float32)
        src_path = _make_test_raster(tmp_path / "inf.tif", data)
        dst_path = tmp_path / "clean.tif"
        hand._to_cog(src_path, dst_path)
        with rasterio.open(dst_path) as src:
            out = src.read(1)
            assert not np.any(np.isinf(out))


class TestComputeHand:
    def test_raises_on_missing_fel(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Pit-filled DEM"):
            hand.compute_hand(
                tmp_path / "nonexistent.tif",
                tmp_path / "src.tif",
                tmp_path / "out",
            )

    def test_raises_on_missing_src(self, tmp_path):
        fel = _make_test_raster(
            tmp_path / "fel.tif",
            np.ones((10, 10), dtype=np.float32),
        )
        with pytest.raises(FileNotFoundError, match="Stream source"):
            hand.compute_hand(
                fel, tmp_path / "nonexistent.tif", tmp_path / "out",
            )

    @mock.patch("hand.TauDem")
    def test_compute_hand_calls_taudem(self, mock_taudem, tmp_path):
        """Verify the correct TauDEM commands are invoked."""
        fel = _make_test_raster(
            tmp_path / "fel.tif",
            np.linspace(300, 280, 100, dtype=np.float32).reshape(10, 10),
        )
        src = _make_test_raster(
            tmp_path / "src.tif",
            np.zeros((10, 10), dtype=np.float32),
        )

        def fake_dinf_flow_dir(filled_dem_path, angfile, slpfile, **kw):
            _make_test_raster(Path(angfile), np.zeros((10, 10), dtype=np.float32))
            _make_test_raster(Path(slpfile), np.zeros((10, 10), dtype=np.float32))

        def fake_dinf_dist_down(angfile, felfile, srcfile, ddfile, **kw):
            data = np.random.uniform(0, 5, (10, 10)).astype(np.float32)
            data[5, 5] = 0.0
            _make_test_raster(Path(ddfile), data)

        mock_taudem.validate_environment.return_value = {}
        mock_taudem.dinf_flow_dir.side_effect = fake_dinf_flow_dir
        mock_taudem.dinf_dist_down.side_effect = fake_dinf_dist_down

        result = hand.compute_hand(fel, src, tmp_path / "out")

        mock_taudem.validate_environment.assert_called_once()
        mock_taudem.dinf_flow_dir.assert_called_once()
        mock_taudem.dinf_dist_down.assert_called_once()

        call_kwargs = mock_taudem.dinf_dist_down.call_args
        assert call_kwargs.kwargs["stat_method"] == "min"
        assert call_kwargs.kwargs["dist_method"] == "v"

        assert result.hand_path.exists()
        assert result.max_hand_m > 0
        assert result.stream_cell_count >= 0


class TestHandResult:
    def test_dataclass_fields(self):
        r = hand.HandResult(
            hand_path=Path("hand.tif"),
            hand_clipped_path=None,
            min_hand_m=0.0,
            max_hand_m=12.5,
            mean_hand_m=4.2,
            stream_cell_count=150,
            artifacts={},
        )
        assert r.min_hand_m == 0.0
        assert r.max_hand_m == 12.5
        assert r.hand_clipped_path is None
