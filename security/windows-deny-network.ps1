#requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$ruleName = "Local Call Transcriber - Block Outbound"
$python = (Resolve-Path -LiteralPath "$PSScriptRoot\..\.venv\Scripts\python.exe").Path
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue

if ($existing) {
    $program = ($existing | Get-NetFirewallApplicationFilter).Program
    if ($program -ne $python -or $existing.Direction -ne "Outbound" -or $existing.Action -ne "Block") {
        throw "Firewall rule '$ruleName' exists with unexpected settings."
    }
} else {
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Description "Deny all outbound traffic for the local call transcriber runtime Python." `
        -Direction Outbound `
        -Program $python `
        -Action Block `
        -Profile Any `
        -Enabled True | Out-Null
}

Write-Output "Outbound network is blocked for $python"
