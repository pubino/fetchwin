from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

import pytest

from fetchwin.convert import (
    FAT32_MAX,
    convert,
    iso_argv,
    parse_wim_info,
    sanitize_label,
    select_editions,
    validate_esd_layout,
)
from fetchwin.errors import FetchwinError
from fetchwin.tools import Toolchain

INFO = """WIM Information:
----------------
Path:           x.esd
Image Count:    5
Compression:    LZMS

Available Images:
-----------------
Index:                  1
Name:                   Windows Setup Media
Description:            Windows Setup Media

Index:                  2
Name:                   Microsoft Windows PE (arm64)
Edition ID:             WindowsPE

Index:                  3
Name:                   Microsoft Windows Setup (arm64)
Edition ID:             WindowsPE

Index:                  4
Name:                   Windows 11 Education
Edition ID:             Education

Index:                  5
Name:                   Windows 11 Enterprise
Edition ID:             Enterprise
"""

CDR = Toolchain("wimlib-imagex", "mkisofs", "cdrtools", "bsdtar", None)
GEN = Toolchain("wimlib-imagex", "genisoimage", "genisoimage", "bsdtar", None)


def test_parse_wim_info() -> None:
    imgs = parse_wim_info(INFO)
    assert [i.index for i in imgs] == [1, 2, 3, 4, 5]
    assert imgs[4].edition_id == "Enterprise"
    assert imgs[0].name == "Windows Setup Media"


def test_parse_wim_info_garbage() -> None:
    with pytest.raises(FetchwinError):
        parse_wim_info("nothing useful")


def test_layout_validation_rejects_non_installer_wim() -> None:
    with pytest.raises(FetchwinError, match="expected installer layout"):
        validate_esd_layout(parse_wim_info(INFO.replace("Windows PE", "Something")))
    with pytest.raises(FetchwinError, match="expected installer layout"):
        validate_esd_layout(parse_wim_info(INFO)[:3])


def test_select_editions() -> None:
    imgs = parse_wim_info(INFO)
    assert [i.index for i in select_editions(imgs, [])] == [4, 5]
    assert [i.index for i in select_editions(imgs, ["enterprise"])] == [5]
    assert [i.index for i in select_editions(imgs, ["Windows 11 Education"])] == [4]
    with pytest.raises(FetchwinError, match=r"not in ESD: professional; have: Education"):
        select_editions(imgs, ["Professional"])


def test_iso_argv_arm64_cdrtools_is_uefi_only() -> None:
    argv = iso_argv(CDR, arch="arm64", stage=Path("/s"), iso=Path("/o.iso"), label="L")
    assert "etfsboot" not in " ".join(argv)
    assert argv[argv.index("-eltorito-platform") + 1] == "efi"
    assert argv[argv.index("-b") + 1] == "efi/microsoft/boot/efisys.bin"
    assert {"-udf", "-iso-level"} <= set(argv)
    assert argv[-2:] == ["/o.iso", "/s"]


def test_iso_argv_x64_has_bios_then_uefi() -> None:
    for tc, efi_flag in ((CDR, "-b"), (GEN, "-e")):
        argv = iso_argv(tc, arch="x64", stage=Path("/s"), iso=Path("/o.iso"), label="L")
        joined = " ".join(argv)
        assert "-b boot/etfsboot.com -no-emul-boot -boot-load-size 8 -eltorito-alt-boot" in joined
        assert f"{efi_flag} efi/microsoft/boot/efisys.bin -no-emul-boot" in joined
        assert joined.index("etfsboot") < joined.index("efisys")


def test_iso_argv_genisoimage_allows_large_files() -> None:
    argv = iso_argv(GEN, arch="arm64", stage=Path("/s"), iso=Path("/o.iso"), label="L")
    assert "-allow-limited-size" in argv
    assert "-eltorito-platform" not in argv


def test_sanitize_label() -> None:
    assert sanitize_label("26100.4349.250607-1500") == "26100.4349.250607-1500"
    assert sanitize_label("a b/c;rm") == "a_b_c_rm"
    assert len(sanitize_label("x" * 50)) == 32


