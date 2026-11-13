# fetchwin

Script the download of official Windows 11/10 installation media from Microsoft and
assemble a bootable ISO, from the terminal, on macOS or Linux.

Microsoft's download page hands out ISOs through a session-gated web flow that breaks
scripts. The Media Creation Tool instead reads a public **product catalog**
(`products.cab` → `products.xml`) listing **ESD** files on Microsoft's CDN, with sizes and
hashes. fetchwin does what the Media Creation Tool, CrystalFetch and `w11arm_esd2iso`
do with that catalog:

1. Fetch the catalog over HTTPS and pick the newest ESD for your arch, channel and language.
2. Download the ESD (resumable). Verify its size and its catalog `Sha1`/`Sha256`.
3. Rebuild the installer layout with `wimlib-imagex`:
   ESD image 1 → ISO file tree, images 2–3 → `sources/boot.wim`, images 4+ → `sources/install.wim`.
4. Write a UDF ISO with El Torito boot entries: UEFI for arm64, BIOS + UEFI for x64.
5. Write `<iso>.json` (provenance manifest) and `<iso>.sha256` next to the ISO.

## Install

macOS:

```sh
scripts/bootstrap.sh        # brew bundle (uv, wimlib, cdrtools) + uv sync
uv run fetchwin --help
# or install the command globally: uv tool install .
```

Linux: install `wimtools genisoimage libarchive-tools` (Debian/Ubuntu names) and `uv`,
or use the container:

```sh
scripts/docker-fetch.sh fetch --arch x64 --channel business    # ISO lands in ./out
```

## Usage

```sh
fetchwin list                                   # ESDs in the catalog, newest marked *
fetchwin list --arch x64 --all-langs --json
fetchwin fetch --dry-run                        # show what would be downloaded and built
fetchwin fetch --out ~/ISOs                     # arm64, consumer, en-us (defaults)
fetchwin fetch --arch x64 --channel business --edition Enterprise --split-wim
fetchwin build ./some.esd --out ~/ISOs          # convert an ESD you already have
```

Re-running `fetch` is cheap. If the ISO for the newest ESD already exists with the same
options (per its manifest), nothing is downloaded or rebuilt. An interrupted download
resumes from `*.esd.part`. The output goes to stdout (ISO path, or JSON with `--json`)
and progress goes to stderr, so `iso=$(fetchwin fetch)` works in scripts.

Every flag has a `FETCHWIN_*` environment variable (see `example.env`). Invalid values
fail at startup.

### Latest cumulative update (`--updates`)

Install media lags behind Windows Update. With `--updates download` or
`--updates integrate`, after building, fetchwin:

1. Reads the base build from the media (e.g. `26300.9711`).
2. Looks up the latest build for that release in Microsoft's
   [Windows 11 release information](https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information)
   table (e.g. 26H2 → `26300.9974`).
3. Asks Windows Update's metadata service (UMS) for that build's update offer. It downloads
   the offer's `.msu` packages for your architecture, each checked against its SHA-256.
4. Writes them to `<iso-stem>.updates/`, plus `updates.json` with the KBs, hashes and the
   **target** package (the cumulative update the offer is for), plus
   `Install-FetchwinUpdates.ps1` to install it.

The offer holds the target and every earlier checkpoint update, including old ones the
media already contains. Install only the target and keep the rest in the same folder:
DISM takes the checkpoints the system lacks from there. Installing each package in turn
does not work; on 26300 media, DISM fails on the 24H2 checkpoint KB5043080 (`0x80070228`).

`integrate` also tries to service `install.wim` offline. That isn't available yet: the
WinPE/QEMU DISM worker is still a prototype. **When integration fails or isn't available,
the packages stay next to the ISO.** That's the intended fallback, for the installed
system to apply with the script below.

#### Installing the packages on the running system

