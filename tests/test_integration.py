"""End-to-end test with the real external tools (wimlib-imagex + mkisofs/genisoimage).

A tiny fake ESD with the same 5-image layout as Microsoft's is built with
wimlib, run through `fetchwin build`, and the resulting ISO's El Torito boot
catalog is parsed to confirm the boot entries.

These tests FAIL (they do not skip) when the tools are missing, so a green run
always means the real pipeline was exercised. Deselect explicitly with
`-m "not integration"` if you only want unit tests.
"""

from __future__ import annotations

import io
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from fetchwin import cli
from fetchwin.convert import parse_wim_info
from fetchwin.tools import discover

pytestmark = pytest.mark.integration

SECTOR = 2048


def _wim() -> str:
    path = shutil.which("wimlib-imagex")
    if not path:
        pytest.fail("wimlib-imagex not installed (brew bundle / apt-get install wimtools)")
    return path


def _make_esd(tmp: Path, arch_tag: str) -> Path:
    wim = _wim()
    esd = tmp / f"26100.1.250101-0000.ge_release_CLIENTBUSINESS_VOL_{arch_tag}FRE_en-us.esd"
    layout = [
        ("Windows Setup Media", None),
        ("Microsoft Windows PE", None),
        ("Microsoft Windows Setup", None),
        ("Windows 11 Education", "Education"),
        ("Windows 11 Enterprise", "Enterprise"),
    ]
    for i, (name, edition) in enumerate(layout, start=1):
        src = tmp / f"img{i}"
        src.mkdir()
        if i == 1:
            (src / "efi/microsoft/boot").mkdir(parents=True)
            (src / "efi/microsoft/boot/efisys.bin").write_bytes(b"\xeb" * (SECTOR * 4))
            (src / "boot").mkdir()
            (src / "boot/etfsboot.com").write_bytes(b"\xfa" * SECTOR)
            (src / "setup.exe").write_bytes(b"MZ")
        (src / f"marker{i}.txt").write_text(name)
        verb = "capture" if i == 1 else "append"
        subprocess.run(
            [wim, verb, str(src), str(esd), name, "--solid", "--compress=LZMS", "--quiet"],
            check=True,
        )
        if edition:
            subprocess.run(
                [wim, "info", str(esd), str(i), "--image-property", f"WINDOWS/EDITIONID={edition}"],
                check=True,
            )
    return esd


def _boot_entries(iso: Path) -> list[tuple[int, int]]:
    """Return [(platform_id, load_rba)] for each El Torito boot entry."""
    data = iso.read_bytes()
    brvd = data[17 * SECTOR : 18 * SECTOR]
    assert brvd[1:6] == b"CD001" and brvd[7:30].rstrip(b"\0") == b"EL TORITO SPECIFICATION"
    cat_lba = struct.unpack_from("<I", brvd, 0x47)[0]
    cat = data[cat_lba * SECTOR : cat_lba * SECTOR + SECTOR]
    assert cat[0] == 0x01 and cat[0x1E:0x20] == b"\x55\xaa", "bad validation entry"
    entries = [(cat[1], struct.unpack_from("<I", cat, 0x20 + 8)[0])]
    off = 0x40
    while off < SECTOR and cat[off] in (0x90, 0x91):
        platform, count = cat[off + 1], struct.unpack_from("<H", cat, off + 2)[0]
        for k in range(count):
            e = off + 32 * (k + 1)
            entries.append((platform, struct.unpack_from("<I", cat, e + 8)[0]))
        off += 32 * (count + 1)
    return entries


def _wim_guids_in(blob: bytes) -> set[bytes]:
    out, i = set(), blob.find(b"MSWIM\0\0\0")
    while i != -1:
        out.add(blob[i + 0x18 : i + 0x28])
        i = blob.find(b"MSWIM\0\0\0", i + 1)
    return out


