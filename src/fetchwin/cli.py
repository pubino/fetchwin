"""Command-line interface.

Every option can also come from a FETCHWIN_* environment variable (see example.env).
Invalid values fail at startup, before any network or disk work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from fetchwin import __version__, ums, updates
from fetchwin.catalog import (
    ARCHES,
    CATALOG_URLS,
    CHANNELS,
    EsdEntry,
    catalog_xml,
    default_cab_extractor,
    http_get,
    matching,
    parse_products_xml,
    read_catalog_source,
    select_latest,
)
from fetchwin.convert import Runner, convert
from fetchwin.download import download
from fetchwin.errors import FetchwinError
from fetchwin.manifest import existing_build_matches, manifest_path, sha256_file, write_manifest
from fetchwin.tools import Toolchain, discover

GB = 1024**3
UPDATE_MODES = ("none", "download", "integrate")
EXIT_UPDATES_FAILED = 3

CATALOG_HELP = (
    "where to get the catalog: auto (default for 11: live Windows Update catalog, like the "
    "Media Creation Tool, falling back to the frozen fwlink), ums (live only), fwlink "
    "(frozen public link), or an https URL / local products.cab / products.xml"
)


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FETCHWIN_{name}", "").strip() or default


def _choice_env(name: str, default: str, choices: Sequence[str]) -> str:
    v = _env(name, default)
    if v not in choices:
        raise FetchwinError(f"FETCHWIN_{name}={v!r} is invalid; choose from {', '.join(choices)}")
    return v


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--windows",
        choices=sorted(CATALOG_URLS),
        default=_choice_env("WINDOWS", "11", sorted(CATALOG_URLS)),
        help="Windows major version catalog to use (env FETCHWIN_WINDOWS; default 11)",
    )
    common.add_argument(
        "--catalog",
        default=_env("CATALOG", ""),
        help=CATALOG_HELP + " (env FETCHWIN_CATALOG)",
    )
    common.add_argument(
        "--arch",
        choices=sorted(ARCHES),
        default=_choice_env("ARCH", "arm64", sorted(ARCHES)),
        help="env FETCHWIN_ARCH; default arm64",
    )
    common.add_argument(
        "--channel",
        choices=sorted(CHANNELS),
        default=_choice_env("CHANNEL", "consumer", sorted(CHANNELS)),
        help="consumer = retail Home/Pro/Education; business = volume-licensed "
        "Enterprise/Education/Pro (KMS/MAK). env FETCHWIN_CHANNEL",
    )
    common.add_argument(
        "--lang",
        default=_env("LANG", "en-us"),
        help="language code, e.g. en-us, en-gb, de-de (env FETCHWIN_LANG)",
    )
    common.add_argument("--json", action="store_true", help="machine-readable output on stdout")

    p = argparse.ArgumentParser(
        prog="fetchwin",
        description="Download official Windows ESD media from Microsoft and build a bootable ISO.",
    )
    p.add_argument("--version", action="version", version=f"fetchwin {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", parents=[common], help="show ESDs in the catalog")
    ls.add_argument("--all-langs", action="store_true", help="ignore --lang")

    def build_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--out",
            type=Path,
            default=Path(_env("OUT", ".")),
            help="output directory for the ISO (env FETCHWIN_OUT; default .)",
        )
        sp.add_argument(
            "--edition",
            action="append",
            default=[],
            metavar="ID",
            help="only include this edition in install.wim (Edition ID or name, "
            "e.g. Enterprise, Professional). Repeatable. Default: all",
        )
        sp.add_argument(
            "--split-wim",
            action="store_true",
            help="split install.wim into <4 GiB install.swm parts if needed, so "
            "the ISO contents can be copied to a FAT32 USB stick",
        )
        sp.add_argument(
            "--work-dir",
            type=Path,
            default=None,
            help="scratch directory (default: <out>/.fetchwin-work)",
        )
        sp.add_argument(
            "--keep-work",
            action="store_true",
            help="keep the scratch directory after a successful build",
        )
        sp.add_argument(
            "--updates",
            choices=UPDATE_MODES,
            default=_choice_env("UPDATES", "none", UPDATE_MODES),
            help="also fetch the latest cumulative update for the media's release: "
            "download = leave verified .msu packages in <iso-stem>.updates/ with an "
            "install order; integrate = try to service install.wim offline, falling back "
            "to download behaviour (env FETCHWIN_UPDATES; default none)",
        )

    fe = sub.add_parser(
        "fetch", parents=[common], help="download the newest matching ESD and build an ISO"
    )
    build_opts(fe)
    fe.add_argument("--keep-esd", action="store_true", help="keep the ESD after building")
    fe.add_argument("--force", action="store_true", help="rebuild even if an up-to-date ISO exists")
    fe.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the catalog and print the plan; download and build nothing",
    )

    bu = sub.add_parser("build", help="build an ISO from an ESD you already have")
    bu.add_argument("esd", type=Path)
    bu.add_argument(
        "--arch",
        choices=sorted(ARCHES),
        default=None,
        help="default: inferred from the ESD filename",
    )
    bu.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    build_opts(bu)
    return p


def _check_serves_request(args: argparse.Namespace, entries: list[EsdEntry]) -> None:
    """Raise unless the catalog has media for this request (so auto can fall back)."""
    if getattr(args, "all_langs", False):
        if not matching(entries, arch=args.arch, lang=None, channel=args.channel):
            raise FetchwinError(f"no {args.channel} {args.arch} ESDs in catalog")
    else:
        select_latest(entries, arch=args.arch, lang=args.lang, channel=args.channel)


def _load_entries(
    args: argparse.Namespace, tools: Toolchain, log: IO[str]
) -> tuple[str, str, list[EsdEntry]]:
    extractor = default_cab_extractor(tools.bsdtar, tools.cabextract)
    source = args.catalog or ("auto" if args.windows == "11" else "fwlink")
    if source in ("auto", "ums"):
        if args.windows != "11":
            raise FetchwinError("the live (ums) catalog is only available for Windows 11")
        # Everything that can go wrong with the live path - network, TLS, the response,
        # the CAB, the XML, or a catalog that doesn't carry the requested media - is
        # a reason to fall back in auto mode.
        try:
            loc, raw = ums.fetch_products_cab()
            entries = parse_products_xml(catalog_xml(raw, extract_cab=extractor))
            _check_serves_request(args, entries)
            return (
                f"ums:{ums.UMS_URL}#update={loc.update_id}",
                hashlib.sha256(raw).hexdigest(),
                entries,
            )
        except (FetchwinError, OSError) as e:
            if source == "ums":
                raise FetchwinError(f"live catalog query failed: {e}") from e
            log.write(
                f"fetchwin: WARNING: live catalog unusable ({e}); falling back to the "
                "frozen fwlink catalog, which may be months out of date\n"
            )
            source = "fwlink"
    if source == "fwlink":
        source = CATALOG_URLS[args.windows]
    raw = read_catalog_source(source, fetch=http_get)
    entries = parse_products_xml(catalog_xml(raw, extract_cab=extractor))
    return source, hashlib.sha256(raw).hexdigest(), entries


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _infer_arch(filename: str) -> str:
    for name, (_, tag) in ARCHES.items():
        if tag.lower() in filename.lower():
            return name
    raise FetchwinError(f"cannot infer arch from {filename!r}; pass --arch")


def cmd_list(args: argparse.Namespace, out: IO[str], log: IO[str], tools: Toolchain) -> int:
    source, _, entries = _load_entries(args, tools, log)
    lang = None if args.all_langs else args.lang
    found = matching(entries, arch=args.arch, lang=lang, channel=args.channel)
    if args.json:
        json.dump({"catalog": source, "entries": [e.to_dict() for e in found]}, out, indent=2)
        out.write("\n")
        return 0
    if not found:
        raise FetchwinError(f"no {args.channel} {args.arch} entries for {lang or 'any language'}")
    newest = max(e.build for e in found)
    for e in found:
        mark = "*" if e.build == newest else " "
        out.write(
            f"{mark} {e.label:<24} {e.language_code:<7} {e.size / GB:5.2f} GiB  {e.filename}\n"
        )
    return 0


def _options(args: argparse.Namespace, arch: str) -> dict[str, Any]:
    return {
        "arch": arch,
        "editions": sorted(e.lower() for e in args.edition),
        "split_wim": bool(args.split_wim),
    }


def cmd_fetch(
    args: argparse.Namespace, out: IO[str], log: IO[str], tools: Toolchain, run: Runner | None
) -> int:
    source, cat_sha, entries = _load_entries(args, tools, log)
    entry = select_latest(entries, arch=args.arch, lang=args.lang, channel=args.channel)
    out_dir: Path = args.out.expanduser().resolve()
    iso = out_dir / entry.iso_name
    esd = out_dir / entry.filename
    work = (args.work_dir or out_dir / ".fetchwin-work").expanduser().resolve()
    options = _options(args, args.arch)
    algo, digest = entry.hash()
    up_to_date = existing_build_matches(iso, entry, options)

    plan = {
        "catalog": source,
        "esd": entry.to_dict(),
        "iso": str(iso),
        "options": options,
        "up_to_date": up_to_date,
        "action": "skip" if (up_to_date and not args.force) else "build",
    }
    if args.dry_run:
        if args.json:
            json.dump(plan, out, indent=2)
            out.write("\n")
        else:
            out.write(f"Catalog:  {source}\n")
            out.write(f"Latest:   {entry.label} {args.arch} {args.channel} {entry.language_code}\n")
            out.write(f"ESD:      {entry.url}\n")
            out.write(f"          {entry.size / GB:.2f} GiB, {algo} {digest}\n")
            out.write(f"ISO:      {iso}\n")
            out.write(f"Action:   {plan['action']} (dry run, nothing done)\n")
        return 0

    if up_to_date and not args.force:
        log.write(f"Up to date: {iso}\n")
        data = json.loads(manifest_path(iso).read_text())
        rc = _updates_step(args, iso=iso, label=entry.label, arch=args.arch, data=data, log=log)
        _emit(args, out, iso, {**plan, "updates": data.get("updates")})
        return rc

    out_dir.mkdir(parents=True, exist_ok=True)
    need = (0 if esd.exists() else entry.size) + int(entry.size * 2.2) + GB
    free = _free_bytes(work)
    if free < need:
        raise FetchwinError(
            f"need about {need / GB:.1f} GiB free under {work}, have {free / GB:.1f} GiB"
        )

    log.write(f"Latest {args.channel} {args.arch} {entry.language_code}: {entry.label}\n")
    download(entry.url, esd, size=entry.size, algo=algo, digest=digest, log=log)
    data = _build(
        args,
        entry=entry,
        esd=esd,
        iso=iso,
        work=work,
        arch=args.arch,
        tools=tools,
        run=run,
        log=log,
        out=out,
        catalog=(source, cat_sha),
        options=options,
    )
    if not args.keep_esd:
        esd.unlink()
    rc = _updates_step(args, iso=iso, label=entry.label, arch=args.arch, data=data, log=log)
    _emit(args, out, iso, data)
    return rc


def cmd_build(
    args: argparse.Namespace, out: IO[str], log: IO[str], tools: Toolchain, run: Runner | None
) -> int:
    esd: Path = args.esd.expanduser().resolve()
    if not esd.is_file():
        raise FetchwinError(f"ESD not found: {esd}")
    arch = args.arch or _infer_arch(esd.name)
    log.write(f"Hashing {esd.name} ...\n")
    entry = EsdEntry(
        filename=esd.name if esd.name.endswith(".esd") else esd.name + ".esd",
        language_code="unknown",
        architecture=ARCHES[arch][0],
        size=esd.stat().st_size,
        url=esd.as_uri(),
        sha1=None,
        sha256=sha256_file(esd),
    )
    out_dir: Path = args.out.expanduser().resolve()
    work = (args.work_dir or out_dir / ".fetchwin-work").expanduser().resolve()
    iso = out_dir / entry.iso_name
    data = _build(
        args,
        entry=entry,
        esd=esd,
        iso=iso,
        work=work,
        arch=arch,
        tools=tools,
        run=run,
        log=log,
        out=out,
        catalog=("local", ""),
        options=_options(args, arch),
    )
    rc = _updates_step(args, iso=iso, label=entry.label, arch=arch, data=data, log=log)
    _emit(args, out, iso, data)
    return rc


def _build(
    args: argparse.Namespace,
    *,
    entry: EsdEntry,
    esd: Path,
    iso: Path,
    work: Path,
    arch: str,
    tools: Toolchain,
    run: Runner | None,
    log: IO[str],
    out: IO[str],
    catalog: tuple[str, str],
    options: dict[str, Any],
) -> dict[str, Any]:
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        result = convert(
            esd,
            iso,
            tools=tools,
            arch=arch,
            label=entry.label,
            work_dir=work,
            editions=args.edition,
            split_wim=args.split_wim,
            run=run,
            log=log,
        )
    except BaseException:
        log.write(f"Build failed; scratch directory kept for debugging: {work}\n")
        raise
    if not args.keep_work:
        shutil.rmtree(work)
    log.write("Hashing ISO and writing manifest ...\n")
    data = write_manifest(
        iso,
        entry=entry,
        catalog_source=catalog[0],
        catalog_sha256=catalog[1],
        options=options,
        tool_versions=tools.versions(),
        result=result,
    )
    return data


def _emit(args: argparse.Namespace, out: IO[str], iso: Path, data: dict[str, Any]) -> None:
    if args.json:
        json.dump(data, out, indent=2)
        out.write("\n")
    else:
        out.write(f"{iso}\n")


def _updates_step(
    args: argparse.Namespace,
    *,
    iso: Path,
    label: str,
    arch: str,
    data: dict[str, Any],
    log: IO[str],
) -> int:
    """Fetch (and maybe integrate) updates. Never touches the ISO on failure."""
    if args.updates == "none":
        return 0
    dest = iso.with_name(iso.stem + ".updates")
    rc = 0
    try:
        res = updates.fetch_updates(
            base_label=label,
            arch=arch,
            dest=dest,
            get_release_page=http_get,
            post=ums.https_post,
            get_pinned=updates.https_get_pinned,
            download=download,
            log=log,
        )
        if res.status == "downloaded" and args.updates == "integrate":
            try:
                updates.integrate(res, iso)
                res.status = "integrated"
            except FetchwinError as e:
                res.note = str(e)
                log.write(f"fetchwin: WARNING: update integration failed: {e}\n")
        if res.packages:
            updates.write_updates_manifest(res, dest)
            if res.status == "downloaded":
                log.write(
                    f"Updates left alongside the ISO in {dest} (install {res.target} "
                    "(DISM takes the checkpoints it needs from the same folder), "
                    f"or run {updates.INSTALLER})\n"
                )
        data["updates"] = res.to_dict()
    except (FetchwinError, OSError) as e:
        log.write(f"fetchwin: WARNING: updates step failed ({e}); the ISO is unaffected\n")
        data["updates"] = {"status": "failed", "error": str(e)}
        rc = EXIT_UPDATES_FAILED
    manifest_path(iso).write_text(json.dumps(data, indent=2) + "\n")
    return rc


def main(
    argv: Sequence[str] | None = None,
    *,
    out: IO[str] | None = None,
    log: IO[str] | None = None,
    tools: Toolchain | None = None,
    run: Runner | None = None,
) -> int:
    out = out or sys.stdout
    log = log or sys.stderr
    try:
        args = build_parser().parse_args(argv)
        need_build = args.cmd == "build" or (args.cmd == "fetch" and not args.dry_run)
        tools = tools or discover(need_build=need_build)
        if args.cmd == "list":
            return cmd_list(args, out, log, tools)
        if args.cmd == "fetch":
            return cmd_fetch(args, out, log, tools, run)
        return cmd_build(args, out, log, tools, run)
    except FetchwinError as e:
        log.write(f"fetchwin: error: {e}\n")
        return 1
    except KeyboardInterrupt:
        log.write("fetchwin: interrupted (partial download is kept and will resume)\n")
        return 130
