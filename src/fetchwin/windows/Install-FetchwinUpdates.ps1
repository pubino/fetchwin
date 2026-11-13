<#
.SYNOPSIS
    Install the update packages that fetchwin left next to an ISO, on the running system.

.DESCRIPTION
    Reads updates.json from the package folder and checks every .msu against its recorded
    SHA-256 and its Microsoft Authenticode signature. Then it installs only the target
    cumulative update with DISM (/Online /Add-Package). DISM takes whatever checkpoint
    updates the system lacks from the same folder: Microsoft's "in one go" method for
    checkpoint cumulative updates. (Installing every package in turn fails: the folder
    also holds old checkpoints that newer media already contains.)

    The script is safe to re-run. It records progress in
    %ProgramData%\fetchwin\updates-state.json and exits at once when the OS build already
    matches target_build.

    When the install needs a restart, or cannot start until one, the script asks for a
    restart (exit 3010). With -Restart it restarts by itself. With -AutoResume it
    registers a startup task that runs it again after each restart and removes itself
    when the work is done or has failed.

    Exit codes:
      0     The OS is at target_build, or nothing applies.
      3010  A restart is required. Run the script again after it.
      1     An install failed, or too many restarts.
      2     Invalid input: bad updates.json, wrong OS release or architecture, bad hash
            or signature, or not elevated.

.PARAMETER Path
    The folder that holds updates.json and the .msu files. Default: this script's folder.
    With -AutoResume, use a local folder (not removable media) that survives a restart.

.PARAMETER Restart
    Restart Windows when a restart is required, instead of exiting with 3010.

.PARAMETER AutoResume
    Register the startup task 'fetchwin-updates' (runs as SYSTEM) to continue after each
    restart. Use it with -Restart for an unattended run.

.PARAMETER MaxRestarts
    Stop with exit code 1 after this many restarts for one target build. Default: 4.

.PARAMETER DryRun
    Check the folder and show what the script would do. Install nothing, restart nothing.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Updates\Install-FetchwinUpdates.ps1 -Restart -AutoResume
