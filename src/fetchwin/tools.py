"""Discovery of the external CLI tools fetchwin drives."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from fetchwin.errors import FetchwinError


@dataclass(frozen=True)
class Toolchain:
    wimlib: str
    iso_builder: str
    iso_flavor: str  # "cdrtools" (mkisofs) or "genisoimage"
    bsdtar: str | None
    cabextract: str | None

    def versions(self) -> dict[str, str]:
        out = {
            "wimlib-imagex": _first_line([self.wimlib, "--version"]),
            "iso_builder": _first_line([self.iso_builder, "-version"]),
        }
        if self.bsdtar:
            out["bsdtar"] = _first_line([self.bsdtar, "--version"])
        if self.cabextract:
            out["cabextract"] = _first_line([self.cabextract, "--version"])
        return out


def _first_line(argv: list[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except OSError as e:
        return f"unavailable ({e})"
    text = (r.stdout or r.stderr).strip()
    return text.splitlines()[0] if text else "unknown"


def _resolve(env_var: str, *names: str) -> str | None:
    override = os.environ.get(env_var)
    if override:
        path = shutil.which(override)
        if not path:
            raise FetchwinError(f"{env_var}={override!r} is not an executable")
        return path
    for n in names:
        path = shutil.which(n)
        if path:
            return path
    return None


def detect_flavor(iso_builder: str) -> str:
    text = _first_line([iso_builder, "-version"]).lower()
    # Debian's mkisofs is a genisoimage shim that prints a joke banner mentioning 2.01.
    if "genisoimage" in text or "mkisofs 2.01" in text:
        return "genisoimage"
    if "mkisofs" in text:
        return "cdrtools"
    raise FetchwinError(f"unrecognised ISO builder {iso_builder!r}: {text!r}")


def discover(*, need_build: bool = True) -> Toolchain:
    """Locate tools. Raises with install hints if a required tool is missing."""
    bsdtar = _resolve("FETCHWIN_BSDTAR", "bsdtar")
    cabextract = _resolve("FETCHWIN_CABEXTRACT", "cabextract")
    wimlib = _resolve("FETCHWIN_WIMLIB", "wimlib-imagex")
    builder = _resolve("FETCHWIN_MKISOFS", "mkisofs", "genisoimage")
    # Prefer genisoimage by name only if mkisofs is absent.
    missing = []
    if not (bsdtar or cabextract):
        missing.append("bsdtar or cabextract")
    if need_build and not wimlib:
        missing.append("wimlib-imagex")
    if need_build and not builder:
        missing.append("mkisofs (cdrtools) or genisoimage")
    if missing:
        raise FetchwinError(
            "missing required tools: "
            + ", ".join(missing)
            + "\n  macOS:  brew bundle   (uses the repo Brewfile: wimlib, cdrtools)"
            + "\n  Debian: apt-get install wimtools genisoimage libarchive-tools"
            + "\n  or run via Docker: docker-compose run --rm fetchwin ..."
        )
    flavor = detect_flavor(builder) if builder else "none"
    return Toolchain(
        wimlib=wimlib or "",
        iso_builder=builder or "",
        iso_flavor=flavor,
        bsdtar=bsdtar,
        cabextract=cabextract,
    )
