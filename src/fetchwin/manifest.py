"""Provenance sidecar written next to each ISO, so builds can be audited and reproduced."""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fetchwin import __version__
from fetchwin.catalog import EsdEntry
from fetchwin.convert import ConvertResult


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(iso: Path) -> Path:
    return iso.with_name(iso.name + ".json")


def write_manifest(
    iso: Path,
    *,
    entry: EsdEntry,
    catalog_source: str,
    catalog_sha256: str,
    options: dict[str, Any],
    tool_versions: dict[str, str],
    result: ConvertResult,
) -> dict[str, Any]:
    iso_sha = sha256_file(iso)
    data: dict[str, Any] = {
        "fetchwin": __version__,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": {"system": platform.system(), "machine": platform.machine()},
        "catalog": {"source": catalog_source, "sha256": catalog_sha256},
        "esd": entry.to_dict(),
        "options": options,
        "tools": tool_versions,
        "install_images": [
            {"index": i.index, "name": i.name, "edition_id": i.edition_id}
            for i in result.install_images
        ],
        "install_wim_split": result.split,
        "iso": {"name": iso.name, "size": iso.stat().st_size, "sha256": iso_sha},
    }
    manifest_path(iso).write_text(json.dumps(data, indent=2) + "\n")
    iso.with_name(iso.name + ".sha256").write_text(f"{iso_sha}  {iso.name}\n")
    return data


def existing_build_matches(iso: Path, entry: EsdEntry, options: dict[str, Any]) -> bool:
    """True if iso exists and its manifest says it came from this ESD with these options."""
    mp = manifest_path(iso)
    if not (iso.is_file() and mp.is_file()):
        return False
    try:
        data = json.loads(mp.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        data.get("esd", {}).get("filename") == entry.filename
        and data.get("esd", {}).get("sha1") == entry.sha1
        and data.get("esd", {}).get("sha256") == entry.sha256
        and data.get("options") == options
        and data.get("iso", {}).get("size") == iso.stat().st_size
    )
