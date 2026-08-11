#requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$python = (Resolve-Path -LiteralPath "$PSScriptRoot\..\.venv\Scripts\python.exe").Path
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
        $program = ($existing | Get-NetFirewallApplicationFilter).Program
        if (
            $program -ne $target.Program -or
            $existing.Direction -ne "Outbound" -or
            $existing.Action -ne "Block" -or
            $existing.Enabled -ne "True" -or
            $existing.Profile -ne "Any"
        ) {
            throw "Firewall rule '$($target.Name)' exists with unexpected settings."
        }
    } else {
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