#>
[CmdletBinding()]
param(
    [string]$Path = $PSScriptRoot,
    [switch]$Restart,
    [switch]$AutoResume,
    [ValidateRange(1, 20)][int]$MaxRestarts = 4,
    [switch]$DryRun
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$script:TaskName = 'fetchwin-updates'
$script:MsuPattern = '^Windows1[01]\.0-KB\d{6,8}-(x64|arm64)\.msu$'
$script:Sha256Pattern = '^[0-9a-fA-F]{64}$'
$script:BuildPattern = '^\d{5}\.\d{1,5}$'
$script:NotApplicable = -2146498530  # 0x800F081E CBS_E_NOT_APPLICABLE
$script:RebootRequired = 3010
$script:Opt = [pscustomobject]@{ Path = ''; Restart = $false; AutoResume = $false; MaxRestarts = 4; DryRun = $false }

class FetchwinInputError : System.Exception {
    FetchwinInputError([string]$message) : base($message) {}
}

# ---------- environment probes (mocked in tests) ----------

function Get-StateDirectory {
    Join-Path $env:ProgramData 'fetchwin'
}

function Write-UpdateLog {
    param([string]$Message)
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    Write-Host $line
    if (-not $script:Opt.DryRun) {
        $dir = Get-StateDirectory
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Add-Content -LiteralPath (Join-Path $dir 'install-updates.log') -Value $line -Encoding UTF8
    }
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-OsBuild {
    $cv = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    '{0}.{1}' -f $cv.CurrentBuild, $cv.UBR
}

function Get-OsArchTag {
    switch ((Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1).Architecture) {
        9 { 'x64' }
        12 { 'arm64' }
        default { 'unsupported' }
    }
}

function Test-RebootPending {
    Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
}

function Test-MicrosoftSignature {
    param([string]$File)
    $sig = Get-AuthenticodeSignature -LiteralPath $File
    ($sig.Status -eq 'Valid') -and ($sig.SignerCertificate.Subject -match 'O=Microsoft Corporation')
}

function Invoke-Dism {
    param([string]$Package, [string]$LogFile)
    $dism = Join-Path $env:SystemRoot 'System32\dism.exe'
    & $dism '/Online' '/Add-Package' "/PackagePath:$Package" '/NoRestart' '/Quiet' "/LogPath:$LogFile" | Out-Host
    $LASTEXITCODE
}

function Invoke-Restart {
    Restart-Computer -Force
}

function Register-ResumeTask {
    param([string]$ScriptPath, [string]$PackageDir, [int]$Restarts)
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$ScriptPath`"",
        '-Path', "`"$PackageDir`"", '-AutoResume', '-MaxRestarts', $Restarts)
    if ($script:Opt.Restart) { $argList += '-Restart' }
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ($argList -join ' ')
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4)
    Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
}

function Unregister-ResumeTask {
    if (Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false
    }
}

# ---------- pure logic ----------

function Compare-Build {
    # Returns -1, 0 or 1. Both builds look like 26300.9711.
    param([string]$A, [string]$B)
    $pa = $A.Split('.') | ForEach-Object { [int]$_ }
    $pb = $B.Split('.') | ForEach-Object { [int]$_ }
    for ($i = 0; $i -lt 2; $i++) {
        if ($pa[$i] -lt $pb[$i]) { return -1 }
        if ($pa[$i] -gt $pb[$i]) { return 1 }
    }
    0
}

function Read-UpdatesManifest {
    param([string]$Dir)
    $file = Join-Path $Dir 'updates.json'
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw [FetchwinInputError]::new("updates.json not found in $Dir")
    }
    try {
        $m = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
    } catch {
        throw [FetchwinInputError]::new("updates.json is not valid JSON: $($_.Exception.Message)")
    }
    foreach ($key in 'target_build', 'target', 'packages') {
        if (-not ($m.PSObject.Properties.Name -contains $key)) {
            throw [FetchwinInputError]::new("updates.json has no '$key'")
        }
    }
    if ([string]$m.target_build -notmatch $script:BuildPattern) {
        throw [FetchwinInputError]::new("invalid target_build '$($m.target_build)'")
    }
    $packages = @()
    foreach ($p in @($m.packages)) {
        $name = [string]$p.filename
        if ($name -notmatch $script:MsuPattern) {
            throw [FetchwinInputError]::new("invalid package name '$name'")
        }
        if ([string]$p.sha256 -notmatch $script:Sha256Pattern) {
            throw [FetchwinInputError]::new("invalid sha256 for $name")
        }
        $packages += [pscustomobject]@{
            Name   = $name
            File   = Join-Path $Dir $name
            Sha256 = ([string]$p.sha256).ToLowerInvariant()
            Arch   = ([regex]::Match($name, $script:MsuPattern)).Groups[1].Value.ToLowerInvariant()
        }
    }
    $target = @($packages | Where-Object { $_.Name -eq [string]$m.target })
    if ($target.Count -ne 1) {
        throw [FetchwinInputError]::new("target '$($m.target)' is not one of the packages")
    }
    [pscustomobject]@{ TargetBuild = [string]$m.target_build; Packages = $packages; Target = $target[0] }
}

function Read-State {
    param([string]$Target)
    $file = Join-Path (Get-StateDirectory) 'updates-state.json'
    $empty = [pscustomobject]@{ target_build = $Target; restarts = 0; done = @() }
    if (-not (Test-Path -LiteralPath $file)) { return $empty }
    try {
        $s = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
    } catch {
        return $empty
    }
    if ([string]$s.target_build -ne $Target) { return $empty }
    [pscustomobject]@{
        target_build = $Target
        restarts     = [int]$s.restarts
        done         = @($s.done | ForEach-Object { [string]$_ })
    }
}

function Save-State {
    param($State)
    if ($script:Opt.DryRun) { return }
    $dir = Get-StateDirectory
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $State | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dir 'updates-state.json') -Encoding UTF8
}

function Complete-Run {
    param([int]$Code)
    if ($script:Opt.AutoResume -and -not $script:Opt.DryRun -and $Code -ne $script:RebootRequired) { Unregister-ResumeTask }
    $Code
}

function Request-Restart {
    param($State, [string]$Reason)
    $State.restarts = [int]$State.restarts + 1
    if ($State.restarts -gt $script:Opt.MaxRestarts) {
        Save-State $State
        Write-UpdateLog "ERROR: still not done after $($script:Opt.MaxRestarts) restarts ($Reason)"
        return (Complete-Run 1)
    }
    Save-State $State
    if ($script:Opt.DryRun) {
        Write-UpdateLog "Dry run: would restart ($Reason)"
        return $script:RebootRequired
    }
    if ($script:Opt.AutoResume) {
        Register-ResumeTask -ScriptPath $PSCommandPath -PackageDir $script:Opt.Path -Restarts $script:Opt.MaxRestarts
    }
    if ($script:Opt.Restart) {
        Write-UpdateLog "Restarting: $Reason"
        Invoke-Restart
    } else {
        Write-UpdateLog "Restart required: $Reason. Run this script again after the restart."
    }
    $script:RebootRequired
}

