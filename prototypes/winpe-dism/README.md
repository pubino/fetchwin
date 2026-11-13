# Prototype: DISM in WinPE under QEMU (parked)

**Status: parked, not part of fetchwin.** This is the research behind
`fetchwin --updates integrate`: servicing `install.wim` offline on a Mac, with no Windows
host, so the ISO itself carries the latest cumulative update. It boots and runs DISM, but
offline package installs fail (see [Blocker](#blocker)). fetchwin leaves the packages beside
the ISO instead, and `Install-FetchwinUpdates.ps1` installs them on the running system;
that path is tested end to end.

Tracking issue: [#1](https://github.com/pubino/fetchwin/issues/1).

The pure parts (startnet script, MBR, QEMU and mkisofs command lines, QMP key names) are
unit-tested in `tests/test_prototype_winpe.py`. Nothing here is run in CI against real
media.

## How it works

1. Copy the boot files from Windows install media and export `boot.wim` index 1 (plain
   WinPE: it has `Dism.exe`, the CBS provider, `diskpart`, `wpeutil`, `xcopy`,
   `robocopy`; it does not have `findstr`, `tasklist` or `sc`).
2. Inject a `startnet.cmd` with `wimlib-imagex update`. It finds the job folder and the
   output disk by marker files, runs `job.cmd` through `cmd /c` (so a failing job still
   reaches the shutdown), copies `dism.log`, writes `done.txt` and shuts down.
3. Build a UDF job ISO that boots `efisys_noprompt.bin` (no "press any key").
4. Create the output disk on the host (MBR + FAT32 at 1 MiB, formatted with `mformat`; no
   mounting, no root) and an empty scratch disk that the job formats as NTFS with
   `diskpart`.
5. Boot headless: arm64 under HVF (`-M virt`, USB CD, NVMe disks), x64 under TCG (`q35`,
   IDE CD). Screenshots come over QMP; `qmp_type.py` types into the VM.
6. Read `job.log`, `dism.log` and `done.txt` back with `mcopy`.

Boot to DISM takes about 16 s on arm64 (HVF). x64 under TCG is about 60x slower.

## Running it

```zsh
brew install qemu wimlib cdrtools mtools
python3 winpe_worker.py SOURCE.iso /tmp/pe jobs/01-smoke.cmd
# Servicing attempt: the job names the files it expects in %JOB%
python3 winpe_worker.py SOURCE.iso /tmp/pe jobs/02-service-lcu.cmd pro.wim \
  SSU-26100.9971-arm64.cab Windows11.0-KB5132061-arm64.msu \
  Windows11.0-KB5132061-arm64.wim Windows11.0-KB5132061-arm64.psf
ARCH=x64 python3 winpe_worker.py X64.iso /tmp/pe-x64 --timeout 1800 \
  jobs/04-dismhost-diagnostics.cmd image.wim package.cab
```

`pro.wim`/`image.wim` is one edition exported from the ESD with `wimlib-imagex export`.
The extra files are hard-linked, so keep them on the same volume as the work directory.

| Job | What it does |
| --- | --- |
| `01-smoke.cmd` | WinPE version, `dism /Get-WimInfo`, `diskpart list disk`. Works. |
| `02-service-lcu.cmd` | Mount, then try the SSU cab, the LCU `.msu`, and the LCU PSFX `.wim`; commit and split to FAT32 if any works. All fail (below). |
| `03-service-lcu-synced-dism.cmd` | The same, after copying the image's own DISM into WinPE. Same failure. |
| `04-dismhost-diagnostics.cmd` | One `/Add-Package` at log level 4, plus scratch and COM registry listings. |

## Blocker

Any `dism /Image:S:\mnt /Add-Package` fails with `0x80004002` (`E_NOINTERFACE`). With
`/LogLevel:4` the sequence is:

```
Copying DISM from "S:\mnt\Windows\System32\Dism"
Adding code integrity validation for offline OS [S:\mnt\Windows]
Attempting CoCreateInstance on DismHost: Number of tries = 1..5
Failed to create DismHostManager remote object (hr:0x80004002)
```

DISM copies the image's servicing stack to scratch and starts it in `dismhost.exe` over
COM; WinPE cannot create that object. Measured 2026-11-12:

- Same on arm64 (HVF, WinPE 26100.9549, image 26300.9711 Pro) and x64 (TCG, WinPE
  26100.1150, image 26100.4349 Enterprise). Not architecture, not HVF.
- Same for an SSU `.cab`, the LCU `.msu`, the PSFX `.wim`, and a 24 KB test package. Not
  the package format.
- Same with the image's DISM copied into WinPE (`03`), and with Setup's WinPE (boot.wim
  index 2; its System32 is identical). Not a version mismatch.
- `Mount-Image`, `Get-WimInfo` and other read-only operations work.

Untested ideas:

1. **Code integrity.** The step right before the failure adds code-integrity validation
   for the offline OS. If the copied `dismhost.exe` or its providers fail CI checks in
   WinPE, the COM server would never start. Test by watching for CI events, or by
   trying an image whose servicing stack WinPE already trusts.
2. **COM registration at runtime.** Look for the proxy/stub and AppID registrations DISM
   expects once it has copied itself to scratch (`04` dumps what WinPE has).
3. **Fallback:** a small Windows VM as the servicing worker (Windows Server Core or an
   evaluation image), which works but needs a Windows license and more setup.

Background on the update packages themselves: the cumulative update `.msu` is now a WIM
container (DesktopDeployment.cab with UpdateAgent.dll, the SSU cab, a PSFX `.wim` and
`.psf`). The UMS offer includes old checkpoint packages; online, DISM must be given only
the target (see the main README), and offline integration would likely need the same.