class FakeWim:
    """Simulates wimlib-imagex and mkisofs by creating the files they would."""

    def __init__(self, install_size: int = 10) -> None:
        self.calls: list[list[str]] = []
        self.install_size = install_size

    def __call__(self, argv: Sequence[str]) -> str:
        argv = list(argv)
        self.calls.append(argv)
        tool, verb = Path(argv[0]).name, argv[1]
        if tool == "wimlib-imagex" and verb == "info":
            return INFO
        if verb == "apply":
            stage = Path(argv[4])
            (stage / "efi/microsoft/boot").mkdir(parents=True)
            (stage / "efi/microsoft/boot/efisys.bin").write_bytes(b"e")
            (stage / "boot").mkdir()
            (stage / "boot/etfsboot.com").write_bytes(b"b")
        elif verb == "export":
            dest = Path(argv[4])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("ab") as f:
                if dest.name == "install.wim":
                    f.truncate(self.install_size)
                else:
                    f.write(b"x")
        elif verb == "split":
            Path(argv[3]).write_bytes(b"swm")
        elif tool in ("mkisofs", "genisoimage"):
            Path(argv[argv.index("-o") + 1]).write_bytes(b"ISO")
        return ""


def test_convert_runs_expected_pipeline(tmp_path: Path) -> None:
    fake = FakeWim()
    iso = tmp_path / "out" / "w.iso"
    res = convert(
        tmp_path / "x.esd",
        iso,
        tools=CDR,
        arch="x64",
        label="26100.1",
        work_dir=tmp_path / "w",
        editions=["Enterprise"],
        run=fake,
        log=io.StringIO(),
    )
    assert iso.read_bytes() == b"ISO"
    assert not iso.with_name("w.iso.tmp").exists()
    assert [i.edition_id for i in res.install_images] == ["Enterprise"]
    verbs = [c[1] for c in fake.calls if c[0] == "wimlib-imagex"]
    assert verbs == ["info", "apply", "export", "export", "export"]
    exports = [c for c in fake.calls if c[1] == "export"]
    assert exports[0][2:4] == [str(tmp_path / "x.esd"), "2"]
    assert "--boot" in exports[1] and exports[1][3] == "3"
    assert exports[2][3] == "5" and "--compress=LZMS" in exports[2]
    assert res.split is False


def test_convert_splits_only_when_needed(tmp_path: Path) -> None:
    small = FakeWim(install_size=10)
    convert(
        tmp_path / "x.esd",
        tmp_path / "a.iso",
        tools=CDR,
        arch="arm64",
        label="L",
        work_dir=tmp_path / "w1",
        split_wim=True,
        run=small,
        log=io.StringIO(),
    )
    assert not any(c[1] == "split" for c in small.calls)

    big = FakeWim(install_size=FAT32_MAX + 1)  # sparse file, no real disk use
    res = convert(
        tmp_path / "x.esd",
        tmp_path / "b.iso",
        tools=CDR,
        arch="arm64",
        label="L",
        work_dir=tmp_path / "w2",
        split_wim=True,
        run=big,
        log=io.StringIO(),
    )
    assert res.split is True
    split = next(c for c in big.calls if c[1] == "split")
    assert split[3].endswith("install.swm") and split[4] == "3800"
    assert not (tmp_path / "w2/iso/sources/install.wim").exists()


def test_convert_x64_requires_bios_loader(tmp_path: Path) -> None:
    fake = FakeWim()
    orig = fake.__call__

    def no_bios(argv: Sequence[str]) -> str:
        out = orig(argv)
        if argv[1] == "apply":
            (Path(argv[4]) / "boot/etfsboot.com").unlink()
        return out

    with pytest.raises(FetchwinError, match="etfsboot"):
        convert(
            tmp_path / "x.esd",
            tmp_path / "a.iso",
            tools=CDR,
            arch="x64",
            label="L",
            work_dir=tmp_path / "w",
            run=no_bios,
            log=io.StringIO(),
        )


def test_convert_fails_if_builder_produces_nothing(tmp_path: Path) -> None:
    fake = FakeWim()
    orig = fake.__call__

    def silent_builder(argv: Sequence[str]) -> str:
        if argv[0] == "mkisofs":
            return ""  # "succeeds" but writes nothing: must not be treated as success
        return orig(argv)

    with pytest.raises(FetchwinError, match="produced no ISO"):
        convert(
            tmp_path / "x.esd",
            tmp_path / "a.iso",
            tools=CDR,
            arch="arm64",
            label="L",
            work_dir=tmp_path / "w",
            run=silent_builder,
            log=io.StringIO(),
        )
