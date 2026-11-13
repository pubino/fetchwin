"""Find, download and (optionally) integrate the latest cumulative update for built media.

Flow:
  1. The ISO's base build (e.g. 26300.9711) comes from the ESD label.
  2. Microsoft's Windows release-information page says which build is the latest for
     that release (e.g. 26H2 -> 26300.9974).
  3. Windows Update's Update Metadata Service (UMS, see fetchwin.ums) returns the
     update files for that exact build, each with a SHA-256 digest.
  4. The .msu packages are downloaded and verified into <iso-stem>.updates/, with an
     updates.json manifest naming the target package, and Install-FetchwinUpdates.ps1,
     which installs it on a running Windows system.

The offer holds the target cumulative update and every checkpoint update before it,
including old ones the media already contains. Only the target is installed: DISM
takes whatever checkpoints the system lacks from the same folder (Microsoft's "in one
go" method). Installing each package in turn failed: DISM on 26300 cannot parse the
24H2 checkpoint KB5043080 (0x80070228).

Integration into install.wim needs an offline-servicing worker. If integration is
unavailable or fails, the verified packages stay next to the ISO for the installer
(e.g. an unattended setup) to apply. That fallback is the default, not an error.
"""

from __future__ import annotations

import base64
import binascii
import importlib.resources
import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import IO, Any

from fetchwin import ums
from fetchwin.errors import FetchwinError

RELEASE_INFO_URL = (
    "https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information"
)
UMS_UPDATE_URL = "https://fe3.delivery.mp.microsoft.com/UpdateMetadataService/updates/v1/"
UMS_ARCH = {"x64": ("amd64", "AMD64", "x64"), "arm64": ("arm64", "ARM64", "arm64")}
_MSU_RE = re.compile(r"^Windows1[01]\.0-KB(\d{6,8})-(x64|arm64)\.msu$", re.I)
_BUILD_RE = re.compile(r"^(\d{5})\.(\d{1,5})$")
INSTALLER = "Install-FetchwinUpdates.ps1"


class IntegrationUnavailable(FetchwinError):
    """Offline integration can't run here; packages are left alongside the ISO."""


@dataclass(frozen=True)
class ReleaseRow:
    version: str
    latest_update: str
    latest_build: str


@dataclass(frozen=True)
class UpdatePackage:
    filename: str
    kb: str
    size: int
    sha256: str
    url: str


@dataclass
class UpdatesResult:
    status: str  # "current" | "downloaded" | "integrated"
    base_build: str
    target_build: str
    release: dict[str, str] = field(default_factory=dict)
    update_id: str = ""
    title: str = ""
    packages: list[dict[str, Any]] = field(default_factory=list)
    target: str = ""
    directory: str = ""
    installer: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- release information page ----------


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self.tables:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_release_info(html: str) -> list[ReleaseRow]:
    """Rows of the summary table (the one with a 'Latest build' column)."""
    p = _TableParser()
    p.feed(html)
    for table in p.tables:
        if not table:
            continue
        header = [h.lower() for h in table[0]]
        if "latest build" in header and "version" in header:
            iv, ib = header.index("version"), header.index("latest build")
            iu = header.index("latest update") if "latest update" in header else None
            rows = []
            for r in table[1:]:
                if len(r) <= max(iv, ib) or not _BUILD_RE.match(r[ib]):
                    continue
                rows.append(ReleaseRow(r[iv], r[iu] if iu is not None else "", r[ib]))
            if rows:
                return rows
    raise FetchwinError("release information page has no recognizable 'Latest build' table")


def latest_for_base(base_build: str, rows: list[ReleaseRow]) -> ReleaseRow:
    major = base_build.split(".")[0]
    for r in rows:
        if r.latest_build.split(".")[0] == major:
            return r
    raise FetchwinError(f"release information lists no release for build {major}")


def _rev(build: str) -> int:
    return int(build.split(".")[1])


# ---------- UMS offer ----------


def offer_request(arch: str, build: str) -> bytes:
    pn, osarch, _ = UMS_ARCH[arch]
    return json.dumps(
        {
            "Products": (
                f"PN=Client.OS.rs2.{pn}&Branch=ge_release&PrimaryOSProduct=1&V=10.0.{build}"
            ),
            "DeviceAttributes": f"DUScan=1;OSArchitecture={osarch};OSVersion=10.0.{build}",
        }
    ).encode()


