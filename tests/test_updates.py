from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from fetchwin import cli, ums, updates
from fetchwin.errors import FetchwinError
from fetchwin.tools import Toolchain
from tests.conftest import FIXTURES
from tests.test_convert import FakeWim

HTML = (FIXTURES / "release-info.html").read_bytes()
MSU_A = b"msu-checkpoint" * 10
MSU_B = b"msu-cumulative" * 20


def _loc(name: str, body: bytes, url: str | None = None) -> dict[str, Any]:
    return {
        "FileName": name,
        "Size": len(body),
        "Digest": base64.b64encode(hashlib.sha256(body).digest()).decode(),
        "Url": url or f"http://tlu.dl.delivery.mp.microsoft.com/files/{name}?P1=1",
    }


def _offer(*locs: dict[str, Any]) -> bytes:
    return json.dumps(
        [{"UpdateIds": ["15d51712-b97d-4190-95ad-a3925fba8311"], "FileLocations": list(locs)}]
    ).encode()


OFFER = _offer(
    _loc("Windows11.0-KB5132061-arm64.msu", MSU_B),
    _loc("Windows11.0-KB5043080-arm64.msu", MSU_A),
    _loc("Windows11.0-KB5132061-x64.msu", b"wrong arch"),
    _loc("SSU-26100.9971-arm64.psf", b"psf"),
    _loc("Microsoft-Windows-Foo-FoD-Package~31bf3856ad364e35~arm64~en-us~.cab", b"cab"),
)


def test_parse_release_info_summary_table() -> None:
    rows = updates.parse_release_info(HTML.decode())
    assert [(r.version, r.latest_build) for r in rows] == [
        ("26H2", "26300.9974"),
        ("25H2", "26200.9974"),
        ("24H2", "26100.9974"),
    ]
    assert rows[0].latest_update == "2026-11 B"
    assert updates.latest_for_base("26300.9711", rows).version == "26H2"
    with pytest.raises(FetchwinError, match="no release for build 22631"):
        updates.latest_for_base("22631.1", rows)


def test_parse_release_info_rejects_changed_page() -> None:
    with pytest.raises(FetchwinError, match="no recognizable"):
        updates.parse_release_info("<table><tr><th>Something else</th></tr></table>")


def test_offer_request_shape() -> None:
    body = json.loads(updates.offer_request("x64", "26300.9974"))
    assert body["Products"].startswith("PN=Client.OS.rs2.amd64&")
    assert body["Products"].endswith("V=10.0.26300.9974")
    assert "OSArchitecture=AMD64" in body["DeviceAttributes"]
    assert "DUInternal" not in body["DeviceAttributes"]


def test_msu_packages_filters_arch_and_orders_by_kb() -> None:
    uid, pkgs = updates.msu_packages(OFFER, "arm64")
    assert uid.startswith("15d51712")
    assert [p.filename for p in pkgs] == [
        "Windows11.0-KB5043080-arm64.msu",
        "Windows11.0-KB5132061-arm64.msu",
    ]
    assert pkgs[1].kb == "KB5132061"
    assert pkgs[1].sha256 == hashlib.sha256(MSU_B).hexdigest()


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"[]", "no update offer"),
        (b"nope", "invalid JSON"),
        (_offer(_loc("SSU-1-arm64.psf", b"x")), "no arm64 .msu"),
        (
            _offer(_loc("Windows11.0-KB5132061-arm64.msu", b"x", url="http://evil.example/x")),
            "non-Microsoft URL",
        ),
    ],
)
def test_msu_packages_rejects(data: bytes, match: str) -> None:
    with pytest.raises(FetchwinError, match=match):
        updates.msu_packages(data, "arm64")


