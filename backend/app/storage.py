"""Cloud storage for finished clips (Blomp, over its S3-compatible API).

A stored file is referred to as "s3:<key>"; anything else is a path relative to
the data folder (a clip that was never uploaded, or an upload that failed).
The access keys live only in backend/.env. Playback and download use
short-lived signed links, so the bucket itself stays private.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from . import config, db

PREFIX = "s3:"
LINK_SECONDS = 6 * 3600
_client_cache: dict[tuple, Any] = {}
_lock = threading.Lock()


class StorageError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


def cfg() -> dict[str, Any]:
    out = dict(db.get_settings()["storage"])
    out["bucket"] = (out.get("bucket") or config.secret("BLOMP_BUCKET")).strip()
    return out


def keys() -> tuple[str, str]:
    return config.secret("BLOMP_ACCESS_KEY_ID"), config.secret("BLOMP_SECRET_ACCESS_KEY")


def enabled() -> bool:
    c = cfg()
    return bool(c["enabled"] and c["bucket"] and all(keys()))


def is_remote(ref: str | None) -> bool:
    return bool(ref) and ref.startswith(PREFIX)


def _client():
    import boto3
    from botocore.config import Config

    c = cfg()
    access, secret = keys()
    if not (access and secret):
        raise StorageError("No storage keys are set. Add them to backend/.env.")
    ident = (c["endpoint"], c["region"], access, secret)
    with _lock:
        if ident not in _client_cache:
            _client_cache.clear()
            _client_cache[ident] = boto3.client(
                "s3", endpoint_url=c["endpoint"], region_name=c["region"],
                aws_access_key_id=access, aws_secret_access_key=secret,
                config=Config(s3={"addressing_style": "path"}, signature_version="s3v4",
                              retries={"max_attempts": 5, "mode": "standard"},
                              connect_timeout=15, read_timeout=120),
            )
        return _client_cache[ident]


def _explain(exc: Exception) -> str:
    text = str(exc)
    for needle, message in (
        ("InvalidAccessKeyId", "The storage service does not recognise the access key ID."),
        ("SignatureDoesNotMatch", "The secret access key does not match the key ID."),
        ("AccessDenied", "The storage service denied access. Check the bucket name and the keys."),
        ("NoSuchBucket", "That bucket does not exist. Check BLOMP_BUCKET in backend/.env."),
    ):
        if needle in text:
            return message
    if "Could not connect" in text or "timed out" in text.lower() or "Name or service" in text:
        return "Could not reach the storage service. Check the internet connection."
    return f"Storage error: {text[:200]}"


def key_for(job_id: str, name: str) -> str:
    prefix = cfg().get("prefix", "clipper").strip("/")
    return f"{prefix}/{job_id}/{name}" if prefix else f"{job_id}/{name}"


def put(path: Path, key: str, content_type: str) -> str:
    """Upload a file and return its reference."""
    from boto3.s3.transfer import TransferConfig

    try:
        _client().upload_file(str(path), cfg()["bucket"], key,
                              ExtraArgs={"ContentType": content_type},
                              Config=TransferConfig(multipart_threshold=64 * 1024 * 1024,
                                                    multipart_chunksize=16 * 1024 * 1024,
                                                    max_concurrency=4))
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(_explain(exc)) from None
    return PREFIX + key


def url(ref: str, *, download_name: str | None = None) -> str:
    params: dict[str, Any] = {"Bucket": cfg()["bucket"], "Key": ref.removeprefix(PREFIX)}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=LINK_SECONDS)


def delete(ref: str | None) -> None:
    """Best effort: a failed delete must never block deleting the clip itself."""
    if not is_remote(ref):
        return
    with contextlib.suppress(Exception):
        _client().delete_object(Bucket=cfg()["bucket"], Key=ref.removeprefix(PREFIX))


@contextlib.contextmanager
def local_copy(ref: str) -> Iterator[Path]:
    """A local path to the file: the stored file itself, or a temporary download."""
    if not is_remote(ref):
        yield config.DATA / ref
        return
    tmp_dir = config.DATA / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(suffix=".mp4", dir=tmp_dir)
    os.close(handle)
    path = Path(name)
    try:
        try:
            _client().download_file(cfg()["bucket"], ref.removeprefix(PREFIX), str(path))
        except Exception as exc:
            raise StorageError(_explain(exc)) from None
        yield path
    finally:
        path.unlink(missing_ok=True)


def check() -> dict[str, Any]:
    """Round trip: write a small object, read it through a signed link, delete it."""
    import httpx

    c = cfg()
    if not c["bucket"]:
        raise StorageError("Enter the bucket name first.")
    started = time.time()
    key = f"{c.get('prefix', 'clipper').strip('/')}/_connection-test.txt"
    try:
        client = _client()
        client.put_object(Bucket=c["bucket"], Key=key, Body=b"clipper connection test",
                          ContentType="text/plain")
        got = httpx.get(client.generate_presigned_url(
            "get_object", Params={"Bucket": c["bucket"], "Key": key}, ExpiresIn=300), timeout=30)
        client.delete_object(Bucket=c["bucket"], Key=key)
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(_explain(exc)) from None
    if got.status_code != 200:
        raise StorageError(f"The file was saved but could not be read back (HTTP {got.status_code}).")
    return {"ok": True, "bucket": c["bucket"], "seconds": round(time.time() - started, 1)}


def local_clip_count() -> int:
    rows = db.query("SELECT COUNT(*) AS n FROM clips WHERE status = 'ready' AND file IS NOT NULL "
                    "AND file NOT LIKE 's3:%'")
    return rows[0]["n"]


def migrate_local_clips(progress=None) -> dict[str, int]:
    """Upload clips that are still on this disk, then remove the local copies and
    the downloaded sources of finished videos."""
    rows = db.query("SELECT id, job_id, file, thumb FROM clips WHERE status = 'ready' AND "
                    "file IS NOT NULL AND file NOT LIKE 's3:%'")
    moved = 0
    for index, row in enumerate(rows):
        video = config.DATA / row["file"]
        thumb = config.DATA / row["thumb"] if row["thumb"] and not is_remote(row["thumb"]) else None
        if not video.exists():
            continue
        file_ref = put(video, key_for(row["job_id"], f"clips/{video.name}"), "video/mp4")
        thumb_ref = row["thumb"]
        if thumb and thumb.exists():
            thumb_ref = put(thumb, key_for(row["job_id"], f"clips/{thumb.name}"), "image/jpeg")
        db.update("clips", row["id"], file=file_ref, thumb=thumb_ref)
        video.unlink(missing_ok=True)
        if thumb:
            thumb.unlink(missing_ok=True)
        moved += 1
        if progress:
            progress((index + 1) / len(rows))
    freed = 0
    if cfg().get("delete_source"):
        for job in db.query("SELECT id FROM jobs WHERE kind = 'video' AND status = 'done'"):
            folder = config.JOBS_DIR / job["id"]
            for source in folder.glob("source.*"):
                freed += source.stat().st_size
                source.unlink(missing_ok=True)
            (folder / "download.done").unlink(missing_ok=True)
    return {"moved": moved, "freed_mb": round(freed / 1e6)}