def msu_packages(offer_json: bytes, arch: str) -> tuple[str, list[UpdatePackage]]:
    try:
        offers = json.loads(offer_json)
    except json.JSONDecodeError as e:
        raise FetchwinError(f"UMS returned invalid JSON: {e}") from e
    if not isinstance(offers, list) or not offers:
        raise FetchwinError("UMS has no update offer for that build")
    if len(offers) != 1 or not isinstance(offers[0], dict):
        raise FetchwinError("UMS returned an unexpected number of update offers")
    offer = offers[0]
    ids = offer.get("UpdateIds") or [""]
    want_tag = UMS_ARCH[arch][2]
    pkgs: dict[str, UpdatePackage] = {}
    for loc in offer.get("FileLocations") or []:
        if not isinstance(loc, dict):
            continue
        m = _MSU_RE.match(str(loc.get("FileName", "")))
        if not m or m.group(2).lower() != want_tag:
            continue
        size, digest, url = loc.get("Size"), loc.get("Digest"), loc.get("Url")
        if not isinstance(size, int) or size <= 0:
            raise FetchwinError(f"invalid size for {loc.get('FileName')}")
        try:
            raw = base64.b64decode(str(digest), validate=True)
        except (binascii.Error, ValueError) as e:
            raise FetchwinError(f"malformed digest for {loc.get('FileName')}") from e
        if len(raw) != 32:
            raise FetchwinError(f"digest for {loc.get('FileName')} is not SHA-256")
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
        if not host.endswith(".microsoft.com"):
            raise FetchwinError(f"non-Microsoft URL for {loc.get('FileName')}: {url!r}")
        name = str(loc["FileName"])
        pkgs[name.lower()] = UpdatePackage(name, f"KB{m.group(1)}", size, raw.hex(), str(url))
    if not pkgs:
        raise FetchwinError(f"UMS offer contains no {want_tag} .msu packages")
    return str(ids[0]), sorted(pkgs.values(), key=lambda p: int(p.kb[2:]))


def pick_target(pkgs: list[UpdatePackage], title: str) -> UpdatePackage:
    """The cumulative update to install: the KB the update's title names, else the newest."""
    named = re.findall(r"\(KB(\d{6,8})\)", title)
    for p in pkgs:
        if named and p.kb == f"KB{named[0]}":
            return p
    return max(pkgs, key=lambda p: int(p.kb[2:]))


def update_title(update_id: str, get: Callable[[str], bytes]) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", update_id):
        return ""
    try:
        meta = json.loads(get(UMS_UPDATE_URL + update_id))
        for p in meta.get("LocalizedProperties") or []:
            if p.get("Language") == "en":
                return str(p.get("Title", ""))
    except (FetchwinError, OSError, ValueError, AttributeError):
        pass
    return ""


def https_get_pinned(url: str) -> bytes:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "fetchwin"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=60, context=ums.pinned_context()) as r:  # noqa: S310
        data: bytes = r.read(ums.MAX_RESPONSE + 1)
    return data


def installer_script() -> bytes:
    """The Windows-side installer for downloaded packages (shipped as package data)."""
    return importlib.resources.files("fetchwin").joinpath("windows", INSTALLER).read_bytes()


# ---------- orchestration ----------

Downloader = Callable[..., Path]


def fetch_updates(
    *,
    base_label: str,
    arch: str,
    dest: Path,
    get_release_page: Callable[[str], bytes],
    post: Callable[[str, bytes], bytes],
    get_pinned: Callable[[str], bytes],
    download: Downloader,
    log: IO[str],
) -> UpdatesResult:
    base = ".".join(base_label.split(".")[:2])
    if not _BUILD_RE.match(base):
        raise FetchwinError(f"cannot derive base build from {base_label!r}")
    rows = parse_release_info(get_release_page(RELEASE_INFO_URL).decode("utf-8", "replace"))
    row = latest_for_base(base, rows)
    result = UpdatesResult(
        status="current", base_build=base, target_build=row.latest_build, release=asdict(row)
    )
    if _rev(row.latest_build) <= _rev(base):
        result.note = f"media is already at the latest build for {row.version}"
        log.write(f"Updates: {base} is current for {row.version}; nothing to download\n")
        return result

    log.write(
        f"Updates: {row.version} latest is {row.latest_build} ({row.latest_update}); "
        f"media is {base}\n"
    )
    update_id, pkgs = msu_packages(post(ums.UMS_URL, offer_request(arch, row.latest_build)), arch)
    result.update_id = update_id
    result.title = update_title(update_id, get_pinned)
    dest.mkdir(parents=True, exist_ok=True)
    for p in pkgs:
        download(p.url, dest / p.filename, size=p.size, algo="sha256", digest=p.sha256, log=log)
    (dest / INSTALLER).write_bytes(installer_script())
    result.status = "downloaded"
    result.directory = str(dest)
    result.installer = INSTALLER
    result.packages = [{k: v for k, v in asdict(p).items() if k != "url"} for p in pkgs]
    result.target = pick_target(pkgs, result.title).filename
    return result


def integrate(result: UpdatesResult, iso: Path) -> None:
    """Integrate downloaded packages into the ISO's install image.

    Not available yet: the WinPE/QEMU offline-servicing worker is still a prototype
    (DISM's image-session host fails to start inside WinPE). Callers fall back to
    leaving the packages alongside the ISO.
    """
    raise IntegrationUnavailable(
        "offline integration is not available yet (WinPE DISM host cannot start); "
        "packages were left alongside the ISO"
    )


def write_updates_manifest(result: UpdatesResult, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "updates.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n")