@pytest.mark.parametrize("arch,tag", [("x64", "X64"), ("arm64", "A64")])
def test_build_real_tools(tmp_path: Path, arch: str, tag: str) -> None:
    tools = discover()
    (tmp_path / "src").mkdir()
    esd = _make_esd(tmp_path / "src", tag)
    out_dir = tmp_path / "out"
    out, log = io.StringIO(), io.StringIO()
    code = cli.main(
        [
            "build",
            str(esd),
            "--out",
            str(out_dir),
            "--edition",
            "Enterprise",
            "--keep-work",
            "--json",
        ],
        out=out,
        log=log,
        tools=tools,
    )
    assert code == 0, log.getvalue()
    manifest = json.loads(out.getvalue())
    iso = out_dir / manifest["iso"]["name"]
    assert iso.stat().st_size == manifest["iso"]["size"] > 0

    stage = out_dir / ".fetchwin-work" / "iso"
    install = stage / "sources" / "install.wim"
    boot = stage / "sources" / "boot.wim"
    wim = _wim()
    install_info = parse_wim_info(
        subprocess.run(
            [wim, "info", str(install)], check=True, capture_output=True, text=True
        ).stdout
    )
    assert [(i.name, i.edition_id) for i in install_info] == [
        ("Windows 11 Enterprise", "Enterprise")
    ]
    boot_text = subprocess.run(
        [wim, "info", str(boot)], check=True, capture_output=True, text=True
    ).stdout
    assert "Boot Index:     2" in boot_text
    assert [i.name for i in parse_wim_info(boot_text)] == [
        "Microsoft Windows PE",
        "Microsoft Windows Setup",
    ]

    # The WIMs actually landed inside the ISO.
    iso_bytes = iso.read_bytes()
    assert _wim_guids_in(install.read_bytes()) <= _wim_guids_in(iso_bytes)
    assert _wim_guids_in(boot.read_bytes()) <= _wim_guids_in(iso_bytes)
    # UDF present (needed for >4 GiB install.wim).
    assert (
        b"NSR02" in iso_bytes[16 * SECTOR : 64 * SECTOR]
        or b"NSR03" in iso_bytes[16 * SECTOR : 64 * SECTOR]
    )

    platforms = [p for p, _ in _boot_entries(iso)]
    if arch == "x64":
        assert platforms[0] == 0x00, "first entry must be BIOS (x86)"
        assert 0xEF in platforms[1:], "x64 media must also boot UEFI"
    else:
        assert 0xEF in platforms
        efi_rba = next(rba for p, rba in _boot_entries(iso) if p == 0xEF)
        assert iso_bytes[efi_rba * SECTOR : efi_rba * SECTOR + 4] == b"\xeb" * 4


def test_split_wim_from_solid_esd_real_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: real ESDs are solid LZMS, and wimlib cannot split solid WIMs.

    Force the split path by lowering the FAT32 threshold so the tiny fake image qualifies.
    """
    import fetchwin.convert as conv

    monkeypatch.setattr(conv, "FAT32_MAX", 0)
    monkeypatch.setattr(conv, "SWM_PART_MB", 1)
    tools = discover()
    (tmp_path / "src").mkdir()
    esd = _make_esd(tmp_path / "src", "X64")
    out_dir = tmp_path / "out"
    out, log = io.StringIO(), io.StringIO()
    code = cli.main(
        ["build", str(esd), "--out", str(out_dir), "--split-wim", "--keep-work", "--json"],
        out=out,
        log=log,
        tools=tools,
    )
    assert code == 0, log.getvalue()
    assert json.loads(out.getvalue())["install_wim_split"] is True
    sources = out_dir / ".fetchwin-work" / "iso" / "sources"
    assert not (sources / "install.wim").exists()
    swm = sources / "install.swm"
    assert swm.is_file()
    info = subprocess.run(
        [_wim(), "info", str(swm)], check=True, capture_output=True, text=True
    ).stdout
    assert [i.edition_id for i in parse_wim_info(info)] == ["Education", "Enterprise"]
