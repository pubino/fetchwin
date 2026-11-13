# Lint, then test. Exits non-zero on any analyzer finding or failed/absent test.
$ErrorActionPreference = 'Stop'
Import-Module Pester -RequiredVersion 5.7.1
Import-Module PSScriptAnalyzer -RequiredVersion 1.23.0

$root = Join-Path $PSScriptRoot '../..'
$target = Join-Path $root 'src/fetchwin/windows'
$findings = Invoke-ScriptAnalyzer -Path $target -Recurse -Settings (Join-Path $PSScriptRoot 'PSScriptAnalyzerSettings.psd1')
if ($findings) {
    $findings | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
    Write-Host "PSScriptAnalyzer: $(@($findings).Count) finding(s)"
    exit 1
}
Write-Host 'PSScriptAnalyzer: clean'

$cfg = New-PesterConfiguration
$cfg.Run.Path = $PSScriptRoot
$cfg.Run.Exit = $true
$cfg.Run.Throw = $false
$cfg.Output.Verbosity = 'Detailed'
$cfg.Run.PassThru = $false
$result = Invoke-Pester -Configuration $cfg