function Invoke-FetchwinUpdate {
    [CmdletBinding()]
    [OutputType([int])]
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$Restart,
        [switch]$AutoResume,
        [int]$MaxRestarts = 4,
        [switch]$DryRun
    )
    $script:Opt = [pscustomobject]@{
        Path        = $Path
        Restart     = [bool]$Restart
        AutoResume  = [bool]$AutoResume
        MaxRestarts = $MaxRestarts
        DryRun      = [bool]$DryRun
    }
    try {
        $manifest = Read-UpdatesManifest -Dir $Path
    } catch [FetchwinInputError] {
        Write-UpdateLog "ERROR: $($_.Exception.Message)"
        return (Complete-Run 2)
    }
    $target = $manifest.TargetBuild
    $os = Get-OsBuild
    Write-UpdateLog "OS build $os; target build $target; packages in $Path"

    if ($os.Split('.')[0] -ne $target.Split('.')[0]) {
        Write-UpdateLog "ERROR: these updates are for build $target; this OS is $os"
        return (Complete-Run 2)
    }
    if ((Compare-Build $os $target) -ge 0) {
        Write-UpdateLog 'OS is current; nothing to install'
        return (Complete-Run 0)
    }
    if (-not $DryRun -and -not (Test-Elevated)) {
        Write-UpdateLog 'ERROR: run this script as Administrator (or SYSTEM)'
        return 2
    }
    $arch = Get-OsArchTag
    foreach ($p in $manifest.Packages) {
        if ($p.Arch -ne $arch) {
            Write-UpdateLog "ERROR: $($p.Name) is for $($p.Arch); this OS is $arch"
            return (Complete-Run 2)
        }
    }

    $state = Read-State -Target $target
    $logDir = Join-Path (Get-StateDirectory) 'dism'
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

    foreach ($p in $manifest.Packages) {
        if (-not (Test-Path -LiteralPath $p.File -PathType Leaf)) {
            Write-UpdateLog "ERROR: $($p.Name) is missing from $Path"
            return (Complete-Run 2)
        }
        $actual = (Get-FileHash -LiteralPath $p.File -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $p.Sha256) {
            Write-UpdateLog "ERROR: $($p.Name) SHA-256 is $actual; expected $($p.Sha256)"
            return (Complete-Run 2)
        }
        if (-not (Test-MicrosoftSignature -File $p.File)) {
            Write-UpdateLog "ERROR: $($p.Name) does not have a valid Microsoft signature"
            return (Complete-Run 2)
        }
    }
    $t = $manifest.Target
    if ($DryRun) {
        Write-UpdateLog "Dry run: all packages verified; would install $($t.Name)"
        return 0
    }

    if ($state.done -contains $t.Name) {
        Write-UpdateLog "$($t.Name): installed by an earlier run"
    } else {
        Write-UpdateLog "Installing $($t.Name) (checkpoints from the same folder) ..."
        $code = Invoke-Dism -Package $t.File -LogFile (Join-Path $logDir ($t.Name + '.log'))
        if ($code -eq 0 -or $code -eq $script:RebootRequired) {
            Write-UpdateLog "$($t.Name): installed (DISM exit $code)"
            $state.done = @($state.done) + $t.Name
            Save-State $state
            # DISM's 3010 is the evidence; the CBS RebootPending key is not always set.
            if ($code -eq $script:RebootRequired) {
                return (Request-Restart $state 'finish installing updates')
            }
        } elseif ($code -eq $script:NotApplicable) {
            Write-UpdateLog "$($t.Name): DISM says it does not apply to this OS"
            $state.done = @($state.done) + $t.Name
            Save-State $state
        } elseif (Test-RebootPending) {
            Write-UpdateLog ('{0}: DISM exit 0x{1:X8} with a restart pending' -f $t.Name, $code)
            return (Request-Restart $state "$($t.Name) needs the pending restart first")
        } else {
            Write-UpdateLog ('ERROR: {0}: DISM exit 0x{1:X8}; see {2}' -f $t.Name, $code, $logDir)
            return (Complete-Run 1)
        }
    }

    if (Test-RebootPending) {
        return (Request-Restart $state 'finish installing updates')
    }
    $os = Get-OsBuild
    if ((Compare-Build $os $target) -lt 0) {
        Write-UpdateLog "ERROR: all packages processed, but the OS build is $os (target $target)"
        return (Complete-Run 1)
    }
    Write-UpdateLog "OS is at $os"
    Complete-Run 0
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-FetchwinUpdate -Path $Path -Restart:$Restart -AutoResume:$AutoResume `
            -MaxRestarts $MaxRestarts -DryRun:$DryRun)
}