def _fakes(tmp_path: Path, offer: bytes = OFFER) -> dict[str, Any]:
    bodies = {"Windows11.0-KB5043080-arm64.msu": MSU_A, "Windows11.0-KB5132061-arm64.msu": MSU_B}
    posted: list[bytes] = []
    downloaded: list[str] = []

    def post(url: str, body: bytes) -> bytes:
        posted.append(body)
        return offer

    def get_pinned(url: str) -> bytes:
        return json.dumps(
            {"LocalizedProperties": [{"Language": "en", "Title": "2026-11 Security Update"}]}
        ).encode()

    def dl(url: str, dest: Path, *, size: int, algo: str, digest: str, log: Any) -> Path:
        body = bodies[dest.name]
        assert hashlib.sha256(body).hexdigest() == digest and len(body) == size
        dest.write_bytes(body)
        downloaded.append(dest.name)
        return dest

    return {
        "kw": {
            "get_release_page": lambda url: HTML,
            "post": post,
            "get_pinned": get_pinned,
            "download": dl,
            "log": io.StringIO(),
        },
        "posted": posted,
        "downloaded": downloaded,
    }


def test_fetch_updates_downloads_in_order(tmp_path: Path) -> None:
    f = _fakes(tmp_path)
    res = updates.fetch_updates(
        base_label="26300.9711.261011-1601", arch="arm64", dest=tmp_path / "u", **f["kw"]
    )
    assert res.status == "downloaded"
    assert res.target_build == "26300.9974" and res.base_build == "26300.9711"
    assert res.title == "2026-11 Security Update"
    assert f["downloaded"] == [
        "Windows11.0-KB5043080-arm64.msu",
        "Windows11.0-KB5132061-arm64.msu",
    ]
    assert res.target == "Windows11.0-KB5132061-arm64.msu"
    assert b"V=10.0.26300.9974" in f["posted"][0]


def test_fetch_updates_current_media_downloads_nothing(tmp_path: Path) -> None:
    f = _fakes(tmp_path)
    res = updates.fetch_updates(
        base_label="26300.9974.261109-0000", arch="arm64", dest=tmp_path / "u", **f["kw"]
    )
    assert res.status == "current" and res.packages == []
    assert f["posted"] == [] and f["downloaded"] == []
    assert not (tmp_path / "u").exists()


def _pkg(kb: str) -> updates.UpdatePackage:
    return updates.UpdatePackage(f"Windows11.0-{kb}-arm64.msu", kb, 1, "0" * 64, "u")


def test_target_is_the_kb_the_update_title_names() -> None:
    # Regression (VM run 2026-11-12): installing every package in KB order sent the 24H2
    # checkpoint KB5043080 through DISM on 26300, which failed (0x80070228). Installing only
    # the target, with the checkpoints in the same folder, reached the target build.
    pkgs = [_pkg("KB5043080"), _pkg("KB5132060"), _pkg("KB5132061"), _pkg("KB5199999")]
    title = "2026-11 Security Update (KB5132061) (26300.9974)"
    assert updates.pick_target(pkgs, title).kb == "KB5132061"


def test_target_falls_back_to_the_highest_kb() -> None:
    pkgs = [_pkg("KB5043080"), _pkg("KB5132061")]
    assert updates.pick_target(pkgs, "").kb == "KB5132061"
    assert updates.pick_target(pkgs, "Update (KB5000001)").kb == "KB5132061"


def test_installer_script_is_packaged_ascii_powershell() -> None:
    body = updates.installer_script()
    assert body.isascii()
    assert b"function Invoke-FetchwinUpdate" in body
    assert b"exit (Invoke-FetchwinUpdate -Path $Path" in body


def test_fetch_updates_current_media_ships_no_installer(tmp_path: Path) -> None:
    f = _fakes(tmp_path)
    res = updates.fetch_updates(
        base_label="26300.9974.261109-0000", arch="arm64", dest=tmp_path / "u", **f["kw"]
    )
    assert res.installer == ""


def test_integrate_is_not_available_yet(tmp_path: Path) -> None:
    res = updates.UpdatesResult(status="downloaded", base_build="1", target_build="2")
    with pytest.raises(updates.IntegrationUnavailable):
        updates.integrate(res, tmp_path / "x.iso")


# ---------- CLI ----------

TOOLS = Toolchain("wimlib-imagex", "mkisofs", "cdrtools", "bsdtar", None)
ESD = "26300.9711.261011-1601.ge_release_CLIENTCONSUMER_RET_A64FRE_en-us.esd"


