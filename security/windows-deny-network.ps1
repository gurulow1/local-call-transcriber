#requires -RunAsAdministrator

param(
    [string] $PythonPath
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $ManagedPython = "$PSScriptRoot\..\.poetry-cache\venv\Scripts\python.exe"
    $DeveloperPython = "$PSScriptRoot\..\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $ManagedPython -PathType Leaf) {
        $PythonPath = $ManagedPython
    }
    elseif (Test-Path -LiteralPath $DeveloperPython -PathType Leaf) {
        $PythonPath = $DeveloperPython
    }
    else {
        throw "Local transcriber Python runtime was not found."
    }
}
$python = (Resolve-Path -LiteralPath $PythonPath).Path
$targets = @(
    @{
        Name = "Local Call Transcriber - Block Outbound"
        Program = $python
        Description = "Deny all outbound traffic for the local transcriber Python runtime."
    }
)
$whisper = Get-ChildItem -LiteralPath "$PSScriptRoot\..\third_party\whisper.cpp" -Recurse -Filter whisper-cli.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($whisper) {
    $targets += @{
        Name = "Local Call Transcriber - Block Outbound - whisper.cpp"
        Program = $whisper.FullName
        Description = "Deny all outbound traffic for the local whisper.cpp runtime."
    }
}

foreach ($target in $targets) {
    $existing = Get-NetFirewallRule -DisplayName $target.Name -ErrorAction SilentlyContinue
    if ($existing) {
        $existingRules = @($existing)
        $applicationFilters = @($existing | Get-NetFirewallApplicationFilter)
        $program = $applicationFilters.Program
        if (
            $existingRules.Count -ne 1 -or
            $applicationFilters.Count -ne 1 -or
            $program -ne $target.Program -or
            $existing.Direction -ne "Outbound" -or
            $existing.Action -ne "Block" -or
            $existing.Enabled -ne "True" -or
            $existing.Profile -ne "Any"
        ) {
            $existing | Remove-NetFirewallRule
            $existing = $null
        }
    }
    if (-not $existing) {
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
