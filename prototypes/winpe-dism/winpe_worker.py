"""PROTOTYPE (parked): run DISM inside WinPE, headless in QEMU on a Mac, with no Windows host.

Boots WinPE (boot.wim index 1 from Windows install media) from a no-prompt UDF job ISO,
runs a job script in it, and reads the results back from a FAT32 disk with mtools.
Offline servicing (`dism /Image:... /Add-Package`) does not work this way yet; see
README.md in this directory and the GitHub issue on offline integration.

Usage (macOS, Homebrew qemu, wimlib, cdrtools and mtools):

    python3 winpe_worker.py SOURCE_ISO WORK_DIR JOB.cmd [--timeout SEC] [EXTRA_FILE ...]

EXTRA_FILEs are hard-linked into the job folder (%JOB% in the guest); job.log, dism.log
and done.txt come back from the output disk (%OUT%).

Environment: ARCH=arm64|x64 (default arm64; x64 runs under TCG, slowly), CPUS, MEM_MB,
OUT_GB, SCR_GB, SHOT_EVERY (seconds between screenshots), FETCH_OUT (extra output files to
copy back), QEMU_SHARE (default /opt/homebrew/share/qemu).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

PART_START = 2048  # sectors: the FAT32 partition starts at 1 MiB
SECTOR = 512
DRIVES = "C D E F G H I J K L M N O P Q R S T U V W Y Z"


def startnet_script() -> str:
    """WinPE's startnet.cmd: find the job and output disks, run the job, always shut down."""
    lines = [
        "@echo off",
        "wpeinit",
        f"for %%d in ({DRIVES}) do if exist %%d:\\fetchwin-job\\job.cmd set JOB=%%d:\\fetchwin-job",
        f"for %%d in ({DRIVES}) do if exist %%d:\\fetchwin-out.marker set OUT=%%d:",
        # cmd /c: an error inside the job must not end startnet before the shutdown.
        "if defined JOB if defined OUT cmd /c %JOB%\\job.cmd > %OUT%\\job.log 2>&1",
        "set RC=%errorlevel%",
        "if defined OUT copy /y X:\\Windows\\Logs\\DISM\\dism.log %OUT%\\dism.log > nul",
        "if defined OUT echo %RC% > %OUT%\\done.txt",
        "wpeutil shutdown",
        "",
    ]
    return "\r\n".join(lines)


def fat32_mbr(size: int) -> tuple[bytes, int]:
    """An MBR with one FAT32 (LBA) partition from 1 MiB to the end; returns (mbr, sectors)."""
    nsec = size // SECTOR - PART_START
    if nsec <= 0:
        raise ValueError(f"disk of {size} bytes is too small")
    mbr = bytearray(SECTOR)
    chs = b"\xfe\xff\xff"
    mbr[446:462] = struct.pack("<B3sB3sII", 0x00, chs, 0x0C, chs, PART_START, nsec)
    struct.pack_into("<I", mbr, 440, 0x46574F55)  # disk signature "UOWF"
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr), nsec


def mkisofs_argv(stage: Path, iso: Path) -> list[str]:
    """UDF job ISO that boots WinPE's EFI loader without the 'press any key' prompt."""
    return [
        "mkisofs", "-quiet", "-iso-level", "3", "-udf", "-hide", "*", "-V", "FWJOB",
        "-eltorito-platform", "efi", "-b", "efi/microsoft/boot/efisys_noprompt.bin",
        "-no-emul-boot", "-o", str(iso), str(stage),
    ]  # fmt: skip


