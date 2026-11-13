from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from fetchwin import cli
from fetchwin.tools import Toolchain
from tests.conftest import FIXTURES
from tests.test_convert import FakeWim

TOOLS = Toolchain("wimlib-imagex", "mkisofs", "cdrtools", "bsdtar", None)


def run(argv: list[str], **kw: Any) -> tuple[int, str, str]:
    out, log = io.StringIO(), io.StringIO()
    code = cli.main(argv, out=out, log=log, tools=kw.pop("tools", TOOLS), **kw)
    return code, out.getvalue(), log.getvalue()


def cat() -> list[str]:
    return ["--catalog", str(FIXTURES / "products.xml")]


def test_list_marks_newest() -> None:
    code, out, _ = run(["list", *cat(), "--arch", "arm64"])
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("* 26100.4349.250607-1500")
    assert lines[0].startswith("  22631.2861")


def test_list_json_all_langs() -> None:
    code, out, _ = run(["list", *cat(), "--all-langs", "--json"])
    assert code == 0
    langs = {e["language_code"] for e in json.loads(out)["entries"]}
    assert langs == {"en-us", "de-de"}


def test_list_no_match_is_an_error_not_silence() -> None:
    code, out, err = run(["list", *cat(), "--arch", "x64", "--channel", "consumer"])
    assert code == 1
    assert out == ""
    assert "no consumer x64 entries" in err


def test_fetch_dry_run_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("dry run must not download")

    monkeypatch.setattr(cli, "download", boom)
    code, out, _ = run(
        [
            "fetch",
            *cat(),
            "--channel",
            "business",
            "--arch",
            "x64",
            "--out",
            str(tmp_path),
            "--dry-run",
            "--json",
        ]
    )
    assert code == 0
    plan = json.loads(out)
    assert plan["action"] == "build"
    assert plan["esd"]["sha256"] == "2" * 64
    assert plan["iso"].endswith("CLIENTBUSINESS_VOL_x64FRE_en-us.iso")
    assert list(tmp_path.iterdir()) == []


def test_env_config_is_validated_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCHWIN_ARCH", "sparc")
    code, _, err = run(["list", *cat()])
    assert code == 1
    assert "FETCHWIN_ARCH='sparc' is invalid" in err


def test_env_config_supplies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCHWIN_CHANNEL", "business")
    monkeypatch.setenv("FETCHWIN_ARCH", "x64")
    code, out, _ = run(["fetch", *cat(), "--dry-run"])
    assert code == 0
    assert "26200.6584.250915-1905 x64 business en-us" in out


def _fake_catalog(tmp_path: Path, payload: bytes) -> Path:
    name = "26100.1.250101-0000.ge_release_CLIENTBUSINESS_VOL_x64FRE_en-us.esd"
    xml = (FIXTURES / "products.xml").read_text()
    entry = (
        f"<File><FileName>{name}</FileName><LanguageCode>en-us</LanguageCode>"
        f"<Edition>Enterprise</Edition><Architecture>x64</Architecture>"
        f"<Size>{len(payload)}</Size>"
        f"<Sha1>{hashlib.sha1(payload, usedforsecurity=False).hexdigest()}</Sha1>"
        f"<FilePath>http://dl.delivery.mp.microsoft.com/f/{name}</FilePath></File>"
    )
    xml = xml.replace("<!-- business x64", entry + "<!-- business x64")
    # drop the newer 26200 x64 entry so ours is the latest
    start = xml.index("<!-- business x64")
    xml = xml[:start] + xml[xml.index("<!-- another language") :]
    p = tmp_path / "products.xml"
    p.write_text(xml)
    return p


def test_fetch_end_to_end_with_fakes_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"fake esd payload"
    catalog = _fake_catalog(tmp_path, payload)
    out_dir = tmp_path / "out"
    downloads: list[str] = []

    def fake_download(url: str, dest: Path, **kw: Any) -> Path:
        downloads.append(url)
        dest.write_bytes(payload)
        return dest

    monkeypatch.setattr(cli, "download", fake_download)
    monkeypatch.setattr(cli, "_free_bytes", lambda p: 10**15)
    argv = [
        "fetch",
        "--catalog",
        str(catalog),
        "--arch",
        "x64",
        "--channel",
        "business",
        "--edition",
        "Enterprise",
        "--out",
        str(out_dir),
    ]

    code, out, err = run(argv, run=FakeWim())
    assert code == 0, err
    iso = Path(out.strip())
    assert iso.read_bytes() == b"ISO"
    manifest = json.loads(iso.with_name(iso.name + ".json").read_text())
    assert manifest["options"] == {"arch": "x64", "editions": ["enterprise"], "split_wim": False}
    assert manifest["install_images"][0]["edition_id"] == "Enterprise"
    assert manifest["iso"]["sha256"] == hashlib.sha256(b"ISO").hexdigest()
    assert (
        iso.with_name(iso.name + ".sha256").read_text()
        == f"{manifest['iso']['sha256']}  {iso.name}\n"
    )
    assert not (out_dir / ".fetchwin-work").exists()
    assert not list(out_dir.glob("*.esd")), "ESD should be deleted without --keep-esd"
    assert len(downloads) == 1

    # Second run: same ESD + options -> skip, no download, no rebuild.
    fake2 = FakeWim()
    code, _, err = run(argv, run=fake2)
    assert code == 0
    assert "Up to date" in err
    assert len(downloads) == 1 and fake2.calls == []

    # Different options -> rebuild.
    fake3 = FakeWim()
    code, _, _ = run([*argv, "--split-wim"], run=fake3)
    assert code == 0 and fake3.calls
    assert len(downloads) == 2


def test_fetch_refuses_when_disk_too_small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_free_bytes", lambda p: 1)
    code, _, err = run(["fetch", *cat(), "--out", str(tmp_path)])
    assert code == 1
    assert "GiB free" in err


def test_failed_build_keeps_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    esd = tmp_path / "26100.1.250101-0000.ge_release_CLIENTCONSUMER_RET_A64FRE_en-us.esd"
    esd.write_bytes(b"x")

    def failing(argv: Any) -> str:
        from fetchwin.errors import FetchwinError

        raise FetchwinError("wimlib exploded")

    code, _, err = run(["build", str(esd), "--out", str(tmp_path)], run=failing)
    assert code == 1
    assert "wimlib exploded" in err
    assert "scratch directory kept" in err
    assert (tmp_path / ".fetchwin-work").is_dir()


def test_build_infers_arch_and_rejects_unknown(tmp_path: Path) -> None:
    esd = tmp_path / "mystery.esd"
    esd.write_bytes(b"x")
    code, _, err = run(["build", str(esd), "--out", str(tmp_path)], run=FakeWim())
    assert code == 1
    assert "cannot infer arch" in err


def test_missing_tools_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    out, log = io.StringIO(), io.StringIO()
    code = cli.main(["fetch", *cat()], out=out, log=log)
    assert code == 1
    assert "missing required tools" in log.getvalue()
    assert "brew bundle" in log.getvalue()
