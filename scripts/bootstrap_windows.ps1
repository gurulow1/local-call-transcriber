$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectDir
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir "worker.py"))) {
    throw "Launcher is not inside an unpacked project folder."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Windows x64 is required."
}

$CacheDir = Join-Path $ProjectDir ".poetry-cache"
$BinDir = Join-Path $CacheDir "bin"
$DownloadDir = Join-Path $CacheDir "downloads"
$LogDir = Join-Path $ProjectDir "logs"
$InputDir = Join-Path $ProjectDir "data\input"
$UvVersion = "0.11.13"
$UvTarget = "x86_64-pc-windows-msvc"
$UvHash = "0953ac2ef4fbe47ad469bfa80b658a577a02c4d73a2fb9c4c7c70dda432efded"
$UvArchive = Join-Path $DownloadDir "uv-$UvVersion-$UvTarget.zip"
$UvExe = Join-Path $BinDir "uv.exe"
$VenvDir = Join-Path $CacheDir "venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $BinDir, $DownloadDir, $LogDir, $InputDir | Out-Null
Start-Transcript -Path (Join-Path $LogDir "setup.log") -Append | Out-Null

try {
    $UvReady = (Test-Path -LiteralPath $UvExe)
    if ($UvReady) {
        $UvOutput = & $UvExe --version 2>$null
        $UvReady = ($LASTEXITCODE -eq 0 -and $UvOutput -eq "uv $UvVersion")
    }
    if (-not $UvReady) {
        $ArchiveReady = (Test-Path -LiteralPath $UvArchive)
        if ($ArchiveReady) {
            $ArchiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $UvArchive).Hash.ToLowerInvariant()
            $ArchiveReady = ($ArchiveHash -eq $UvHash)
        }
        if (-not $ArchiveReady) {
            Write-Host "Downloading verified bootstrap runtime..."
            $PartialArchive = "$UvArchive.part"
            Remove-Item -LiteralPath $PartialArchive -Force -ErrorAction SilentlyContinue
            Invoke-WebRequest `
                -UseBasicParsing `
                -Uri "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-$UvTarget.zip" `
                -OutFile $PartialArchive
            $ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $PartialArchive).Hash.ToLowerInvariant()
            if ($ActualHash -ne $UvHash) {
                throw "SHA-256 mismatch for uv bootstrap."
            }
            Move-Item -LiteralPath $PartialArchive -Destination $UvArchive -Force
        }
        $StageDir = Join-Path $CacheDir "uv-stage"
        if (-not $StageDir.StartsWith($ProjectDir + [IO.Path]::DirectorySeparatorChar)) {
            throw "Unsafe bootstrap staging path."
        }
        Remove-Item -LiteralPath $StageDir -Recurse -Force -ErrorAction SilentlyContinue
        Expand-Archive -LiteralPath $UvArchive -DestinationPath $StageDir -Force
        $ExtractedUv = Join-Path $StageDir "uv.exe"
        if (-not (Test-Path -LiteralPath $ExtractedUv)) {
            throw "uv.exe is missing from the verified archive."
        }
        Copy-Item -LiteralPath $ExtractedUv -Destination $UvExe -Force
        Remove-Item -LiteralPath $StageDir -Recurse -Force
    }

    $env:UV_CACHE_DIR = Join-Path $CacheDir "uv-cache"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $CacheDir "python"
    $env:UV_NO_CONFIG = "1"

    $PythonReady = (Test-Path -LiteralPath $PythonExe)
    if ($PythonReady) {
        & $PythonExe -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" 2>$null
        $PythonReady = ($LASTEXITCODE -eq 0)
    }
    if (-not $PythonReady) {
        if (-not $VenvDir.StartsWith($ProjectDir + [IO.Path]::DirectorySeparatorChar)) {
            throw "Unsafe virtual environment path."
        }
        Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
        & $UvExe venv --python 3.12 --managed-python $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to create the local Python environment."
        }
    }

    & $PythonExe scripts\bootstrap_runtime.py --uv $UvExe --profile windows-auto --prepare-only
    if ($LASTEXITCODE -ne 0) {
        throw "Transcriber setup stopped with exit code $LASTEXITCODE."
    }

    Write-Host "Applying outbound firewall rules (Windows will request administrator approval)..."
    $FirewallScript = Join-Path $ProjectDir "security\windows-deny-network.ps1"
    $ElevatedPowerShell = Join-Path $PSHOME "powershell.exe"
    $FirewallArguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + `
        $FirewallScript + '" -PythonPath "' + $PythonExe + '"'
    try {
        $FirewallProcess = Start-Process `
            -FilePath $ElevatedPowerShell `
            -Verb RunAs `
            -ArgumentList $FirewallArguments `
            -Wait `
            -PassThru
    }
    catch {
        throw "Administrator approval for the outbound firewall was not granted. The worker was not started."
    }
    if ($FirewallProcess.ExitCode -ne 0) {
        throw "Outbound firewall setup failed with exit code $($FirewallProcess.ExitCode). The worker was not started."
    }

    & $PythonExe scripts\bootstrap_runtime.py --uv $UvExe --profile windows-auto
    if ($LASTEXITCODE -ne 0) {
        throw "Transcriber stopped with exit code $LASTEXITCODE."
    }
}
catch {
    Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    throw
}
finally {
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
}