Copy the whole `.updates` folder to a local disk on the target (it must survive a
restart), then run as Administrator or SYSTEM, for example as the last step of an
unattended install:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File C:\fetchwin-updates\Install-FetchwinUpdates.ps1 -Restart -AutoResume
```

The script:

- Exits `0` at once if the OS build is already at `target_build`.
- Checks each `.msu` against the SHA-256 in `updates.json` and requires a valid
  Microsoft Authenticode signature. It refuses packages for another release or CPU
  architecture.
- Installs only the target with `dism /Online /Add-Package`, which takes the checkpoints
  it needs from the same folder. This is Microsoft's documented "in one go" method for
  checkpoint cumulative updates. Tested on a Windows 11 26H2 Arm64 VM: 26300.9711 to
  26300.9974 in about 5 minutes plus one restart.
- Records progress in `%ProgramData%\fetchwin\updates-state.json`, so it is safe to re-run.
  It logs to `%ProgramData%\fetchwin\install-updates.log`, with DISM logs in `dism\`.
- When a restart is needed, exits `3010`. With `-Restart` it restarts by itself. With
  `-AutoResume` it registers a `fetchwin-updates` startup task (as SYSTEM) that runs it
  again after each restart, and removes the task when it finishes or fails. `-MaxRestarts`
  (default 4) stops a restart loop.
- `-DryRun` verifies everything and changes nothing.

Exit codes: `0` current, `3010` restart required, `1` install failed, `2` invalid input
(manifest, hash, signature, release, architecture, or not elevated).

If the media is already current, nothing is downloaded. The ISO's `<iso>.json` gains an
`updates` section either way.

Exit codes: `0` success, `1` error (nothing usable produced), `3` ISO built but the
updates step failed (WARNING printed, `updates.status = "failed"` in the manifest).



Each ESD contains several editions. Setup picks one from the image index or the product key.

| `--channel` | ESD          | Contains                                   | Activation            |
|-------------|--------------|--------------------------------------------|-----------------------|
| `consumer`  | `CLIENTCONSUMER_RET` | Home, Home Single Language, Pro (+ key-selectable variants) | Retail / OEM / digital license |
| `business`  | `CLIENTBUSINESS_VOL` | Enterprise, Education, Pro (volume)  | KMS / MAK (GVLKs preinstalled) |

`--edition ID` (repeatable) keeps only those editions in `install.wim`, matched by
Edition ID (`Enterprise`, `Professional`, …) or full name. Smaller image, no edition
prompt during setup.

### USB install on real (x64) hardware

UEFI firmware boots from FAT32, and FAT32 can't hold a file of 4 GiB or more.
`--split-wim` splits `install.wim` into `install.swm` parts when needed. Windows Setup
accepts those natively, so you can copy the ISO's contents straight onto a FAT32 stick.
Even a single edition is over 4 GiB, so plan on splitting. wimlib can't split the
ESD's solid LZMS data, so with `--split-wim` the editions are recompressed to classic LZX,
the format Microsoft's own ISOs use. The build takes several minutes longer and the
result is larger. Without the flag, the solid blocks are copied as they are, which is
fast. That's what CrystalFetch does, and it's fine for VMs.
On macOS (**double-check the disk identifier; this erases it**):

```sh
diskutil list                                     # find the stick, e.g. /dev/disk6
diskutil eraseDisk MS-DOS WINSETUP GPT /dev/diskN
hdiutil attach -readonly path/to/x64.iso          # mounts at /Volumes/<label>
rsync -rh --progress /Volumes/<label>/ /Volumes/WINSETUP/
diskutil eject /dev/diskN
```

The ISO itself is not a hybrid image, so don't `dd` it to a stick.

## Where "latest" comes from

The public catalog link (`go.microsoft.com/fwlink/?LinkId=2156292`) has been frozen
since June 2025 (build 26100.4349). The current Media Creation Tool doesn't rely on it.
Its `udiapi.dll` asks Windows Update's **Update Metadata Service** (UMS) for the
`Windows.Products.Cab` product, and uses the fwlink only as a fallback. fetchwin does the
same:

| `--catalog` | Behaviour |
|-------------|-----------|
| `auto` (default, Windows 11) | Live catalog from UMS. On failure, prints a WARNING and falls back to the fwlink |
| `ums` | Live catalog only. Fails if UMS can't be reached |
| `fwlink` | The frozen public catalog (the default for `--windows 10`) |
| URL / path | Any `products.cab` or `products.xml` you provide |

In November 2026 UMS returned a catalog listing **26300.9711 (26H2)** for both ARM64 and
x64, while the fwlink still served 26100.4349.

How the live lookup works and why it's trustworthy:

* fetchwin sends one public-release query (`PN=Windows.Products.Cab.amd64&V=0.0.0.0`,
  `DUScan=1`) to `https://fe3.delivery.mp.microsoft.com/UpdateMetadataService/...`. It
  never sends `DUInternal`, which can expose unreleased content.
* UMS uses a certificate chained to *Microsoft Root Certificate Authority 2011*.
  Windows trusts that root, but the public CA bundles on macOS and Linux don't. fetchwin ships
  that root in `src/fetchwin/certs/`, checks it against its pinned SHA-256 fingerprint,
  and trusts **only** that root for this connection.
* UMS returns the `products.cab` URL with its size and SHA-256. The CAB is verified
  before parsing.
* The API is undocumented. The request shape was recovered from `MediaCreationTool.exe`
  and matches community findings, so it may change. The fallback is there for that case,
  and the warning makes it visible.

## Reproducibility and trust

* **Integrity:** the ESD CDN (`dl.delivery.mp.microsoft.com`) has no valid TLS
  certificate, so ESDs are downloaded over HTTP. That's safe only because every byte is
  checked against the size and hash in the catalog, which is fetched over HTTPS. Catalog
  download URLs must be on `*.microsoft.com`, and filenames are validated before use.
* **Provenance:** `<iso>.json` records the catalog source (including the UMS update ID) and its SHA-256, the ESD entry, the
  options, the tool versions, the editions included, and the ISO's SHA-256.
* **Toolchain:** the Docker image pins its base image digests and apt package versions.
  `uv.lock` pins the Python dev dependencies; the runtime itself has none. ISOs aren't
  bit-identical across runs, because UDF and ISO9660 embed timestamps. Their contents
  (the WIMs) are deterministic exports of the verified ESD.

## Development

```sh
scripts/test.sh            # ruff, ruff format --check, mypy --strict, pytest (macOS toolchain)
scripts/test.sh --docker   # same, inside the pinned Linux container
```

`tests/test_integration.py` builds a tiny ESD with the real `wimlib-imagex`, runs the full
pipeline through the real `mkisofs`/`genisoimage`, and parses the ISO's El Torito boot
catalog. If the tools are missing it **fails** rather than skipping.

## Acknowledgements

The ESD → ISO recipe follows the approach of
[CrystalFetch](https://github.com/TuringSoftware/CrystalFetch),
Paul Rockwell's `w11arm_esd2iso`, and b0gdanw's "ESD to ISO on macOS" notes.
No code is copied from them.

Windows is a trademark of Microsoft. This tool downloads media from Microsoft's
servers; you need a valid license to install and activate it.
