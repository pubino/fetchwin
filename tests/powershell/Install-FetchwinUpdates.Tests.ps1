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
            target        = 'Windows11.0-KB5132061-arm64.msu'
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
        $script:DismCodes = [System.Collections.Generic.Queue[int]]::new()
        $script:Installed = [System.Collections.Generic.List[string]]::new()

        $Path = New-Fixture
        $Restart = $false
        $AutoResume = $false
        $DryRun = $false
        $MaxRestarts = 4
        $Target = 'Windows11.0-KB5132061-arm64.msu'

        Mock Get-StateDirectory { $script:State }
        Mock Get-OsBuild {
            if ($script:Builds.Count -gt 1) { $script:Builds.Dequeue() } else { $script:Builds.Peek() }
        }
        Mock Get-OsArchTag { 'arm64' }
        Mock Test-Elevated { $true }
        Mock Test-RebootPending { $script:Pending }
        Mock Test-MicrosoftSignature { $true }
        Mock Invoke-Dism {
            $script:Installed.Add((Split-Path -Leaf $Package))
            if ($script:DismCodes.Count) { return $script:DismCodes.Dequeue() }
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

    It 'installs only the target; DISM takes the checkpoints from the same folder' {
        '26300.9711', '26300.9974' | ForEach-Object { $script:Builds.Enqueue($_) }
        Invoke-Run | Should -Be 0
        $script:Installed | Should -Be @($Target)
        Get-Content (Join-Path $script:State 'install-updates.log') -Raw | Should -Match 'OS is at 26300.9974'
    }

    It 'installs the package updates.json names as the target' {
        '26300.9711', '26300.9974' | ForEach-Object { $script:Builds.Enqueue($_) }
        $Path = New-Fixture -Override @{ target = 'Windows11.0-KB5043080-arm64.msu' }
        Invoke-Run | Should -Be 0
        $script:Installed | Should -Be @('Windows11.0-KB5043080-arm64.msu')
    }

    It 'asks for a restart on DISM 3010 even when Windows shows no pending restart' {
        # Regression (VM run 2026-11-12): DISM returned 3010 for the target but the CBS
        # RebootPending key was not set, so the script went on to fail its build check.
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes.Enqueue(3010)
        Invoke-Run | Should -Be 3010
        Should -Invoke Invoke-Restart -Times 0 -Exactly
    }

    It 'restarts and resumes by itself with -Restart -AutoResume, then finishes' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes.Enqueue(3010)
        $Restart = $true
        $AutoResume = $true
        Invoke-Run | Should -Be 3010
        Should -Invoke Invoke-Restart -Times 1 -Exactly
        Should -Invoke Register-ResumeTask -Times 1 -Exactly
        Should -Invoke Unregister-ResumeTask -Times 0 -Exactly

        # After the restart the build is current: nothing more to install.
        $script:Builds.Clear(); $script:Builds.Enqueue('26300.9974')
        Invoke-Run | Should -Be 0
        $script:Installed.Count | Should -Be 1
        Should -Invoke Unregister-ResumeTask -Times 1 -Exactly
    }

    It 'fails after the restart if the target installed but the build did not move' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes.Enqueue(3010)
        Invoke-Run | Should -Be 3010
        Invoke-Run | Should -Be 1
        $script:Installed.Count | Should -Be 1  # not installed again
    }

    It 'restarts first when an earlier restart blocks the install, then installs' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes.Enqueue(-2146498511)  # 0x800F0831
        $script:Pending = $true
        Invoke-Run | Should -Be 3010

        $script:Pending = $false
        $script:Builds.Clear()
        '26300.9711', '26300.9974' | ForEach-Object { $script:Builds.Enqueue($_) }
        Invoke-Run | Should -Be 0
        $script:Installed | Should -Be @($Target, $Target)
    }

    It 'fails with 1 after MaxRestarts restarts' {
        $script:Builds.Enqueue('26300.9711')
        1..3 | ForEach-Object { $script:DismCodes.Enqueue(-2146498511) }
        $script:Pending = $true
        $MaxRestarts = 2
        Invoke-Run | Should -Be 3010
        Invoke-Run | Should -Be 3010
        Invoke-Run | Should -Be 1
        Get-Content (Join-Path $script:State 'install-updates.log') -Raw | Should -Match 'after 2 restarts'
    }

    It 'fails with 1 and removes the resume task on a DISM error with no restart pending' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes.Enqueue(-2147024344)  # 0x80070228, the error from installing KB5043080
        $AutoResume = $true
        Invoke-Run | Should -Be 1
        Should -Invoke Unregister-ResumeTask -Times 1 -Exactly
    }

    It 'fails with 1 when DISM says the target does not apply and the build is old' {
        $script:Builds.Enqueue('26300.9711')
        $script:DismCodes.Enqueue(-2146498530)
        Invoke-Run | Should -Be 1
    }

    It 'checks the checkpoint packages too: a bad hash on one stops the install' {
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
        Remove-Item -LiteralPath (Join-Path $Path 'Windows11.0-KB5043080-arm64.msu')
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
            packages = @(@{ filename = '..\..\evil.msu'; sha256 = ('0' * 64) })
            target   = '..\..\evil.msu'
        }
        Invoke-Run | Should -Be 2
        Should -Invoke Invoke-Dism -Times 0 -Exactly
    }

    It 'rejects a target that is not one of the packages' {
        $script:Builds.Enqueue('26300.9711')
        $Path = New-Fixture -Override @{ target = 'Windows11.0-KB5000000-arm64.msu' }
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
