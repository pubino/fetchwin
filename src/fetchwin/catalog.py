"""Microsoft ESD product catalog (products.cab / products.xml) handling.

The catalog is the same one the Media Creation Tool uses. Each <File> entry
points at an .esd on Microsoft's CDN together with its size and hash.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from fetchwin.errors import FetchwinError

CATALOG_URLS = {
    "11": "https://go.microsoft.com/fwlink/?LinkId=2156292",
    "10": "https://go.microsoft.com/fwlink/?LinkId=841361",
}

# Friendly arch name -> (catalog Architecture value, filename tag).
ARCHES = {
    "arm64": ("ARM64", "A64FRE"),
    "x64": ("x64", "X64FRE"),
}

# Friendly channel name -> filename tag.
CHANNELS = {
    "consumer": "CLIENTCONSUMER_RET",
    "business": "CLIENTBUSINESS_VOL",
}

MAX_CATALOG_BYTES = 64 * 1024 * 1024
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.esd$")
_BUILD_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-(\d+)\.")
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8}){0,2}$")


@dataclass(frozen=True)
class EsdEntry:
    filename: str
    language_code: str
    architecture: str
    size: int
    url: str
    sha1: str | None
    sha256: str | None
    catalog_editions: tuple[str, ...] = ()

    @property
    def build(self) -> tuple[int, ...]:
        return build_key(self.filename)

    @property
    def label(self) -> str:
        """Volume label in the style Microsoft/CrystalFetch use: '26100.4349.250607-1500'."""
        m = _BUILD_RE.match(self.filename)
        if m is None:
            raise FetchwinError(f"cannot parse build number from {self.filename!r}")
        return f"{m[1]}.{m[2]}.{m[3]}-{m[4]}"

    @property
    def iso_name(self) -> str:
        return self.filename[: -len(".esd")] + ".iso"

    def hash(self) -> tuple[str, str]:
        """Strongest available (algorithm, hexdigest)."""
        if self.sha256:
            return "sha256", self.sha256
        if self.sha1:
            return "sha1", self.sha1
        raise FetchwinError(f"catalog entry {self.filename} has no hash")

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["catalog_editions"] = list(self.catalog_editions)
        d["label"] = self.label
        return d


def build_key(filename: str) -> tuple[int, ...]:
    m = _BUILD_RE.match(filename)
    if not m:
        raise FetchwinError(f"cannot parse build number from {filename!r}")
    return tuple(int(g) for g in m.groups())


def _validate_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not (
        host == "microsoft.com" or host.endswith(".microsoft.com")
    ):
        raise FetchwinError(f"refusing non-Microsoft download URL in catalog: {url!r}")
    return url


def _hex(value: str | None, length: int) -> str | None:
    if value is None:
        return None
    v = value.strip().lower()
    if not re.fullmatch(rf"[0-9a-f]{{{length}}}", v):
        raise FetchwinError(f"malformed hash in catalog: {value!r}")
    return v


def parse_products_xml(data: bytes) -> list[EsdEntry]:
    """Parse and validate catalog XML. Entries that fail validation abort the parse."""
    if len(data) > MAX_CATALOG_BYTES:
        raise FetchwinError("catalog is unexpectedly large; refusing to parse")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FetchwinError(f"catalog is not valid XML: {e}") from e

    entries: dict[tuple[str, str, str], EsdEntry] = {}
    editions: dict[tuple[str, str, str], set[str]] = {}
    for f in root.iter("File"):

        def text(tag: str, f: ET.Element = f) -> str | None:
            v = f.findtext(tag)
            return v.strip() if v and v.strip() else None

        filename, url, size = text("FileName"), text("FilePath"), text("Size")
        lang, arch = text("LanguageCode"), text("Architecture")
        if not (filename and url and size and lang and arch):
            continue  # non-ESD helper entries
        if not filename.lower().endswith(".esd"):
            continue
        if not _FILENAME_RE.match(filename) or not _BUILD_RE.match(filename):
            raise FetchwinError(f"suspicious ESD filename in catalog: {filename!r}")
        if not size.isdigit():
            raise FetchwinError(f"malformed size for {filename}: {size!r}")
        entry = EsdEntry(
            filename=filename,
            language_code=lang.lower(),
            architecture=arch,
            size=int(size),
            url=_validate_url(url),
            sha1=_hex(text("Sha1"), 40),
            sha256=_hex(text("Sha256"), 64),
        )
        if entry.sha1 is None and entry.sha256 is None:
            raise FetchwinError(f"catalog entry {filename} has no hash")
        # The same ESD is listed once per edition it contains; collapse duplicates.
        key = (entry.filename, entry.language_code, entry.architecture)
        prev = entries.get(key)
        if prev is not None and prev.hash() != entry.hash():
            raise FetchwinError(f"conflicting hashes for {filename} in catalog")
        entries[key] = entry
        edition = text("Edition")
        if edition:
            editions.setdefault(key, set()).add(edition)
    if not entries:
        raise FetchwinError("catalog contains no ESD entries")
    merged = [
        replace(e, catalog_editions=tuple(sorted(editions.get(k, set()))))
        for k, e in entries.items()
    ]
    return sorted(merged, key=lambda e: (e.build, e.filename, e.language_code))


def matching(
    entries: Iterable[EsdEntry], *, arch: str, lang: str | None, channel: str
) -> list[EsdEntry]:
    cat_arch, arch_tag = ARCHES[arch]
    chan_tag = CHANNELS[channel]
    return [
        e
        for e in entries
        if e.architecture.lower() == cat_arch.lower()
        # Microsoft is inconsistent about case ("A64FRE" vs "x64FRE").
        and arch_tag.lower() in e.filename.lower()
        and chan_tag.lower() in e.filename.lower()
        and (lang is None or e.language_code == lang.lower())
    ]


def select_latest(entries: list[EsdEntry], *, arch: str, lang: str, channel: str) -> EsdEntry:
    if not _LANG_RE.match(lang.lower()):
        raise FetchwinError(f"invalid language code {lang!r} (expected e.g. en-us)")
    found = matching(entries, arch=arch, lang=lang, channel=channel)
    if not found:
        langs = sorted(
            {e.language_code for e in matching(entries, arch=arch, lang=None, channel=channel)}
        )
        hint = f"; available languages: {', '.join(langs)}" if langs else ""
        raise FetchwinError(f"no {channel} {arch} ESD for {lang} in catalog{hint}")
    return max(found, key=lambda e: e.build)


Fetcher = Callable[[str], bytes]
CabExtractor = Callable[[Path, Path], Path]


def http_get(url: str, timeout: float = 60.0) -> bytes:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise FetchwinError(f"catalog URL must be https: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "fetchwin"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data: bytes = resp.read(MAX_CATALOG_BYTES + 1)
    if len(data) > MAX_CATALOG_BYTES:
        raise FetchwinError("catalog download is unexpectedly large")
    return data


def read_catalog_source(source: str, *, fetch: Fetcher) -> bytes:
    """Raw bytes of a catalog given as an https URL or a local .cab/.xml path."""
    if source.startswith(("https://", "http://")):
        return fetch(source)
    path = Path(source).expanduser()
    if not path.is_file():
        raise FetchwinError(f"catalog file not found: {path}")
    return path.read_bytes()


def catalog_xml(raw: bytes, *, extract_cab: CabExtractor) -> bytes:
    """products.xml bytes from raw catalog bytes (a CAB or the XML itself)."""
    if raw[:4] == b"MSCF":  # Microsoft Cabinet signature
        with tempfile.TemporaryDirectory(prefix="fetchwin-cab-") as td:
            cab = Path(td) / "products.cab"
            cab.write_bytes(raw)
            out = Path(td) / "x"
            out.mkdir()
            return extract_cab(cab, out).read_bytes()
    if raw.lstrip()[:1] == b"<":
        return raw
    raise FetchwinError("catalog is neither a CAB archive nor XML")


def load_catalog_bytes(
    source: str, *, fetch: Fetcher, extract_cab: CabExtractor
) -> tuple[bytes, bytes]:
    """Return (raw source bytes, products.xml bytes) from a URL or local .cab/.xml path."""
    raw = read_catalog_source(source, fetch=fetch)
    return raw, catalog_xml(raw, extract_cab=extract_cab)


def find_products_xml(out_dir: Path) -> Path:
    for p in out_dir.rglob("*"):
        if p.is_file() and p.name.lower() == "products.xml":
            return p
    raise FetchwinError("products.xml not found inside catalog CAB")


def default_cab_extractor(bsdtar: str | None, cabextract: str | None) -> CabExtractor:
    import subprocess

    def extract(cab: Path, out: Path) -> Path:
        if bsdtar:
            argv = [bsdtar, "-xf", str(cab), "-C", str(out)]
        elif cabextract:
            argv = [cabextract, "-q", "-d", str(out), str(cab)]
        else:
            raise FetchwinError("need bsdtar or cabextract to unpack the catalog CAB")
        r = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        if r.returncode != 0:
            raise FetchwinError(f"CAB extraction failed: {r.stderr.strip()}")
        return find_products_xml(out)

    return extract


def which(name: str) -> str | None:
    return shutil.which(name)
