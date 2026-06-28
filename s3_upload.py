"""Centralized S3 upload for the austin-brief-audio bucket.

Every upload goes through upload_to_s3(), which enforces a random token
in the object key to prevent URL guessing.

Usage:
  from s3_upload import upload_to_s3
  url = upload_to_s3("/tmp/file.mp3", prefix="2026/06/15", stem="brief", content_type="audio/mpeg")
  # → https://austin-brief-audio.s3.us-east-2.amazonaws.com/2026/06/15/brief-a8f3c2d1.mp3

  url = upload_to_s3("/tmp/newsletter.html", prefix="newsletters", stem="kevin-game-2026-06-15", content_type="text/html")
  # → https://austin-brief-audio.s3.us-east-2.amazonaws.com/newsletters/kevin-game-2026-06-15-e4b71a09.html
"""
from __future__ import annotations  # PEP 604 (str | None) on system Python 3.9

import os
import secrets
from pathlib import Path

import boto3

S3_BUCKET = "austin-brief-audio"
S3_REGION = "us-east-2"
TOKEN_BYTES = 4  # 8 hex chars


def upload_to_s3(
    local_path: str,
    *,
    prefix: str,
    stem: str,
    content_type: str,
    token: str | None = None,
) -> str:
    """Upload a file to S3 with a random token baked into the key.

    Args:
        local_path: Path to the local file.
        prefix: S3 key prefix (e.g. "2026/06/15" or "newsletters").
        stem: Human-readable base name without extension.
        content_type: MIME type for the S3 object.
        token: Override the random token (for content-hash use cases).
               If None, a cryptographic random hex token is generated.

    Returns:
        Public URL of the uploaded object.
    """
    ext = Path(local_path).suffix
    if token is None:
        token = secrets.token_hex(TOKEN_BYTES)
    s3_key = f"{prefix}/{stem}-{token}{ext}"

    s3 = boto3.client("s3", region_name=S3_REGION)
    s3.upload_file(local_path, S3_BUCKET, s3_key, ExtraArgs={"ContentType": content_type})

    url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"
    return url
