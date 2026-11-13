from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from fetchwin.download import download
from fetchwin.errors import FetchwinError
from tests.conftest import FakeServer

PAYLOAD = bytes(range(256)) * 9000  # ~2.3 MB, spans several read chunks


def _sha1(b: bytes) -> str:
    return hashlib.sha1(b, usedforsecurity=False).hexdigest()


def test_fresh_download_verifies_and_renames(http_server: FakeServer, tmp_path: Path) -> None:
    http_server.files["a.esd"] = PAYLOAD
    dest = tmp_path / "a.esd"
    download(
        http_server.url("a.esd"),
        dest,
        size=len(PAYLOAD),
        algo="sha1",
        digest=_sha1(PAYLOAD),
        log=io.StringIO(),
    )
    assert dest.read_bytes() == PAYLOAD
    assert not (tmp_path / "a.esd.part").exists()
    assert "range" not in http_server.requests[0]


def test_resume_sends_range_and_completes(http_server: FakeServer, tmp_path: Path) -> None:
    http_server.files["a.esd"] = PAYLOAD
    (tmp_path / "a.esd.part").write_bytes(PAYLOAD[:1_000_000])
    dest = tmp_path / "a.esd"
    download(
        http_server.url("a.esd"),
        dest,
        size=len(PAYLOAD),
        algo="sha256",
        digest=hashlib.sha256(PAYLOAD).hexdigest(),
        log=io.StringIO(),
    )
    assert dest.read_bytes() == PAYLOAD
    assert http_server.requests[0]["range"] == "bytes=1000000-"


def test_resume_restarts_when_server_ignores_range(http_server: FakeServer, tmp_path: Path) -> None:
    http_server.files["a.esd"] = PAYLOAD
    http_server.honour_range = False
    (tmp_path / "a.esd.part").write_bytes(b"X" * 500)  # garbage prefix must be discarded
    dest = tmp_path / "a.esd"
    download(
        http_server.url("a.esd"),
        dest,
        size=len(PAYLOAD),
        algo="sha1",
        digest=_sha1(PAYLOAD),
        log=io.StringIO(),
    )
    assert dest.read_bytes() == PAYLOAD


def test_hash_mismatch_raises_and_discards_complete_part(
    http_server: FakeServer, tmp_path: Path
) -> None:
    http_server.files["a.esd"] = PAYLOAD
    dest = tmp_path / "a.esd"
    with pytest.raises(FetchwinError, match="sha1"):
        download(
            http_server.url("a.esd"),
            dest,
            size=len(PAYLOAD),
            algo="sha1",
            digest="0" * 40,
            log=io.StringIO(),
        )
    assert not dest.exists()
    assert not (tmp_path / "a.esd.part").exists()


def test_short_body_raises_size_mismatch_and_keeps_part(
    http_server: FakeServer, tmp_path: Path
) -> None:
    http_server.files["a.esd"] = PAYLOAD[:1000]
    dest = tmp_path / "a.esd"
    with pytest.raises(FetchwinError, match="size 1000 != expected"):
        download(
            http_server.url("a.esd"),
            dest,
            size=len(PAYLOAD),
            algo="sha1",
            digest=_sha1(PAYLOAD),
            log=io.StringIO(),
        )
    assert (tmp_path / "a.esd.part").stat().st_size == 1000  # resumable later


def test_existing_valid_file_is_not_redownloaded(http_server: FakeServer, tmp_path: Path) -> None:
    dest = tmp_path / "a.esd"
    dest.write_bytes(PAYLOAD)
    download(
        http_server.url("a.esd"),
        dest,
        size=len(PAYLOAD),
        algo="sha1",
        digest=_sha1(PAYLOAD),
        log=io.StringIO(),
    )
    assert http_server.requests == []


def test_existing_corrupt_file_is_an_error(http_server: FakeServer, tmp_path: Path) -> None:
    dest = tmp_path / "a.esd"
    dest.write_bytes(b"corrupt")
    with pytest.raises(FetchwinError, match="size"):
        download(
            http_server.url("a.esd"),
            dest,
            size=len(PAYLOAD),
            algo="sha1",
            digest=_sha1(PAYLOAD),
            log=io.StringIO(),
        )


def test_oversized_part_is_discarded(http_server: FakeServer, tmp_path: Path) -> None:
    http_server.files["a.esd"] = PAYLOAD
    (tmp_path / "a.esd.part").write_bytes(PAYLOAD + b"extra")
    dest = tmp_path / "a.esd"
    download(
        http_server.url("a.esd"),
        dest,
        size=len(PAYLOAD),
        algo="sha1",
        digest=_sha1(PAYLOAD),
        log=io.StringIO(),
    )
    assert dest.read_bytes() == PAYLOAD
    assert "range" not in http_server.requests[0]