def qemu_argv(
    arch: str, *, share: Path, vars_fd: Path, iso: Path, out_img: Path, scratch: Path,
    cpus: str = "4", mem_mb: str = "6144", qmp: str = "qmp.sock",
) -> list[str]:  # fmt: skip
    """QEMU command line: arm64 under HVF (virt, USB CD), x64 under TCG (q35, IDE CD)."""
    disks = [
        "-drive", f"if=none,id=out,format=raw,file={out_img}",
        "-device", "nvme,drive=out,serial=fwout",
        "-drive", f"if=none,id=scr,format=raw,file={scratch}",
        "-device", "nvme,drive=scr,serial=fwscr",
        "-qmp", f"unix:{qmp},server,nowait", "-no-reboot",
    ]  # fmt: skip
    cd = f"if=none,id=cd,media=cdrom,readonly=on,file={iso}"
    if arch == "x64":
        return [
            "qemu-system-x86_64", "-M", "q35", "-accel", "tcg,thread=multi", "-cpu", "max",
            "-smp", cpus, "-m", mem_mb,
            "-drive", f"if=pflash,format=raw,readonly=on,file={share / 'edk2-x86_64-code.fd'}",
            "-drive", f"if=pflash,format=raw,file={vars_fd}",
            "-vga", "std", "-display", "none", "-nic", "none",
            "-drive", cd, "-device", "ide-cd,drive=cd,bus=ide.2,bootindex=0",
            *disks,
        ]  # fmt: skip
    if arch == "arm64":
        return [
            "qemu-system-aarch64", "-M", "virt,highmem=on", "-accel", "hvf", "-cpu", "host",
            "-smp", cpus, "-m", mem_mb,
            "-drive", f"if=pflash,format=raw,readonly=on,file={share / 'edk2-aarch64-code.fd'}",
            "-drive", f"if=pflash,format=raw,file={vars_fd}",
            "-device", "ramfb", "-display", "none", "-nic", "none",
            "-device", "qemu-xhci", "-device", "usb-kbd",
            "-drive", cd, "-device", "usb-storage,drive=cd,removable=on,bootindex=0",
            *disks,
        ]  # fmt: skip
    raise ValueError(f"unsupported arch {arch!r}")


def vars_template(arch: str) -> str:
    return "edk2-i386-vars.fd" if arch == "x64" else "edk2-arm-vars.fd"


# ---------- side effects below: not unit-tested (they need macOS, QEMU and install media) ----


def _run(*argv: object) -> None:
    subprocess.run([str(a) for a in argv], check=True)


def _stage_boot_files(src_iso: Path, stage: Path) -> None:
    out = subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-noverify", str(src_iso)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    mnt = Path(out.strip().splitlines()[-1].split("\t")[-1])
    try:
        for d in ("efi", "boot"):
            if (mnt / d).exists():
                shutil.copytree(mnt / d, stage / d)
        for f in ("bootmgr.efi", "bootmgr"):
            if (mnt / f).exists():
                shutil.copy2(mnt / f, stage / f)
        (stage / "sources").mkdir()
        _run(
            "wimlib-imagex", "export", mnt / "sources/boot.wim", "1",
            stage / "sources/boot.wim", "--boot",
        )  # fmt: skip
    finally:
        subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True, check=False)
    for p in stage.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)


