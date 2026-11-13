from __future__ import annotations

import base64
import hashlib
import io
import json
from typing import Any

import pytest

from fetchwin import cli, ums
from fetchwin.catalog import CATALOG_URLS
from fetchwin.errors import FetchwinError
from fetchwin.tools import Toolchain
from tests.conftest import FIXTURES

CAB = b"MSCF" + b"\0" * 60
CAB_URL = "http://tlu.dl.delivery.mp.microsoft.com/filestreamingservice/files/abc?P1=1"


def _resp(cab: bytes = CAB, url: str = CAB_URL, **override: Any) -> bytes:
    loc = {
        "FileName": "products.cab",
        "Size": len(cab),
        "Language": None,
        "Digest": base64.b64encode(hashlib.sha256(cab).digest()).decode(),
        "ContentType": "Metadata",
        "Url": url,
    }
    loc.update(override)
    return json.dumps([{"UpdateIds": ["80342f72-0000"], "FileLocations": [loc]}]).encode()


def test_pinned_root_is_intact_and_is_the_only_trust_anchor() -> None:
    ctx = ums.pinned_context()
    cas = ctx.get_ca_certs()
    assert len(cas) == 1
    subject = str(cas[0]["subject"])
    assert "Microsoft Root Certificate Authority 2011" in subject
    assert ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname


def test_tampered_root_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ums, "ROOT_SHA256", "0" * 64)
    with pytest.raises(FetchwinError, match="does not match its pinned hash"):
        ums.pinned_context()


def test_request_body_is_public_release_only() -> None:
    body = json.loads(ums.build_request())
    assert body["Products"] == "PN=Windows.Products.Cab.amd64&V=0.0.0.0"
    assert "DUScan=1" in body["DeviceAttributes"]
    assert "DUInternal" not in body["DeviceAttributes"]


def test_parse_response_ok() -> None:
    loc = ums.parse_response(_resp())
    assert loc.update_id == "80342f72-0000"
    assert loc.url == CAB_URL
    assert loc.sha256 == hashlib.sha256(CAB).hexdigest()


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"[]", "no products.cab"),
        (b"{}", "not a list"),
        (b"nope", "invalid JSON"),
        (_resp(Digest="!!notbase64"), "malformed digest"),
        (_resp(Digest=base64.b64encode(b"short").decode()), "not SHA-256"),
        (_resp(Size=-1), "invalid products.cab size"),
        (_resp(url="http://evil.example.com/products.cab"), "non-Microsoft URL"),
        (_resp(url="http://microsoft.com.evil.example/x"), "non-Microsoft URL"),
    ],
)
def test_parse_response_rejects(data: bytes, match: str) -> None:
    with pytest.raises(FetchwinError, match=match):
        ums.parse_response(data)


def test_fetch_verifies_size_and_digest() -> None:
    posted: list[tuple[str, bytes]] = []

    def post(url: str, body: bytes) -> bytes:
        posted.append((url, body))
        return _resp()

    loc, data = ums.fetch_products_cab(post=post, get=lambda u: CAB)
    assert data == CAB and loc.size == len(CAB)
    assert posted[0][0] == ums.UMS_URL

    with pytest.raises(FetchwinError, match="size"):
        ums.fetch_products_cab(post=post, get=lambda u: CAB + b"x")
    tampered = b"MSCF" + b"\1" * 60
    with pytest.raises(FetchwinError, match="sha256"):
        ums.fetch_products_cab(post=post, get=lambda u: tampered)
    with pytest.raises(FetchwinError, match="not a CAB"):
        notcab = b"<xml/>" + b"\0" * 58
        ums.fetch_products_cab(post=lambda u, b: _resp(cab=notcab), get=lambda u: notcab)


TOOLS = Toolchain("wimlib-imagex", "mkisofs", "cdrtools", "bsdtar", None)
XML = (FIXTURES / "products.xml").read_bytes()


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, log = io.StringIO(), io.StringIO()
    return cli.main(argv, out=out, log=log, tools=TOOLS), out.getvalue(), log.getvalue()