def _patch_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **over: Any) -> dict[str, Any]:
    f = _fakes(tmp_path)
    monkeypatch.setattr(cli, "http_get", over.get("http_get", f["kw"]["get_release_page"]))
    monkeypatch.setattr(ums, "https_post", over.get("post", f["kw"]["post"]))
    monkeypatch.setattr(updates, "https_get_pinned", f["kw"]["get_pinned"])
    monkeypatch.setattr(cli, "download", f["kw"]["download"])
    return f


def _build(tmp_path: Path, *extra: str) -> tuple[int, str, str, Path]:
    esd = tmp_path / ESD
    esd.write_bytes(b"x")
    out, log = io.StringIO(), io.StringIO()
    code = cli.main(
        ["build", str(esd), "--out", str(tmp_path / "out"), *extra],
        out=out,
        log=log,
        tools=TOOLS,
        run=FakeWim(),
    )
    iso = tmp_path / "out" / ESD.replace(".esd", ".iso")
    return code, out.getvalue(), log.getvalue(), iso


def test_cli_updates_download_leaves_packages_alongside_iso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cli(monkeypatch, tmp_path)
    code, _, err, iso = _build(tmp_path, "--updates", "download")
    assert code == 0, err
    udir = iso.with_name(iso.stem + ".updates")
    assert sorted(p.name for p in udir.iterdir()) == [
        "Install-FetchwinUpdates.ps1",
        "Windows11.0-KB5043080-arm64.msu",
        "Windows11.0-KB5132061-arm64.msu",
        "updates.json",
    ]
    um = json.loads((udir / "updates.json").read_text())
    assert um["target"] == "Windows11.0-KB5132061-arm64.msu"
    assert "install_order" not in um
    assert all("url" not in p for p in um["packages"])
    assert um["installer"] == "Install-FetchwinUpdates.ps1"
    assert (udir / "Install-FetchwinUpdates.ps1").read_bytes() == updates.installer_script()
    manifest = json.loads(iso.with_name(iso.name + ".json").read_text())
    assert manifest["updates"]["status"] == "downloaded"
    assert "install Windows11.0-KB5132061-arm64.msu (DISM takes the checkpoints" in err


def test_cli_updates_integrate_falls_back_to_leaving_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cli(monkeypatch, tmp_path)
    code, _, err, iso = _build(tmp_path, "--updates", "integrate")
    assert code == 0, err
    assert "WARNING: update integration failed" in err
    manifest = json.loads(iso.with_name(iso.name + ".json").read_text())
    assert manifest["updates"]["status"] == "downloaded"
    assert "not available yet" in manifest["updates"]["note"]
    assert (iso.with_name(iso.stem + ".updates") / "Windows11.0-KB5132061-arm64.msu").is_file()


def test_cli_updates_failure_keeps_iso_and_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(url: str) -> bytes:
        raise OSError("release page unreachable")

    _patch_cli(monkeypatch, tmp_path, http_get=down)
    code, out, err, iso = _build(tmp_path, "--updates", "download")
    assert code == cli.EXIT_UPDATES_FAILED
    assert iso.is_file() and out.strip() == str(iso)
    assert "WARNING: updates step failed (release page unreachable)" in err
    manifest = json.loads(iso.with_name(iso.name + ".json").read_text())
    assert manifest["updates"] == {"status": "failed", "error": "release page unreachable"}


def test_cli_updates_none_is_default_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("no network for --updates none")

    monkeypatch.setattr(ums, "https_post", boom)
    code, _, err, iso = _build(tmp_path)
    assert code == 0, err
    assert "updates" not in json.loads(iso.with_name(iso.name + ".json").read_text())
    assert not iso.with_name(iso.stem + ".updates").exists()


def test_cli_updates_env_validated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FETCHWIN_UPDATES", "sometimes")
    code, _, err, _ = _build(tmp_path)
    assert code == 1 and "FETCHWIN_UPDATES='sometimes' is invalid" in err
