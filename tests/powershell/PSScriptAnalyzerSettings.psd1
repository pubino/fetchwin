@{
    Severity     = @('Error', 'Warning', 'Information')
    ExcludeRules = @(
        # The script is run interactively and from unattended setup; console output is intended.
        'PSAvoidUsingWriteHost'
    )
    Rules        = @{
        PSUseCompatibleSyntax = @{
            Enable         = $true
            TargetVersions = @('5.1', '7.4')
        }
    }
}
