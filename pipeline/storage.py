"""
storage.py — Cloudflare R2 (S3-compatible) results storage

Provides optional upload of RAS Agent result files to Cloudflare R2.
R2 is always opt-in — all callers work without it if env vars are unset.

Usage:
    config = r2_config_from_env()   # None if env vars not set
    if config:
        upload_results_dir(output_dir, run_name, config)

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default file extensions to upload
_DEFAULT_EXTENSIONS = [".tif", ".gpkg", ".shp", ".shx", ".dbf", ".prj", ".html"]

# Skip files larger than this
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class R2Config:
    """Cloudflare R2 bucket configuration."""
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_url: Optional[str] = None   # e.g. "https://pub-xxx.r2.dev"
    prefix: str = ""                    # e.g. "ras-agent/"


# ── Client ────────────────────────────────────────────────────────────────────

def get_r2_client(config: R2Config):
    """
    Create a boto3 S3 client pointed at Cloudflare R2.

    Args:
        config: R2Config with credentials and account_id.

    Returns:
        boto3 S3 client configured for R2.

    Raises:
        ImportError: If boto3 is not installed.
    """
    try:
        import boto3
    except ImportError:
        raise ImportError(
            "boto3 is required for R2 storage. Install it with:\n"
            "    pip install boto3>=1.34"
        )

    endpoint = f"https://{config.account_id}.r2.cloudflarestorage.com"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
    )
    return client


# ── Upload helpers ────────────────────────────────────────────────────────────

def upload_file(
    local_path: Path,
    key: str,
    config: R2Config,
    content_type: str = None,
) -> str:
    """
    Upload a single file to R2.

    Args:
        local_path:   Local file to upload.
        key:          Destination key in the R2 bucket.
        config:       R2Config with credentials.
        content_type: Optional MIME type (e.g. "image/tiff").

    Returns:
        Public URL if config.public_url is set, otherwise the R2 key.
    """
    local_path = Path(local_path)
    file_size = local_path.stat().st_size
    size_mb = file_size / (1024 * 1024)

    client = get_r2_client(config)

    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type

    logger.info(f"Uploading {local_path.name} ({size_mb:.1f} MB) → R2:{key}")
    t0 = time.monotonic()

    client.upload_file(
        Filename=str(local_path),
        Bucket=config.bucket_name,
        Key=key,
        ExtraArgs=extra_args if extra_args else None,
    )

    elapsed = time.monotonic() - t0
    logger.info(f"Uploaded {local_path.name} in {elapsed:.1f}s")

    if config.public_url:
        base = config.public_url.rstrip("/")
        return f"{base}/{key}"
    return key


def upload_results_dir(
    results_dir: Path,
    run_name: str,
    config: R2Config,
    extensions: list = None,
) -> dict:
    """
    Upload all result files from a directory to R2.

    Args:
        results_dir: Local directory containing result files.
        run_name:    Name used as the R2 path component (e.g. the job directory name).
        config:      R2Config with credentials.
        extensions:  File extensions to upload (default: .tif, .gpkg, .shp, etc.).

    Returns:
        Dict mapping local filename to R2 URL or key.
    """
    results_dir = Path(results_dir)
    if extensions is None:
        extensions = _DEFAULT_EXTENSIONS

    ext_set = {e.lower() for e in extensions}
    prefix = config.prefix.rstrip("/")
    uploaded: dict[str, str] = {}

    for local_file in sorted(results_dir.iterdir()):
        if not local_file.is_file():
            continue
        if local_file.suffix.lower() not in ext_set:
            continue

        file_size = local_file.stat().st_size
        if file_size > _MAX_UPLOAD_BYTES:
            logger.warning(
                f"Skipping {local_file.name} ({file_size / (1024**2):.0f} MB) — "
                f"exceeds 500 MB limit"
            )
            continue

        rel = local_file.relative_to(results_dir)
        if prefix:
            key = f"{prefix}/{run_name}/{rel}"
        else:
            key = f"{run_name}/{rel}"

        try:
            url_or_key = upload_file(local_file, key, config)
            uploaded[local_file.name] = url_or_key
        except Exception as exc:
            logger.error(f"Failed to upload {local_file.name}: {exc}")

    logger.info(f"upload_results_dir: {len(uploaded)} file(s) uploaded from {results_dir}")
    return uploaded


# ── Presigned URL ─────────────────────────────────────────────────────────────

def get_presigned_url(key: str, config: R2Config, expires_sec: int = 3600) -> str:
    """
    Generate a presigned download URL for a private R2 object.

    Args:
        key:         R2 object key.
        config:      R2Config with credentials.
        expires_sec: URL expiration in seconds (default 1 hour).

    Returns:
        Presigned URL string.
    """
    client = get_r2_client(config)
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.bucket_name, "Key": key},
        ExpiresIn=expires_sec,
    )
    return url


# ── Env loader ────────────────────────────────────────────────────────────────

def r2_config_from_env() -> Optional[R2Config]:
    """
    Build R2Config from environment variables.

    Required:
        R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME

    Optional:
        R2_PUBLIC_URL  — public bucket URL for download links
        R2_PREFIX      — key prefix, e.g. "ras-agent/"

    Returns:
        R2Config if all required vars are set, otherwise None.
    """
    required = {
        "account_id": "R2_ACCOUNT_ID",
        "access_key_id": "R2_ACCESS_KEY_ID",
        "secret_access_key": "R2_SECRET_ACCESS_KEY",
        "bucket_name": "R2_BUCKET_NAME",
    }

    values = {}
    for attr, env_var in required.items():
        val = os.environ.get(env_var)
        if not val:
            logger.debug(f"R2 not configured: {env_var} is not set")
            return None
        values[attr] = val

    return R2Config(
        account_id=values["account_id"],
        access_key_id=values["access_key_id"],
        secret_access_key=values["secret_access_key"],
        bucket_name=values["bucket_name"],
        public_url=os.environ.get("R2_PUBLIC_URL"),
        prefix=os.environ.get("R2_PREFIX", ""),
    )
