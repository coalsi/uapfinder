"""Clip storage: SigV4 presigning and the local upload path."""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import AsyncIterator, List

import pytest

from skynet_server.models import ClipRecord
from skynet_server.storage import LocalStorage, S3Storage, StorageError, presign_url
from skynet_server.config import S3Config

UTC = dt.timezone.utc


def test_presign_matches_aws_published_example():
    # "Authenticating Requests: Using Query Parameters (AWS Signature Version 4)",
    # Amazon S3 API reference: GET examplebucket/test.txt, expires 86400 s.
    url = presign_url("GET", "https://s3.amazonaws.com", "examplebucket", "test.txt", "us-east-1",
                      "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", 86400, path_style=False,
                      now=dt.datetime(2013, 5, 24, tzinfo=UTC))
    assert url == (
        "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def test_presign_matches_botocore_for_path_style_put():
    # Produced by botocore 1.x generate_presigned_url("put_object") for a
    # Cloudflare R2 style endpoint (path-style, region "auto").
    url = presign_url("PUT", "https://acct123.r2.cloudflarestorage.com", "skynet-clips", "clips/T-abc/req 1.mp4",
                      "auto", "AKIDEXAMPLE", "secret/KEY+example", 3600, path_style=True,
                      now=dt.datetime(2026, 10, 19, 20, 32, 48, tzinfo=UTC))
    assert url == (
        "https://acct123.r2.cloudflarestorage.com/skynet-clips/clips/T-abc/req%201.mp4"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIDEXAMPLE%2F20261019%2Fauto%2Fs3%2Faws4_request"
        "&X-Amz-Date=20261019T203248Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=912a1cc07aaac3dd62b6871782b41330d075b7363aca9e09dc65fcaa98847350")


def clip() -> ClipRecord:
    return ClipRecord("req1", "ev1", "T-1", "node-a", 1, 2, 3)


def test_s3_targets_and_public_downloads():
    cfg = S3Config("https://acct.r2.cloudflarestorage.com", "bucket", "auto", "AK", "SK", path_style=True,
                   public_base_url="https://clips.example.org")
    s3 = S3Storage(cfg)
    target = s3.upload_target(clip(), "https://api.example.org")
    assert target["method"] == "PUT" and target["url"].startswith(
        "https://acct.r2.cloudflarestorage.com/bucket/clips/T-1/req1.mp4?X-Amz-Algorithm=")
    assert s3.download_url(clip(), "") == "https://clips.example.org/clips/T-1/req1.mp4"


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


async def test_local_storage_receive_accept_verify(tmp_path):
    store = LocalStorage(str(tmp_path), max_bytes=1000)
    c = clip()
    assert store.upload_target(c, "http://mac:8000")["url"] == "http://mac:8000/api/v1/clips/req1/data"
    size, digest, tmp = await store.receive(c, chunks(b"hello ", b"world"))
    assert (size, digest) == (11, hashlib.sha256(b"hello world").hexdigest())
    assert not store.path(c).exists()  # nothing is kept before the caller accepts it
    await store.accept(tmp, c)
    assert store.path(c).read_bytes() == b"hello world" and not tmp.exists()
    await store.verify(c, 11, digest)
    with pytest.raises(StorageError):
        await store.verify(c, 12, digest)
    with pytest.raises(StorageError):
        await store.receive(c, chunks(b"x" * 600, b"y" * 600))
    leftovers: List[str] = [p.name for p in store.path(c).parent.iterdir()]
    assert leftovers == ["req1.mp4"]  # the oversized upload left no partial file
