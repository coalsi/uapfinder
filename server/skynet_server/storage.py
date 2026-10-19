"""Clip storage: the server's own disk, or S3-compatible object storage.

Nodes never upload to a fixed URL. They ask ``upload-target`` and get back a
method, URL and headers:

* ``local`` (standalone): a ``PUT`` to the server's own
  ``/api/v1/clips/{id}/data`` endpoint, which the node signs like any other
  node-authenticated request (the signature covers the body's SHA-256);
* ``s3`` (any deployment, required on Vercel, whose Functions accept at most
  ~4.5 MB request bodies and have no persistent disk): a presigned ``PUT``
  straight to the bucket (AWS S3, Cloudflare R2, Backblaze B2, MinIO).

Presigning is AWS Signature Version 4 in query-string form, implemented
here with ``hmac``/``hashlib`` (tests check it against AWS's published
example) rather than pulling boto3 into the serverless bundle.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import AsyncIterator, Dict, Optional, Tuple
from urllib.parse import quote, urlsplit

from .config import S3Config, Settings
from .models import ClipRecord

UPLOAD_TTL_S = 3600
DOWNLOAD_TTL_S = 3600
CONTENT_TYPE = "video/mp4"


class StorageError(Exception):
    """An upload is missing or does not match what the node reported."""


def clip_key(clip: ClipRecord) -> str:
    return f"{clip.track_id}/{clip.request_id}.mp4"


# --- AWS Signature Version 4, query-string authentication ---------------------------


def _uri_encode(value: str, keep_slash: bool) -> str:
    """RFC 3986 encoding as SigV4 requires: only A-Z a-z 0-9 - _ . ~ are left
    as is (and '/' in object paths)."""
    return quote(value, safe="/-_.~" if keep_slash else "-_.~")


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k = hmac.new(("AWS4" + secret).encode(), datestamp.encode(), hashlib.sha256).digest()
    for part in (region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return k


def presign_url(method: str, endpoint: str, bucket: str, key: str, region: str, access_key_id: str,
                secret_access_key: str, expires_s: int, path_style: bool, now: Optional[dt.datetime] = None,
                service: str = "s3") -> str:
    """A SigV4 presigned URL for one object (only the Host header is signed
    and the payload is UNSIGNED-PAYLOAD, so any client can use it as is)."""
    parts = urlsplit(endpoint)
    host = parts.netloc
    base_path = parts.path.rstrip("/")
    if path_style:
        path = f"{base_path}/{bucket}/{key}"
    else:
        host = f"{bucket}.{host}"
        path = f"{base_path}/{key}"
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires_s)),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(f"{_uri_encode(k, False)}={_uri_encode(v, False)}" for k, v in sorted(query.items()))
    canonical_uri = _uri_encode(path, True)
    canonical_request = "\n".join(
        [method.upper(), canonical_uri, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    signature = hmac.new(_signing_key(secret_access_key, datestamp, region, service), string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    return f"{parts.scheme}://{host}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


# --- backends -------------------------------------------------------------------------


class Storage:
    kind = "none"

    def upload_target(self, clip: ClipRecord, base_url: str) -> Dict[str, object]:
        raise StorageError("clip storage is disabled (SKYNET_STORAGE=none)")

    async def verify(self, clip: ClipRecord, size: int, sha256: str) -> None:
        """Raise StorageError unless the uploaded object matches the report."""
        raise StorageError("clip storage is disabled")

    def download_url(self, clip: ClipRecord, base_url: str) -> Optional[str]:
        return None


class LocalStorage(Storage):
    """Files under ``root``; uploads arrive at the server's own data endpoint,
    which authenticates them with the node's request signature."""

    kind = "local"

    def __init__(self, root: str, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = max_bytes

    def path(self, clip: ClipRecord) -> Path:
        return self.root / clip_key(clip)

    def upload_target(self, clip: ClipRecord, base_url: str) -> Dict[str, object]:
        return {"method": "PUT", "url": f"{base_url}/api/v1/clips/{clip.request_id}/data",
                "headers": {"Content-Type": CONTENT_TYPE}}

    async def receive(self, clip: ClipRecord, chunks: AsyncIterator[bytes]) -> Tuple[int, str, Path]:
        """Stream an upload to a temporary file; returns (bytes, sha256 hex, path).

        The caller verifies the request signature (which covers the body's
        SHA-256) and then calls ``accept`` or ``discard``, so an unverified
        upload never replaces a stored clip. File I/O runs in a thread to
        keep the event loop (and the UDP back-signal path) responsive.
        """
        final = self.path(clip)
        await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
        tmp = final.with_name(f"{final.name}.{secrets.token_hex(4)}.part")
        digest = hashlib.sha256()
        size = 0
        f = await asyncio.to_thread(open, tmp, "wb")
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > self.max_bytes:
                    raise StorageError(f"clip exceeds SKYNET_MAX_CLIP_BYTES ({self.max_bytes})")
                digest.update(chunk)
                await asyncio.to_thread(f.write, chunk)
        except BaseException:
            f.close()
            tmp.unlink(missing_ok=True)
            raise
        f.close()
        return size, digest.hexdigest(), tmp

    async def accept(self, tmp: Path, clip: ClipRecord) -> None:
        await asyncio.to_thread(os.replace, tmp, self.path(clip))

    @staticmethod
    def discard(tmp: Path) -> None:
        tmp.unlink(missing_ok=True)

    async def verify(self, clip: ClipRecord, size: int, sha256: str) -> None:
        path = self.path(clip)
        if not path.exists():
            raise StorageError("no upload received for this clip")
        if clip.bytes is not None and clip.sha256 is not None:
            actual = (clip.bytes, clip.sha256)  # recorded while streaming the upload
        else:
            actual = await asyncio.to_thread(_hash_file, path)
        if actual != (size, sha256.lower()):
            raise StorageError(f"uploaded file is {actual[0]} bytes with sha256 {actual[1]}, not as reported")

    def download_url(self, clip: ClipRecord, base_url: str) -> Optional[str]:
        return f"{base_url}/api/v1/clips/{clip.request_id}/download"


def _hash_file(path: Path) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


class S3Storage(Storage):
    kind = "s3"

    def __init__(self, cfg: S3Config):
        self.cfg = cfg

    def key(self, clip: ClipRecord) -> str:
        return self.cfg.prefix + clip_key(clip)

    def presign(self, method: str, key: str, expires_s: int) -> str:
        c = self.cfg
        return presign_url(method, c.endpoint, c.bucket, key, c.region, c.access_key_id, c.secret_access_key,
                           expires_s, c.path_style)

    def upload_target(self, clip: ClipRecord, base_url: str) -> Dict[str, object]:
        return {"method": "PUT", "url": self.presign("PUT", self.key(clip), UPLOAD_TTL_S),
                "headers": {"Content-Type": CONTENT_TYPE}}

    async def verify(self, clip: ClipRecord, size: int, sha256: str) -> None:
        """Check that the object exists with the reported size (a HEAD on a
        presigned URL; S3 cannot confirm a SHA-256 the client did not send)."""
        if not self.cfg.verify_uploads:
            return
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.head(self.presign("HEAD", self.key(clip), 300))
        if resp.status_code == 404:
            raise StorageError("no object found for this clip")
        if resp.status_code >= 300:
            raise StorageError(f"object storage answered {resp.status_code} to HEAD")
        length = resp.headers.get("content-length")
        if length is not None and int(length) != size:
            raise StorageError(f"stored object is {length} bytes, not {size}")

    def download_url(self, clip: ClipRecord, base_url: str) -> Optional[str]:
        key = self.key(clip)
        if self.cfg.public_base_url:
            return f"{self.cfg.public_base_url}/{_uri_encode(key, True)}"
        return self.presign("GET", key, DOWNLOAD_TTL_S)


def create_storage(settings: Settings) -> Storage:
    if settings.storage == "s3":
        assert settings.s3 is not None
        return S3Storage(settings.s3)
    if settings.storage == "local":
        return LocalStorage(settings.storage_dir, settings.max_clip_bytes)
    return Storage()
