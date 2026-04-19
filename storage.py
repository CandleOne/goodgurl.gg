"""
File storage abstraction.

When AWS_ACCESS_KEY_ID / S3_BUCKET are set, files are uploaded to S3-compatible
object storage (AWS S3, Cloudflare R2, Backblaze B2, etc.).
Otherwise files are saved locally to UPLOAD_FOLDER (dev / single-instance).

Public API
----------
upload_file(file_stream, safe_name, ext)          -> url string
upload_avatar(file_stream, safe_name, ext)         -> url string
"""

import os

_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "webm": "video/webm",
}


def _content_type(ext: str) -> str:
    return _CONTENT_TYPES.get(ext.lower(), "application/octet-stream")


def _s3_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("AWS_REGION", "auto"),
    )


def _s3_upload(file_stream, key: str, ext: str) -> str:
    """Upload file_stream to S3 under *key* and return the public URL."""
    bucket = os.environ["S3_BUCKET"]
    s3 = _s3_client()
    s3.upload_fileobj(
        file_stream,
        bucket,
        key,
        ExtraArgs={
            "ContentType": _content_type(ext),
            "ACL": "public-read",
        },
    )
    # Custom CDN / public URL takes priority (e.g. Cloudflare R2 custom domain)
    base = os.environ.get("S3_PUBLIC_URL", "").rstrip("/")
    if base:
        return f"{base}/{key}"
    # Fall back to path-style endpoint URL (R2, Backblaze, MinIO)
    endpoint = os.environ.get("S3_ENDPOINT_URL", "").rstrip("/")
    if endpoint:
        return f"{endpoint}/{bucket}/{key}"
    # Default AWS virtual-hosted style
    region = os.environ.get("AWS_REGION", "us-east-1")
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def upload_file(file_stream, safe_name: str, ext: str) -> str:
    """Upload a post/task media file. Returns its public URL."""
    if os.environ.get("S3_BUCKET"):
        return _s3_upload(file_stream, safe_name, ext)

    # Local fallback
    from flask import current_app, url_for
    dest = os.path.join(current_app.config["UPLOAD_FOLDER"], safe_name)
    file_stream.save(dest)
    return url_for("uploaded_file", filename=safe_name)


def upload_avatar(file_stream, safe_name: str, ext: str) -> str:
    """Upload an avatar image. Returns its public URL."""
    if os.environ.get("S3_BUCKET"):
        return _s3_upload(file_stream, f"avatars/{safe_name}", ext)

    # Local fallback — save under static/avatars/
    from flask import current_app
    avatar_dir = os.path.join(current_app.static_folder, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)
    file_stream.save(os.path.join(avatar_dir, safe_name))
    return f"/static/avatars/{safe_name}"
