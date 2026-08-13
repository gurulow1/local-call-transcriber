param(
    [switch]$CheckOnly,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
if (-not $CheckOnly) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator privileges are required to create firewall rules."
    }
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $pythonCandidates = @(
        @{
            Path = "$PSScriptRoot\..\.poetry-cache\venv\Scripts\python.exe"
            Name = "Local Call Transcriber - Block Outbound - archive Python"
        },
        @{
            Path = "$PSScriptRoot\..\.venv\Scripts\python.exe"
            Name = "Local Call Transcriber - Block Outbound"
        }
    )
}
else {
    $pythonCandidates = @(
        @{
            Path = $PythonPath
            Name = "Local Call Transcriber - Block Outbound - archive Python"
        }
    )
}

$targets = @()
foreach ($candidate in $pythonCandidates) {
    if (Test-Path -LiteralPath $candidate.Path -PathType Leaf) {
        $targets += @{
            Name = $candidate.Name
            Program = (Resolve-Path -LiteralPath $candidate.Path).Path
            Description = "Deny all outbound traffic for the local transcriber Python runtime."
        }
    }
}
if ($targets.Count -eq 0) {
    throw "No project Python runtime was found. Run the project bootstrap first."
}

$whisper = Get-ChildItem -LiteralPath "$PSScriptRoot\..\third_party\whisper.cpp" -Recurse -Filter whisper-cli.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($whisper) {
    $targets += @{
        Name = "Local Call Transcriber - Block Outbound - whisper.cpp"
        Program = $whisper.FullName
        Description = "Deny all outbound traffic for the local whisper.cpp runtime."
    }
}

if ($CheckOnly) {
    # NetSecurity/CIM can require elevation even for reads under hardened policies.
    $firewallPolicy = New-Object -ComObject HNetCfg.FwPolicy2
}

foreach ($target in $targets) {
    if ($CheckOnly) {
        $matches = @($firewallPolicy.Rules | Where-Object { $_.Name -eq $target.Name })
        if ($matches.Count -ne 1) {
            throw "Firewall rule '$($target.Name)' does not exist or is ambiguous."
        }
        $existing = $matches[0]
        if (
            $existing.ApplicationName -ne $target.Program -or
            $existing.Direction -ne 2 -or
            $existing.Action -ne 0 -or
            -not $existing.Enabled -or
            $existing.Profiles -ne 2147483647
        ) {
            throw "Firewall rule '$($target.Name)' exists with unexpected settings."
        }
        Write-Output "Verified outbound block for $($target.Program)"
        continue
    }

    $existingRules = @(Get-NetFirewallRule -DisplayName $target.Name -ErrorAction SilentlyContinue)
    if ($existingRules.Count -gt 0) {
        $applicationFilters = @($existingRules | Get-NetFirewallApplicationFilter)
        if (
            $existingRules.Count -ne 1 -or
            $applicationFilters.Count -ne 1 -or
            $applicationFilters[0].Program -ne $target.Program -or
            $existingRules[0].Direction -ne "Outbound" -or
            $existingRules[0].Action -ne "Block" -or
            $existingRules[0].Enabled -ne "True" -or
            $existingRules[0].Profile -ne "Any"
        ) {
            $existingRules | Remove-NetFirewallRule
            $existingRules = @()
        }
    }
    if ($existingRules.Count -eq 0) {
        New-NetFirewallRule `
            -DisplayName $target.Name `
            -Description $target.Description `
            -Direction Outbound `
            -Program $target.Program `
            -Action Block `
            -Profile Any `
            -Enabled True | Out-Null
    }
    Write-Output "Outbound network is blocked for $($target.Program)"
}
