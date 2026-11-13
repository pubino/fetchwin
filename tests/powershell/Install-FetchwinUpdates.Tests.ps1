#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }

BeforeAll {
    $script:ScriptFile = Join-Path $PSScriptRoot '../../src/fetchwin/windows/Install-FetchwinUpdates.ps1'
    . $script:ScriptFile

    function Invoke-Run {
        # Runs the entry point with the test's current $Path/$Restart/... variables.
        Invoke-FetchwinUpdate -Path $Path -Restart:$Restart -AutoResume:$AutoResume `
            -MaxRestarts $MaxRestarts -DryRun:$DryRun
    }

    function New-Fixture {
        param([hashtable]$Override = @{})
        $dir = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $dir | Out-Null
        $pkgs = @()
        foreach ($n in 'Windows11.0-KB5043080-arm64.msu', 'Windows11.0-KB5132061-arm64.msu') {
            $f = Join-Path $dir $n
            Set-Content -LiteralPath $f -Value "body of $n" -NoNewline
            $pkgs += [ordered]@{
                filename = $n
                kb       = ($n -split '-')[1]
                size     = (Get-Item -LiteralPath $f).Length
                sha256   = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
        $m = [ordered]@{
            status        = 'downloaded'
            base_build    = '26300.9711'
            target_build  = '26300.9974'
            packages      = $pkgs
            install_order = @($pkgs | ForEach-Object { $_.filename })
        }
        foreach ($k in $Override.Keys) { $m[$k] = $Override[$k] }
        $m | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $dir 'updates.json')
        $dir
    }
}

Describe 'Install-FetchwinUpdates' {
    BeforeEach {
        $script:State = Join-Path $TestDrive ('state-' + [guid]::NewGuid().ToString('N'))
        $script:Builds = [System.Collections.Generic.Queue[string]]::new()
        $script:Pending = $false
        $script:DismCodes = @{}
        $script:Installed = [System.Collections.Generic.List[string]]::new()

        $Path = New-Fixture
        $Restart = $false
        $AutoResume = $false
        $DryRun = $false
        $MaxRestarts = 4

        Mock Get-StateDirectory { $script:State }
        Mock Get-OsBuild {
            if ($script:Builds.Count -gt 1) { $script:Builds.Dequeue() } else { $script:Builds.Peek() }
        }
        Mock Get-OsArchTag { 'arm64' }
        Mock Test-Elevated { $true }
        Mock Test-RebootPending { $script:Pending }
        Mock Test-MicrosoftSignature { $true }
        Mock Invoke-Dism {
            $name = Split-Path -Leaf $Package
            $script:Installed.Add($name)
            if ($script:DismCodes.ContainsKey($name)) { return $script:DismCodes[$name] }
            0
        }
        Mock Invoke-Restart {}
        Mock Register-ResumeTask {}
        Mock Unregister-ResumeTask {}
        Mock Write-Host {}
    }

    It 'exits 0 without installing when the OS is already at the target build' {
        $script:Builds.Enqueue('26300.9974')
        Invoke-Run | Should -Be 0
        Should -Invoke Invoke-Dism -Times 0 -Exactly
    }

    It 'installs every package in install_order and exits 0 when the build is reached' {
        '26300.9711', '26300.9974' | ForEach-Object { $script:Builds.Enqueue($_) }
        Invoke-Run | Should -Be 0
        $script:Installed | Should -Be @('Windows11.0-KB5043080-arm64.msu', 'Windows11.0-KB5132061-arm64.msu')
        $saved = Get-Content (Join-Path $script:State 'updates-state.json') -Raw | ConvertFrom-Json
        $saved.done | Should -Be @('Windows11.0-KB5043080-arm64.msu', 'Windows11.0-KB5132061-arm64.msu')
        Get-Content (Join-Path $script:State 'install-updates.log') -Raw | Should -Match 'OS is at 26300.9974'
    }

    It 'skips a package that DISM says is not applicable' {
        '26300.9711', '26300.9974' | ForEach-Object { $script:Builds.Enqueue($_) }
        $script:DismCodes['Windows11.0-KB5043080-arm64.msu'] = -2146498530
        Invoke-Run | Should -Be 0
        $script:Installed.Count | Should -Be 2
    }

    It 'asks for a restart (3010) when the last package needs one' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes['Windows11.0-KB5132061-arm64.msu'] = 3010
        $script:Pending = $true
        Invoke-Run | Should -Be 3010
        Should -Invoke Invoke-Restart -Times 0 -Exactly
        Should -Invoke Register-ResumeTask -Times 0 -Exactly
    }

    It 'stops at a package blocked by a pending restart, then resumes after it' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes['Windows11.0-KB5043080-arm64.msu'] = 3010
        $script:DismCodes['Windows11.0-KB5132061-arm64.msu'] = -2146498511  # 0x800F0831
        $script:Pending = $true
        $Restart = $true
        $AutoResume = $true
        Invoke-Run | Should -Be 3010
        Should -Invoke Invoke-Restart -Times 1 -Exactly
        Should -Invoke Register-ResumeTask -Times 1 -Exactly
        Should -Invoke Unregister-ResumeTask -Times 0 -Exactly

        # After the restart: the first package is done, the second now installs.
        $script:Installed.Clear()
        $script:DismCodes.Clear()
        $script:Pending = $false
        $script:Builds.Clear()
        '26300.9711', '26300.9974' | ForEach-Object { $script:Builds.Enqueue($_) }
        Invoke-Run | Should -Be 0
        $script:Installed | Should -Be @('Windows11.0-KB5132061-arm64.msu')
        Should -Invoke Unregister-ResumeTask -Times 1 -Exactly
    }

    It 'fails with 1 after MaxRestarts restarts' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes['Windows11.0-KB5043080-arm64.msu'] = -2146498511
        $script:Pending = $true
        $MaxRestarts = 2
        Invoke-Run | Should -Be 3010
        Invoke-Run | Should -Be 3010
        Invoke-Run | Should -Be 1
    }

    It 'fails with 1 and removes the resume task on a DISM error with no restart pending' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes['Windows11.0-KB5043080-arm64.msu'] = -2146498302  # 0x800F0922
        $AutoResume = $true
        Invoke-Run | Should -Be 1
        $script:Installed.Count | Should -Be 1
        Should -Invoke Unregister-ResumeTask -Times 1 -Exactly
    }

    It 'fails with 1 when every package is processed but the build did not move' {
        $script:Builds.Enqueue('26300.9711')
        Invoke-Run | Should -Be 1
    }

    It 'refuses a package whose SHA-256 does not match' {
        $script:Builds.Enqueue('26300.9711')
        Add-Content -LiteralPath (Join-Path $Path 'Windows11.0-KB5043080-arm64.msu') -Value 'tampered'
        Invoke-Run | Should -Be 2
        Should -Invoke Invoke-Dism -Times 0 -Exactly
    }

    It 'refuses a package without a valid Microsoft signature' {
        $script:Builds.Enqueue('26300.9711')
        Mock Test-MicrosoftSignature { $false }
        Invoke-Run | Should -Be 2
        Should -Invoke Invoke-Dism -Times 0 -Exactly
    }

    It 'refuses a missing package' {
        $script:Builds.Enqueue('26300.9711')
        Remove-Item -LiteralPath (Join-Path $Path 'Windows11.0-KB5132061-arm64.msu')
        Invoke-Run | Should -Be 2
    }

    It 'refuses updates for another release' {
        $script:Builds.Enqueue('26100.4349')
        Invoke-Run | Should -Be 2
    }

    It 'refuses updates for another architecture' {
        $script:Builds.Enqueue('26300.9711')
        Mock Get-OsArchTag { 'x64' }
        Invoke-Run | Should -Be 2
        Should -Invoke Invoke-Dism -Times 0 -Exactly
    }

    It 'refuses to run unelevated' {
        $script:Builds.Enqueue('26300.9711')
        Mock Test-Elevated { $false }
        Invoke-Run | Should -Be 2
    }

    It 'rejects a manifest entry that is not a plain .msu file name' {
        $script:Builds.Enqueue('26300.9711')
        $Path = New-Fixture -Override @{
            packages      = @(@{ filename = '..\..\evil.msu'; sha256 = ('0' * 64) })
            install_order = @('..\..\evil.msu')
        }
        Invoke-Run | Should -Be 2
        Should -Invoke Invoke-Dism -Times 0 -Exactly
    }

    It 'rejects install_order entries missing from packages' {
        $script:Builds.Enqueue('26300.9711')
        $Path = New-Fixture -Override @{ install_order = @('Windows11.0-KB5000000-arm64.msu') }
        Invoke-Run | Should -Be 2
    }

    It 'rejects a missing or invalid updates.json' {
        $script:Builds.Enqueue('26300.9711')
        Set-Content -LiteralPath (Join-Path $Path 'updates.json') -Value '{not json'
        Invoke-Run | Should -Be 2
        $Path = Join-Path $TestDrive 'empty'
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        Invoke-Run | Should -Be 2
    }

    It 'dry run verifies everything and changes nothing' {
        $script:Builds.Enqueue('26300.9711')
        $DryRun = $true
        Invoke-Run | Should -Be 0
        Should -Invoke Invoke-Dism -Times 0 -Exactly
        Test-Path $script:State | Should -BeFalse
    }
}

Describe 'Compare-Build' {
    It 'orders builds numerically' {
        Compare-Build '26300.9711' '26300.9974' | Should -Be -1
        Compare-Build '26300.10001' '26300.9974' | Should -Be 1
        Compare-Build '26300.9974' '26300.9974' | Should -Be 0
    }
}

Describe 'Script file' {
    It 'is ASCII so Windows PowerShell 5.1 reads it correctly without a BOM' {
        $bytes = [IO.File]::ReadAllBytes($script:ScriptFile)
        @($bytes | Where-Object { $_ -gt 127 }).Count | Should -Be 0
    }
}
