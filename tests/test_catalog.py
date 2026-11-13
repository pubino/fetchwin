from __future__ import annotations

from pathlib import Path

import pytest

from fetchwin.catalog import (
    find_products_xml,
    load_catalog_bytes,
    matching,
    parse_products_xml,
    select_latest,
)
from fetchwin.errors import FetchwinError


def _file(
    name: str,
    *,
    url: str | None = None,
    sha1: str = "a" * 40,
    size: str = "10",
    arch: str = "ARM64",
    lang: str = "en-us",
    edition: str = "Professional",
) -> str:
    url = url or f"http://dl.delivery.mp.microsoft.com/x/{name}"
    return (
        f"<File><FileName>{name}</FileName><LanguageCode>{lang}</LanguageCode>"
        f"<Edition>{edition}</Edition><Architecture>{arch}</Architecture><Size>{size}</Size>"
        f"<Sha1>{sha1}</Sha1><FilePath>{url}</FilePath></File>"
    )


def _xml(*files: str) -> bytes:
    return ("<MCT><Files>" + "".join(files) + "</Files></MCT>").encode()


GOOD = "26100.1.250101-0000.ge_release_CLIENTCONSUMER_RET_A64FRE_en-us.esd"


def test_parse_collapses_per_edition_duplicates_and_records_editions(products_xml: bytes) -> None:
    entries = parse_products_xml(products_xml)
    names = [e.filename for e in entries]
    assert len(names) == len(set(names))
    assert len(entries) == 5
    newest = next(
        e
        for e in entries
        if e.filename.startswith("26100.4349")
        and "CONSUMER" in e.filename
        and e.language_code == "en-us"
    )
    assert newest.catalog_editions == ("Core", "Professional")
    assert newest.sha1 == "c78fd344e845d3b17cb91c40bf4a856459da1b6c"  # lower-cased
    assert newest.label == "26100.4349.250607-1500"
    assert newest.iso_name.endswith("_A64FRE_en-us.iso")


def test_select_latest_picks_highest_build(products_xml: bytes) -> None:
    e = select_latest(
        parse_products_xml(products_xml), arch="arm64", lang="en-us", channel="consumer"
    )
    assert e.label == "26100.4349.250607-1500"
    assert e.hash() == ("sha1", "c78fd344e845d3b17cb91c40bf4a856459da1b6c")


def test_select_business_and_language(products_xml: bytes) -> None:
    entries = parse_products_xml(products_xml)
    biz = select_latest(entries, arch="arm64", lang="en-us", channel="business")
    assert "CLIENTBUSINESS_VOL_A64FRE" in biz.filename
    de = select_latest(entries, arch="arm64", lang="DE-DE", channel="consumer")
    assert de.language_code == "de-de"


def test_sha256_catalog_schema_is_supported_and_preferred(products_xml: bytes) -> None:
    e = select_latest(
        parse_products_xml(products_xml), arch="x64", lang="en-us", channel="business"
    )
    assert e.sha1 is None
    assert e.hash() == ("sha256", "2" * 64)
    assert e.build == (26200, 6584, 250915, 1905)


def test_no_match_lists_available_languages(products_xml: bytes) -> None:
    with pytest.raises(FetchwinError, match=r"available languages: de-de, en-us"):
        select_latest(
            parse_products_xml(products_xml), arch="arm64", lang="fr-fr", channel="consumer"
        )


def test_invalid_language_rejected(products_xml: bytes) -> None:
    with pytest.raises(FetchwinError, match="invalid language"):
        select_latest(
            parse_products_xml(products_xml), arch="arm64", lang="../etc", channel="consumer"
        )


def test_x64_filter_excludes_arm(products_xml: bytes) -> None:
    found = matching(parse_products_xml(products_xml), arch="x64", lang=None, channel="consumer")
    assert found == []


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example.com/x.esd",
        "http://microsoft.com.evil.example/x.esd",
        "ftp://dl.delivery.mp.microsoft.com/x.esd",
        "file:///etc/passwd",
    ],
)
def test_non_microsoft_urls_rejected(url: str) -> None:
    with pytest.raises(FetchwinError, match="non-Microsoft"):
        parse_products_xml(_xml(_file(GOOD, url=url)))


@pytest.mark.parametrize(
    "name",
    [
        "../../26100.1.250101-0000.x.esd",
        "26100.1.250101-0000/evil.esd",
        "-rf.esd",
        "notabuild.esd",
    ],
)
def test_suspicious_filenames_rejected(name: str) -> None:
    with pytest.raises(FetchwinError, match="suspicious"):
        parse_products_xml(_xml(_file(name)))


def test_bad_hash_and_size_rejected() -> None:
    with pytest.raises(FetchwinError, match="malformed hash"):
        parse_products_xml(_xml(_file(GOOD, sha1="xyz")))
    with pytest.raises(FetchwinError, match="malformed size"):
        parse_products_xml(_xml(_file(GOOD, size="-1")))


def test_conflicting_duplicate_hashes_rejected() -> None:
    with pytest.raises(FetchwinError, match="conflicting"):
        parse_products_xml(
            _xml(_file(GOOD, sha1="a" * 40), _file(GOOD, sha1="b" * 40, edition="Core"))
        )


def test_empty_and_malformed_catalogs_rejected() -> None:
    with pytest.raises(FetchwinError, match="no ESD entries"):
        parse_products_xml(b"<MCT/>")
    with pytest.raises(FetchwinError, match="not valid XML"):
        parse_products_xml(b"<MCT>")


def test_load_catalog_from_xml_path(tmp_path: Path, products_xml: bytes) -> None:
    p = tmp_path / "products.xml"
    p.write_bytes(products_xml)

    def no_fetch(url: str) -> bytes:
        raise AssertionError("must not fetch for a local path")

    def no_cab(cab: Path, out: Path) -> Path:
        raise AssertionError("must not extract plain XML")

    raw, xml = load_catalog_bytes(str(p), fetch=no_fetch, extract_cab=no_cab)
    assert raw == xml == products_xml


def test_load_catalog_cab_goes_through_extractor(products_xml: bytes) -> None:
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b"MSCF" + b"\0" * 32

    def extract(cab: Path, out: Path) -> Path:
        assert cab.read_bytes().startswith(b"MSCF")
        (out / "sub").mkdir()
        target = out / "sub" / "PRODUCTS.XML"
        target.write_bytes(products_xml)
        return find_products_xml(out)

    raw, xml = load_catalog_bytes(
        "https://go.microsoft.com/fwlink/?LinkId=1", fetch=fetch, extract_cab=extract
    )
    assert calls == ["https://go.microsoft.com/fwlink/?LinkId=1"]
    assert raw.startswith(b"MSCF")
    assert xml == products_xml


def test_load_catalog_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01garbage")
    with pytest.raises(FetchwinError, match="neither a CAB"):
        load_catalog_bytes(str(p), fetch=lambda u: b"", extract_cab=lambda c, o: c)
    with pytest.raises(FetchwinError, match="not found"):
        load_catalog_bytes(
            str(tmp_path / "missing.cab"), fetch=lambda u: b"", extract_cab=lambda c, o: c
        )


def test_find_products_xml_missing(tmp_path: Path) -> None:
    with pytest.raises(FetchwinError, match=r"products\.xml not found"):
        find_products_xml(tmp_path)
