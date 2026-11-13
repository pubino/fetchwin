"""Live catalog lookup via Windows Update's Update Metadata Service (UMS).

Since 25H2 the Media Creation Tool doesn't read the frozen fwlink catalog first.
Its udiapi.dll asks UMS for the "Windows.Products.Cab" product and downloads
whatever products.cab UMS currently offers, falling back to the fwlink only if
that fails. fetchwin does the same.

UMS (fe3.delivery.mp.microsoft.com) presents a certificate chained to
"Microsoft Root Certificate Authority 2011". Windows trusts that root, but the
public CA bundles on macOS and Linux don't. We pin exactly that root, shipped in
fetchwin/certs and checked against its known fingerprint, and trust nothing else
for this connection.

The API is undocumented. Request shape and attributes were recovered from
MediaCreationTool.exe (SetupMgr.dll / udiapi.dll) and match community findings.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import ssl
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any

from fetchwin.errors import FetchwinError

UMS_URL = (
    "https://fe3.delivery.mp.microsoft.com/UpdateMetadataService/updates/search/v1/bydeviceinfo"
)
# The amd64 products cab covers every architecture (ARM64, x64) and language.
PRODUCTS = "PN=Windows.Products.Cab.amd64&V=0.0.0.0"
# Deliberately minimal and public-release only. Never send DUInternal (internal content).
DEVICE_ATTRIBUTES = "DUScan=1;OSVersion=10.0.26100.1"

ROOT_PEM = "microsoft-root-ca-2011.pem"
# SHA-256 of the DER of "Microsoft Root Certificate Authority 2011"
# (SHA-1 thumbprint 8F43288AD272F3103B6FB1428485EA3014C0BCFE).
ROOT_SHA256 = "847df6a78497943f27fc72eb93f9a637320a02b561d0a91b09e87a7807ed7c61"

MAX_RESPONSE = 4 * 1024 * 1024
MAX_CAB = 16 * 1024 * 1024

Post = Callable[[str, bytes], bytes]
Get = Callable[[str], bytes]


@dataclass(frozen=True)
class CabLocation:
    update_id: str
    url: str
    size: int
    sha256: str  # hex


def pinned_root_pem() -> str:
    pem = resources.files("fetchwin.certs").joinpath(ROOT_PEM).read_text()
    der = ssl.PEM_cert_to_DER_cert(pem)
    if hashlib.sha256(der).hexdigest() != ROOT_SHA256:
        raise FetchwinError("bundled Microsoft root certificate does not match its pinned hash")
    return pem


def pinned_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies certs + hostname
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cadata=pinned_root_pem())
    return ctx


def https_post(url: str, body: bytes, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(  # noqa: S310 (fixed https URL)
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "*/*", "User-Agent": "fetchwin"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=pinned_context()) as r:  # noqa: S310
        data: bytes = r.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise FetchwinError("UMS response is unexpectedly large")
    return data


def http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "fetchwin"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (host validated)
        data: bytes = r.read(MAX_CAB + 1)
    if len(data) > MAX_CAB:
        raise FetchwinError("products.cab download is unexpectedly large")
    return data


def build_request() -> bytes:
    return json.dumps({"Products": PRODUCTS, "DeviceAttributes": DEVICE_ATTRIBUTES}).encode()


def _microsoft_url(url: object) -> str:
    if not isinstance(url, str):
        raise FetchwinError("UMS returned a non-string URL")
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host.endswith(".microsoft.com"):
        raise FetchwinError(f"UMS returned a non-Microsoft URL: {url!r}")
    return url


def parse_response(data: bytes) -> CabLocation:
    try:
        offers: Any = json.loads(data)
    except json.JSONDecodeError as e:
        raise FetchwinError(f"UMS returned invalid JSON: {e}") from e
    if not isinstance(offers, list):
        raise FetchwinError("UMS response is not a list of offers")
    found: list[CabLocation] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        ids = offer.get("UpdateIds") or []
        for loc in offer.get("FileLocations") or []:
            if not isinstance(loc, dict) or str(loc.get("FileName", "")).lower() != "products.cab":
                continue
            size, digest = loc.get("Size"), loc.get("Digest")
            if not isinstance(size, int) or not 0 < size <= MAX_CAB:
                raise FetchwinError(f"UMS returned an invalid products.cab size: {size!r}")
            try:
                raw = base64.b64decode(str(digest), validate=True)
            except (binascii.Error, ValueError) as e:
                raise FetchwinError(f"UMS returned a malformed digest: {digest!r}") from e
            if len(raw) != 32:
                raise FetchwinError(f"UMS digest is not SHA-256: {digest!r}")
            found.append(
                CabLocation(
                    update_id=str(ids[0]) if ids else "",
                    url=_microsoft_url(loc.get("Url")),
                    size=size,
                    sha256=raw.hex(),
                )
            )
    if not found:
        raise FetchwinError("UMS offered no products.cab (empty or unexpected response)")
    if len({c.sha256 for c in found}) > 1:
        raise FetchwinError("UMS offered multiple different products.cab files")
    return found[0]


def fetch_products_cab(
    *, post: Post = https_post, get: Get = http_get
) -> tuple[CabLocation, bytes]:
    """Ask UMS for the current products.cab, download it, verify size and SHA-256."""
    loc = parse_response(post(UMS_URL, build_request()))
    data = get(loc.url)
    if len(data) != loc.size:
        raise FetchwinError(f"products.cab size {len(data)} != UMS size {loc.size}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != loc.sha256:
        raise FetchwinError(f"products.cab sha256 {actual} != UMS digest {loc.sha256}")
    if data[:4] != b"MSCF":
        raise FetchwinError("UMS products.cab is not a CAB archive")
    return loc, data
