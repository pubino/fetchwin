"""Turn a Microsoft ESD into a bootable installer ISO.

ESD image layout (the same one the Media Creation Tool consumes):
  1   Windows Setup Media       -> applied as the ISO's file tree
  2   Microsoft Windows PE      -> sources/boot.wim image 1
  3   Microsoft Windows Setup   -> sources/boot.wim image 2 (bootable)
  4+  Windows editions          -> sources/install.wim (or install.swm parts)

The ISO is UDF (install.wim exceeds ISO9660's 4 GiB per-file limit) with
El Torito boot entries: UEFI for every arch, plus BIOS for x64.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from fetchwin.errors import FetchwinError
from fetchwin.tools import Toolchain

FAT32_MAX = 4 * 1024**3 - 1
SWM_PART_MB = 3800
EFI_BOOT = "efi/microsoft/boot/efisys.bin"
BIOS_BOOT = "boot/etfsboot.com"

Runner = Callable[[Sequence[str]], str]


def subprocess_runner(log: IO[str] = sys.stderr) -> Runner:
    def run(argv: Sequence[str]) -> str:
        r = subprocess.run(list(argv), capture_output=True, text=True, check=False)
        if r.returncode != 0:
            tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-15:])
            raise FetchwinError(f"command failed ({r.returncode}): {argv[0]} {argv[1]}\n{tail}")
        return r.stdout

    return run


@dataclass(frozen=True)
class WimImage:
    index: int
    name: str
    edition_id: str


def parse_wim_info(text: str) -> list[WimImage]:
    images: list[WimImage] = []
    for block in re.split(r"\n(?=Index:\s)", text):
        fields = dict(
            (k.strip(), v.strip())
            for k, _, v in (ln.partition(":") for ln in block.splitlines())
            if _
        )
        if "Index" in fields and fields["Index"].isdigit():
            images.append(
                WimImage(int(fields["Index"]), fields.get("Name", ""), fields.get("Edition ID", ""))
            )
    if not images:
        raise FetchwinError("could not read image list from wimlib-imagex info output")
    return images


def validate_esd_layout(images: list[WimImage]) -> None:
    by_index = {i.index: i for i in images}
    pe, setup = by_index.get(2), by_index.get(3)
    if (
        len(images) < 4
        or pe is None
        or "windows pe" not in pe.name.lower()
        or setup is None
        or "setup" not in setup.name.lower()
    ):
        layout = ", ".join(f"{i.index}={i.name!r}" for i in images)
        raise FetchwinError(f"ESD does not have the expected installer layout: {layout}")


def select_editions(images: list[WimImage], wanted: Sequence[str]) -> list[WimImage]:
    editions = [i for i in images if i.index >= 4]
    if not wanted:
        return editions
    want = {w.lower() for w in wanted}
    chosen = [i for i in editions if i.edition_id.lower() in want or i.name.lower() in want]
    missing = want - {i.edition_id.lower() for i in chosen} - {i.name.lower() for i in chosen}
    if missing:
        avail = ", ".join(f"{i.edition_id} ({i.name})" for i in editions)
        raise FetchwinError(f"edition(s) not in ESD: {', '.join(sorted(missing))}; have: {avail}")
    return chosen


def iso_argv(tools: Toolchain, *, arch: str, stage: Path, iso: Path, label: str) -> list[str]:
    argv = [tools.iso_builder, "-iso-level", "3", "-udf", "-hide", "*", "-V", label]
    gen = tools.iso_flavor == "genisoimage"
    if gen:
        argv.append("-allow-limited-size")
    if arch == "x64":
        argv += ["-b", BIOS_BOOT, "-no-emul-boot", "-boot-load-size", "8", "-eltorito-alt-boot"]
    if gen:
        argv += ["-e", EFI_BOOT, "-no-emul-boot"]
    else:
        argv += ["-eltorito-platform", "efi", "-b", EFI_BOOT, "-no-emul-boot"]
    argv += ["-o", str(iso), str(stage)]
    return argv


def sanitize_label(label: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", label)[:32]
    if not clean:
        raise FetchwinError("empty volume label")
    return clean


@dataclass
class ConvertResult:
    iso: Path
    install_images: list[WimImage]
    split: bool


def convert(
    esd: Path,
    iso: Path,
    *,
    tools: Toolchain,
    arch: str,
    label: str,
    work_dir: Path,
    editions: Sequence[str] = (),
    split_wim: bool = False,
    run: Runner | None = None,
    log: IO[str] = sys.stderr,
) -> ConvertResult:
    run = run or subprocess_runner(log)
    wim = tools.wimlib
    label = sanitize_label(label)

    images = parse_wim_info(run([wim, "info", str(esd)]))
    validate_esd_layout(images)
    chosen = select_editions(images, editions)

    stage = work_dir / "iso"
    sources = stage / "sources"
    boot_wim, install_wim = sources / "boot.wim", sources / "install.wim"

    log.write("[1/4] Applying setup media (image 1)\n")
    run([wim, "apply", str(esd), "1", str(stage), "--no-acls"])
    if not (stage / EFI_BOOT).is_file():
        raise FetchwinError(f"setup media is missing {EFI_BOOT}")
    if arch == "x64" and not (stage / BIOS_BOOT).is_file():
        raise FetchwinError(f"x64 setup media is missing {BIOS_BOOT}")

    sources.mkdir(exist_ok=True)

    log.write("[2/4] Building boot.wim (WinPE + Setup)\n")
    lzx = ["--compress=LZX", "--chunk-size=32K"]
    run([wim, "export", str(esd), "2", str(boot_wim), *lzx])
    run([wim, "export", str(esd), "3", str(boot_wim), *lzx, "--boot"])

    # Default: LZMS, which lets wimlib copy the ESD's solid blocks verbatim (fast, smallest;
    # the same result CrystalFetch produces). wimlib cannot split solid WIMs, so for
    # --split-wim recompress to classic non-solid LZX, the format of Microsoft's own ISOs.
    install_compress = (
        ["--compress=LZX", "--chunk-size=32K"]
        if split_wim
        else ["--compress=LZMS", "--chunk-size=128K"]
    )
    log.write(f"[3/4] Building install.wim ({', '.join(i.name for i in chosen)})\n")
    for img in chosen:
        run([wim, "export", str(esd), str(img.index), str(install_wim), *install_compress])

    did_split = False
    if split_wim and install_wim.stat().st_size > FAT32_MAX:
        log.write("      Splitting install.wim into FAT32-safe install.swm parts\n")
        run([wim, "split", str(install_wim), str(sources / "install.swm"), str(SWM_PART_MB)])
        install_wim.unlink()
        did_split = True

    log.write("[4/4] Writing ISO\n")
    iso.parent.mkdir(parents=True, exist_ok=True)
    tmp_iso = iso.with_name(iso.name + ".tmp")
    tmp_iso.unlink(missing_ok=True)
    run(iso_argv(tools, arch=arch, stage=stage, iso=tmp_iso, label=label))
    if not tmp_iso.is_file() or tmp_iso.stat().st_size == 0:
        raise FetchwinError("ISO builder reported success but produced no ISO")
    tmp_iso.replace(iso)
    return ConvertResult(iso=iso, install_images=chosen, split=did_split)
