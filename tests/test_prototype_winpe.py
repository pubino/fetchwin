"""Unit tests for the parked WinPE/DISM prototype's pure parts (prototypes/winpe-dism)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
import qmp_type
import winpe_worker as w


def test_startnet_runs_the_job_and_always_shuts_down() -> None:
    s = w.startnet_script()
    lines = s.split("\r\n")
    assert "\n" not in s.replace("\r\n", "")  # CRLF only, for cmd.exe
    assert lines[:2] == ["@echo off", "wpeinit"]
    run = next(ln for ln in lines if "job.cmd >" in ln)
    # cmd /c: an error in the job must not abort startnet before the shutdown (regression).
    assert "cmd /c %JOB%\\job.cmd > %OUT%\\job.log 2>&1" in run
    assert lines.index("wpeutil shutdown") > lines.index(run)
    assert any("dism.log" in ln for ln in lines) and any("done.txt" in ln for ln in lines)
    assert "findstr" not in s  # not in WinPE


def test_fat32_mbr_has_one_lba_partition_from_1_mib() -> None:
    size = 2 * 1024**3
    mbr, nsec = w.fat32_mbr(size)
    assert len(mbr) == 512 and mbr[510:] == b"\x55\xaa"
    boot, ptype = mbr[446], mbr[450]
    start, count = struct.unpack_from("<II", mbr, 454)
    assert (boot, ptype, start, count) == (0, 0x0C, 2048, nsec)
    assert (start + count) * 512 == size
    assert mbr[462:510] == bytes(48)  # no other partitions


def test_fat32_mbr_rejects_a_disk_too_small() -> None:
    with pytest.raises(ValueError, match="too small"):
        w.fat32_mbr(512 * 2048)


def test_job_iso_boots_without_the_press_any_key_prompt() -> None:
    argv = w.mkisofs_argv(Path("stage"), Path("job.iso"))
    assert argv[argv.index("-b") + 1] == "efi/microsoft/boot/efisys_noprompt.bin"
    assert "-udf" in argv and argv[-2:] == ["job.iso", "stage"]


@pytest.mark.parametrize(
    ("arch", "binary", "needles"),
    [
        ("arm64", "qemu-system-aarch64", ["hvf", "virt,highmem=on", "edk2-aarch64-code.fd",
                                          "usb-storage,drive=cd,removable=on,bootindex=0"]),
        ("x64", "qemu-system-x86_64", ["tcg,thread=multi", "q35", "edk2-x86_64-code.fd",
                                       "ide-cd,drive=cd,bus=ide.2,bootindex=0"]),
    ],
)  # fmt: skip
def test_qemu_argv_per_arch(arch: str, binary: str, needles: list[str]) -> None:
    argv = w.qemu_argv(
        arch,
        share=Path("/share"),
        vars_fd=Path("vars.fd"),
        iso=Path("job.iso"),
        out_img=Path("out.img"),
        scratch=Path("scratch.img"),
    )
    assert argv[0] == binary
    joined = " ".join(argv)
    for n in needles:
        assert n in joined, n
    # Headless, offline, and it exits when WinPE shuts down.
    for n in ("-display none", "-nic none", "-no-reboot", "unix:qmp.sock,server,nowait"):
        assert n in joined, n
    assert "serial=fwout" in joined and "serial=fwscr" in joined


def test_qemu_argv_rejects_unknown_arch() -> None:
    with pytest.raises(ValueError, match="unsupported arch"):
        w.qemu_argv(
            "riscv",
            share=Path("s"),
            vars_fd=Path("v"),
            iso=Path("i"),
            out_img=Path("o"),
            scratch=Path("x"),
        )


def test_vars_template_per_arch() -> None:
    assert w.vars_template("x64") == "edk2-i386-vars.fd"
    assert w.vars_template("arm64") == "edk2-arm-vars.fd"


def test_main_rejects_missing_inputs_before_touching_anything(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as e:
        w.main([str(tmp_path / "no.iso"), str(tmp_path / "work"), str(tmp_path / "no.cmd")])
    assert e.value.code == 2
    assert not (tmp_path / "work").exists()


def test_qmp_sendkeys_maps_a_windows_command_line() -> None:
    assert qmp_type.sendkeys('C:\\a b>"x"\n') == [
        "shift-c", "shift-semicolon", "backslash", "a", "spc", "b", "shift-dot",
        "shift-apostrophe", "x", "shift-apostrophe", "ret",
    ]  # fmt: skip


def test_qmp_sendkeys_refuses_what_it_cannot_type() -> None:
    with pytest.raises(ValueError, match="no key"):
        qmp_type.sendkeys("é")
