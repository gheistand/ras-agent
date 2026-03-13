"""
Tests for pipeline/storage.py — Cloudflare R2 storage module

All boto3 calls are mocked; no real R2 connection is made.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

from storage import (
    R2Config,
    r2_config_from_env,
    upload_file,
    upload_results_dir,
    get_presigned_url,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def r2_config():
    return R2Config(
        account_id="test-account-id",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
        bucket_name="test-bucket",
        public_url="https://pub-test.r2.dev",
        prefix="ras-agent",
    )


# ── test_r2_config_from_env_complete ──────────────────────────────────────────

def test_r2_config_from_env_complete(monkeypatch):
    """All required env vars set → returns populated R2Config."""
    monkeypatch.setenv("R2_ACCOUNT_ID", "my-account")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "my-key-id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "my-secret")
    monkeypatch.setenv("R2_BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("R2_PUBLIC_URL", "https://pub-abc.r2.dev")
    monkeypatch.setenv("R2_PREFIX", "ras-agent/")

    config = r2_config_from_env()

    assert config is not None
    assert config.account_id == "my-account"
    assert config.access_key_id == "my-key-id"
    assert config.secret_access_key == "my-secret"
    assert config.bucket_name == "my-bucket"
    assert config.public_url == "https://pub-abc.r2.dev"
    assert config.prefix == "ras-agent/"


# ── test_r2_config_from_env_missing ──────────────────────────────────────────

def test_r2_config_from_env_missing(monkeypatch):
    """Missing R2_ACCOUNT_ID → returns None."""
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "my-key-id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "my-secret")
    monkeypatch.setenv("R2_BUCKET_NAME", "my-bucket")

    config = r2_config_from_env()
    assert config is None


# ── test_upload_file_calls_put_object ─────────────────────────────────────────

def test_upload_file_calls_put_object(tmp_path, r2_config):
    """upload_file() calls boto3 upload_file with correct bucket and key."""
    local_file = tmp_path / "depth_grid.tif"
    local_file.write_bytes(b"fake tiff data")

    mock_client = MagicMock()

    with patch("storage.get_r2_client", return_value=mock_client):
        result = upload_file(local_file, "ras-agent/run1/depth_grid.tif", r2_config)

    mock_client.upload_file.assert_called_once_with(
        Filename=str(local_file),
        Bucket="test-bucket",
        Key="ras-agent/run1/depth_grid.tif",
        ExtraArgs=None,
    )
    # Should return public URL since public_url is set
    assert result == "https://pub-test.r2.dev/ras-agent/run1/depth_grid.tif"


# ── test_upload_results_dir_uploads_tif_and_gpkg ─────────────────────────────

def test_upload_results_dir_uploads_tif_and_gpkg(tmp_path, r2_config):
    """upload_results_dir() uploads .tif and .gpkg files, returns mapping."""
    (tmp_path / "depth_grid.tif").write_bytes(b"tif data")
    (tmp_path / "flood_extent.gpkg").write_bytes(b"gpkg data")
    (tmp_path / "README.txt").write_bytes(b"should be skipped")

    captured_uploads = []

    def fake_upload_file(local_path, key, config, content_type=None):
        captured_uploads.append((local_path.name, key))
        return key

    with patch("storage.upload_file", side_effect=fake_upload_file):
        result = upload_results_dir(tmp_path, "run-001", r2_config)

    uploaded_names = {name for name, _ in captured_uploads}
    assert "depth_grid.tif" in uploaded_names
    assert "flood_extent.gpkg" in uploaded_names
    assert "README.txt" not in uploaded_names

    assert "depth_grid.tif" in result
    assert "flood_extent.gpkg" in result
    assert len(result) == 2


# ── test_upload_results_dir_skips_large_files ────────────────────────────────

def test_upload_results_dir_skips_large_files(tmp_path, r2_config):
    """Files larger than 500 MB are skipped with a warning log."""
    large_file = tmp_path / "huge.tif"
    large_file.write_bytes(b"x")

    # Patch stat to report a large file size while keeping a regular-file st_mode
    import stat as stat_module
    mock_stat = MagicMock()
    mock_stat.st_size = 600 * 1024 * 1024  # 600 MB
    mock_stat.st_mode = stat_module.S_IFREG | 0o644  # regular file

    upload_calls = []

    def fake_upload_file(local_path, key, config, content_type=None):
        upload_calls.append(local_path.name)
        return key

    with patch("storage.upload_file", side_effect=fake_upload_file):
        with patch.object(Path, "stat", return_value=mock_stat):
            result = upload_results_dir(tmp_path, "run-002", r2_config)

    assert "huge.tif" not in upload_calls
    assert len(result) == 0