def test_cli_auto_uses_ums(monkeypatch: pytest.MonkeyPatch) -> None:
    loc = ums.CabLocation("uid-1", CAB_URL, len(XML), "x")
    monkeypatch.setattr(ums, "fetch_products_cab", lambda: (loc, XML))

    def no_fwlink(url: str) -> bytes:
        raise AssertionError("must not touch fwlink when UMS works")

    monkeypatch.setattr(cli, "http_get", no_fwlink)
    code, out, err = _run(["fetch", "--dry-run", "--json"])
    assert code == 0, err
    assert json.loads(out)["catalog"].startswith("ums:") and "update=uid-1" in out
    assert "WARNING" not in err


def test_cli_auto_falls_back_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise FetchwinError("UMS offered no products.cab")

    fetched: list[str] = []

    def fake_get(url: str) -> bytes:
        fetched.append(url)
        return XML

    monkeypatch.setattr(ums, "fetch_products_cab", broken)
    monkeypatch.setattr(cli, "http_get", fake_get)
    code, out, err = _run(["fetch", "--dry-run", "--json"])
    assert code == 0
    assert "WARNING: live catalog unusable" in err
    assert fetched == [CATALOG_URLS["11"]]
    assert json.loads(out)["catalog"] == CATALOG_URLS["11"]


def test_cli_ums_mode_fails_hard(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise OSError("network down")

    monkeypatch.setattr(ums, "fetch_products_cab", broken)
    monkeypatch.setattr(cli, "http_get", lambda url: pytest.fail("no fallback in ums mode"))
    code, _, err = _run(["fetch", "--dry-run", "--catalog", "ums"])
    assert code == 1
    assert "live catalog query failed: network down" in err


def test_cli_windows10_defaults_to_fwlink_and_rejects_ums(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ums, "fetch_products_cab", lambda: pytest.fail("no UMS for Windows 10"))
    fetched: list[str] = []

    def fake_get(url: str) -> bytes:
        fetched.append(url)
        return XML

    monkeypatch.setattr(cli, "http_get", fake_get)
    code, _, _ = _run(["list", "--windows", "10"])
    assert code == 0 and fetched == [CATALOG_URLS["10"]]
    code, _, err = _run(["list", "--windows", "10", "--catalog", "ums"])
    assert code == 1 and "only available for Windows 11" in err


def _ums_returns(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> list[str]:
    """UMS 'works' but hands back `raw` as the catalog; record fwlink fallbacks."""
    loc = ums.CabLocation("uid-1", CAB_URL, len(raw), "x")
    monkeypatch.setattr(ums, "fetch_products_cab", lambda: (loc, raw))
    fetched: list[str] = []

    def fake_get(url: str) -> bytes:
        fetched.append(url)
        return XML

    monkeypatch.setattr(cli, "http_get", fake_get)
    return fetched


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (b"<MCT><not-a-catalog/></MCT>", "no ESD entries"),
        (b"<MCT>", "not valid XML"),
        (b"\x00garbage", "neither a CAB"),
    ],
)
def test_cli_auto_falls_back_when_ums_catalog_is_unusable(
    monkeypatch: pytest.MonkeyPatch, raw: bytes, why: str
) -> None:
    fetched = _ums_returns(monkeypatch, raw)
    code, _, err = _run(["fetch", "--dry-run", "--json"])
    assert code == 0, err
    assert "WARNING" in err and why in err
    assert fetched == [CATALOG_URLS["11"]]


def test_cli_auto_falls_back_when_ums_catalog_lacks_requested_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A valid catalog that only has de-de: en-us request must fall back, not fail.
    only_de = XML.replace(
        b"<LanguageCode>en-us</LanguageCode>", b"<LanguageCode>xx-xx</LanguageCode>"
    )
    fetched = _ums_returns(monkeypatch, only_de)
    code, out, err = _run(["fetch", "--dry-run", "--json", "--lang", "en-us"])
    assert code == 0, err
    assert "WARNING" in err and "no consumer arm64 ESD for en-us" in err
    assert json.loads(out)["catalog"] == CATALOG_URLS["11"]
    assert fetched == [CATALOG_URLS["11"]]


def test_cli_ums_mode_still_fails_hard_on_unusable_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = _ums_returns(monkeypatch, b"<MCT>")
    code, _, err = _run(["fetch", "--dry-run", "--catalog", "ums"])
    assert code == 1 and "not valid XML" in err
    assert fetched == []