def _screendump(qmp: Path, target: Path) -> None:
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(qmp))
            s.recv(4096)
            for c in (
                {"execute": "qmp_capabilities"},
                {"execute": "screendump", "arguments": {"filename": str(target)}},
            ):
                s.sendall(json.dumps(c).encode() + b"\n")
                time.sleep(0.5)
                s.recv(4096)
    except OSError as e:
        print(f"screendump failed: {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("source_iso", type=Path)
    ap.add_argument("work", type=Path)
    ap.add_argument("job", type=Path)
    ap.add_argument("extra", type=Path, nargs="*")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args(argv)
    arch = os.environ.get("ARCH", "arm64")
    for p in (args.source_iso, args.job, *args.extra):
        if not p.is_file():
            ap.error(f"not a file: {p}")

    work: Path = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    stage = work / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    _stage_boot_files(args.source_iso, stage)

    startnet = work / "startnet.cmd"
    startnet.write_text(startnet_script(), newline="")
    _run(
        "wimlib-imagex", "update", stage / "sources/boot.wim", "1",
        f"--command=add {startnet} /Windows/System32/startnet.cmd",
    )  # fmt: skip
    jobdir = stage / "fetchwin-job"
    jobdir.mkdir()
    (jobdir / "job.cmd").write_text(args.job.read_text().replace("\n", "\r\n"), newline="")
    for extra in args.extra:
        os.link(extra, jobdir / extra.name)

    iso = work / "job.iso"
    iso.unlink(missing_ok=True)
    _run(*mkisofs_argv(stage, iso))

    out_img = work / "out.img"
    size = int(os.environ.get("OUT_GB", "2")) * 1024**3
    with out_img.open("wb") as f:
        f.truncate(size)
    mbr, nsec = fat32_mbr(size)
    with out_img.open("r+b") as f:
        f.write(mbr)
    img = f"{out_img}@@{PART_START * SECTOR}"
    _run("mformat", "-i", img, "-F", "-T", nsec, "-h", "255", "-s", "63", "-H", PART_START,
         "-v", "FWOUT", "::")  # fmt: skip
    marker = work / "fetchwin-out.marker"
    marker.write_text("fetchwin")
    _run("mcopy", "-i", img, marker, "::fetchwin-out.marker")
    scratch = work / "scratch.img"
    with scratch.open("wb") as f:
        f.truncate(int(os.environ.get("SCR_GB", "8")) * 1024**3)

    share = Path(os.environ.get("QEMU_SHARE", "/opt/homebrew/share/qemu"))
    vars_fd = work / "vars.fd"
    shutil.copy(share / vars_template(arch), vars_fd)
    os.chdir(work)  # the QMP socket path must stay under the 104-byte AF_UNIX limit
    qmp = Path("qmp.sock")
    qmp.unlink(missing_ok=True)
    cmd = qemu_argv(
        arch, share=share, vars_fd=vars_fd, iso=iso, out_img=out_img, scratch=scratch,
        cpus=os.environ.get("CPUS", "4"), mem_mb=os.environ.get("MEM_MB", "6144"), qmp=str(qmp),
    )  # fmt: skip

    t0 = time.time()
    shots = 0
    shot_every = int(os.environ.get("SHOT_EVERY", "45"))
    with (work / "qemu.log").open("w") as qlog:
        proc = subprocess.Popen(cmd, stdout=qlog, stderr=subprocess.STDOUT)
        while proc.poll() is None:
            elapsed = time.time() - t0
            if elapsed > args.timeout:
                _screendump(qmp, work / "timeout.ppm")
                proc.kill()
                print("TIMEOUT")
                break
            if elapsed > shot_every * (shots + 1):
                shots += 1
                _screendump(qmp, work / f"shot{shots}.ppm")
            time.sleep(1)
    print(f"qemu exit={proc.returncode} after {time.time() - t0:.0f}s")

    for name in ("done.txt", "job.log"):
        r = subprocess.run(["mcopy", "-n", "-i", img, f"::{name}", "-"],
                           capture_output=True, check=False)  # fmt: skip
        print(f"--- {name} ---")
        sys.stdout.write(r.stdout.decode("utf-8", "replace") or f"(missing: {r.stderr.decode()})\n")
    for name in os.environ.get("FETCH_OUT", "dism.log").split():
        r = subprocess.run(["mcopy", "-n", "-i", img, f"::{name}", str(work / name)],
                           capture_output=True, check=False)  # fmt: skip
        print(f"fetched {name}: rc={r.returncode} {r.stderr.decode().strip()}")
    done = subprocess.run(["mcopy", "-n", "-i", img, "::done.txt", "-"],
                          capture_output=True, check=False)  # fmt: skip
    return 0 if done.stdout.strip() == b"0" else 1


if __name__ == "__main__":
    sys.exit(main())
