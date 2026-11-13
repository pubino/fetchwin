"""Resumable, hash-verified ESD download.

Microsoft's ESD CDN (dl.delivery.mp.microsoft.com) does not present a valid
TLS certificate, so the payload is fetched over plain HTTP exactly as the
catalog lists it. Integrity comes from the size and hash published in the
catalog, which itself is fetched over HTTPS.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import IO

from fetchwin.errors import FetchwinError

CHUNK = 1024 * 1024


def _hash_file(path: Path, algo: str) -> hashlib._Hash:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h


def verify(path: Path, size: int, algo: str, digest: str) -> None:
    actual_size = path.stat().st_size
    if actual_size != size:
        raise FetchwinError(f"{path.name}: size {actual_size} != expected {size}")
    actual = _hash_file(path, algo).hexdigest()
    if actual != digest.lower():
        raise FetchwinError(f"{path.name}: {algo} {actual} != expected {digest}")


class _Progress:
    def __init__(self, total: int, start: int, stream: IO[str]) -> None:
        self.total, self.done, self.stream = total, start, stream
        self.enabled = stream.isatty()
        self.t0, self.start, self.last = time.monotonic(), start, 0.0

    def update(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if not self.enabled or now - self.last < 0.5:
            return
        self.last = now
        rate = (self.done - self.start) / max(now - self.t0, 1e-6)
        pct = 100.0 * self.done / self.total if self.total else 0.0
        self.stream.write(
            f"\r  {pct:5.1f}%  {self.done / 1e9:6.2f}/{self.total / 1e9:.2f} GB"
            f"  {rate / 1e6:6.1f} MB/s "
        )
        self.stream.flush()

    def close(self) -> None:
        if self.enabled:
            self.stream.write("\n")


def download(
    url: str,
    dest: Path,
    *,
    size: int,
    algo: str,
    digest: str,
    timeout: float = 60.0,
    log: IO[str] = sys.stderr,
) -> Path:
    """Download url to dest, resuming from dest.part. Idempotent once dest verifies."""
    if dest.exists():
        log.write(f"Verifying existing {dest.name} ...\n")
        verify(dest, size, algo, digest)
        return dest

    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0

    if have < size:
        headers = {"User-Agent": "fetchwin"}
        if have:
            headers["Range"] = f"bytes={have}-"
            log.write(f"Resuming {dest.name} at {have / 1e9:.2f} GB\n")
        else:
            log.write(f"Downloading {dest.name} ({size / 1e9:.2f} GB)\n")
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 (host validated by catalog)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
            if have and status != 206:
                # Server ignored the Range header; start over.
                have = 0
            mode = "ab" if have else "wb"
            progress = _Progress(size, have, log)
            with part.open(mode) as out:
                while chunk := resp.read(CHUNK):
                    out.write(chunk)
                    progress.update(len(chunk))
                    if out.tell() > size:
                        break
                out.flush()
                os.fsync(out.fileno())
            progress.close()

    log.write(f"Verifying {algo} ...\n")
    try:
        verify(part, size, algo, digest)
    except FetchwinError:
        # A complete-but-wrong file can never become right; don't resume from it.
        if part.stat().st_size >= size:
            part.unlink()
        raise
    part.rename(dest)
    return dest
