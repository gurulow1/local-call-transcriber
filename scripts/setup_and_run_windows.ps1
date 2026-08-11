# One-click staging and startup for the verified Windows x64 / NVIDIA pilot.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version Latest

function Write-Step {
    param([string] $Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [string] $FilePath,
        [string[]] $ArgumentList,
        [string] $FailureMessage
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Test-PythonCommand {
    param(
        [string] $FilePath,
        [string[]] $PrefixArguments = @()
    )
    try {
        $probeArguments = @($PrefixArguments) + @(
            "-c",
            "import struct,sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}|{struct.calcsize(`"P`") * 8}')"
        )
        $probeOutput = @(& $FilePath @probeArguments 2>$null)
        if ($LASTEXITCODE -ne 0 -or $probeOutput.Count -eq 0) {
            return $false
        }
        return $probeOutput[-1].Trim() -eq "3.11|64"
    }
    catch {
        return $false
    }
}

function Test-ExternalCommand {
    param(
        [string] $FilePath,
        [string[]] $ArgumentList
    )
    try {
        & $FilePath @ArgumentList *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Assert-DownloadedFile {
    param(
        [string] $Path,
        [long] $ExpectedSize,
        [string] $ExpectedSha256
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Downloaded file is missing: $Path"
    }
    $file = Get-Item -LiteralPath $Path
    if ($file.Length -ne $ExpectedSize) {
        throw "Size mismatch for $($file.Name): expected $ExpectedSize, got $($file.Length)"
    }
    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $ExpectedSha256) {
        throw "SHA-256 mismatch for $($file.Name): $actualHash"
    }
}

try {
    if ($env:OS -ne "Windows_NT" -or -not [Environment]::Is64BitOperatingSystem) {
        throw "This launcher supports only 64-bit Windows."
    }

    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    Set-Location -LiteralPath $projectRoot

    $runtimeRoot = Join-Path $projectRoot ".runtime"
    $cacheRoot = Join-Path $projectRoot ".runtime-cache"
    $downloadRoot = Join-Path $cacheRoot "downloads"
    $wheelhouse = Join-Path $cacheRoot "wheelhouse-win-cp311"
    $managedPythonRoot = Join-Path $runtimeRoot "python-3.11.9"
    $managedPython = Join-Path $managedPythonRoot "python.exe"
    $venvRoot = Join-Path $projectRoot ".venv"
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    $requirements = Join-Path $projectRoot "requirements\windows-whisper.txt"
    $modelRoot = Join-Path $projectRoot "models\whisper-large-v3"
    $modelFile = Join-Path $modelRoot "ggml-large-v3.bin"
    $inputRoot = Join-Path $projectRoot "data\input"

    New-Item -ItemType Directory -Force -Path $runtimeRoot, $cacheRoot, $downloadRoot, $wheelhouse | Out-Null

    Write-Step "Checking disk space and NVIDIA runtime"
    $driveRoot = [System.IO.Path]::GetPathRoot($projectRoot)
    $drive = [System.IO.DriveInfo]::new($driveRoot)
    $minimumFreeBytes = 6GB
    if ($drive.AvailableFreeSpace -lt $minimumFreeBytes) {
        $freeGiB = [Math]::Round($drive.AvailableFreeSpace / 1GB, 1)
        throw "At least 6 GiB of free disk space is required; only $freeGiB GiB is available."
    }

    $nvidiaSmiCommand = Get-Command "nvidia-smi.exe" -ErrorAction SilentlyContinue
    $nvidiaSmiPath = if ($null -ne $nvidiaSmiCommand) { $nvidiaSmiCommand.Source } else { $null }
    if ($null -eq $nvidiaSmiPath) {
        $fallbackNvidiaSmi = Join-Path $env:ProgramFiles "NVIDIA Corporation\NVSMI\nvidia-smi.exe"
        if (Test-Path -LiteralPath $fallbackNvidiaSmi -PathType Leaf) {
            $nvidiaSmiPath = $fallbackNvidiaSmi
        }
    }
    if ($null -eq $nvidiaSmiPath) {
        throw "NVIDIA driver was not found. The verified Whisper pilot requires a Windows x64 NVIDIA host."
    }
    $gpuInfo = @(& $nvidiaSmiPath --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "nvidia-smi failed. Install or repair the NVIDIA driver before retrying."
    }
    Write-Host "GPU: $($gpuInfo -join '; ')"

    Write-Step "Locating a 64-bit Python 3.11 runtime"
    $pythonCommand = $null
    $pythonPrefix = @()

    if (Test-PythonCommand -FilePath $managedPython) {
        $pythonCommand = $managedPython
    }
    if ($null -eq $pythonCommand) {
        $pythonLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
        if ($null -ne $pythonLauncher -and (Test-PythonCommand -FilePath $pythonLauncher.Source -PrefixArguments @("-3.11"))) {
            $pythonCommand = $pythonLauncher.Source
            $pythonPrefix = @("-3.11")
        }
    }
    if ($null -eq $pythonCommand) {
        $systemPython = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if ($null -ne $systemPython -and (Test-PythonCommand -FilePath $systemPython.Source)) {
            $pythonCommand = $systemPython.Source
        }
    }

    if ($null -eq $pythonCommand) {
        Write-Step "Downloading the pinned official Python 3.11.9 installer"
        $pythonUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
        $pythonSize = 26216840
        $pythonSha256 = "5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde"
        $pythonInstaller = Join-Path $downloadRoot "python-3.11.9-amd64.exe"

        $installerReady = $false
        if (Test-Path -LiteralPath $pythonInstaller -PathType Leaf) {
            try {
                Assert-DownloadedFile -Path $pythonInstaller -ExpectedSize $pythonSize -ExpectedSha256 $pythonSha256
                $installerReady = $true
            }
            catch {
                Remove-Item -LiteralPath $pythonInstaller -Force
            }
        }
        if (-not $installerReady) {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri $pythonUrl -OutFile $pythonInstaller
            Assert-DownloadedFile -Path $pythonInstaller -ExpectedSize $pythonSize -ExpectedSha256 $pythonSha256
        }

        if (Test-Path -LiteralPath $managedPythonRoot) {
            Remove-Item -LiteralPath $managedPythonRoot -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $managedPythonRoot | Out-Null
        $installArguments = @(
            "/quiet",
            "InstallAllUsers=0",
            "TargetDir=`"$managedPythonRoot`"",
            "Include_exe=1",
            "Include_lib=1",
            "Include_pip=1",
            "Include_launcher=0",
            "InstallLauncherAllUsers=0",
            "Include_test=0",
            "Include_doc=0",
            "Include_tcltk=0",
            "AssociateFiles=0",
            "Shortcuts=0",
            "PrependPath=0"
        )
        $installerProcess = Start-Process -FilePath $pythonInstaller -ArgumentList $installArguments -Wait -PassThru
        if ($installerProcess.ExitCode -notin @(0, 3010) -or -not (Test-PythonCommand -FilePath $managedPython)) {
            throw "The verified Python installer did not create a usable 64-bit Python 3.11 runtime."
        }
        $pythonCommand = $managedPython
        $pythonPrefix = @()
    }

    Write-Step "Creating the isolated Python environment"
    if (-not (Test-PythonCommand -FilePath $venvPython)) {
        if (Test-Path -LiteralPath $venvRoot) {
            Remove-Item -LiteralPath $venvRoot -Recurse -Force
        }
        $venvArguments = @($pythonPrefix) + @("-m", "venv", $venvRoot)
        Invoke-Checked -FilePath $pythonCommand -ArgumentList $venvArguments -FailureMessage "Cannot create .venv"
    }
    if (-not (Test-PythonCommand -FilePath $venvPython)) {
        throw "The created .venv is not a usable 64-bit Python 3.11 environment."
    }

    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
    $env:PIP_NO_INPUT = "1"
    $env:PYTHONUTF8 = "1"

    Write-Step "Installing hash-pinned local audio dependencies"
    $miniaudioWheel = Join-Path $wheelhouse "miniaudio-1.61-cp311-cp311-win_amd64.whl"
    $numpyWheel = Join-Path $wheelhouse "numpy-1.26.4-cp311-cp311-win_amd64.whl"
    $pyavWheel = Join-Path $wheelhouse "av-18.0.0-cp311-abi3-win_amd64.whl"
    if (-not (Test-Path -LiteralPath $miniaudioWheel -PathType Leaf) -or
        -not (Test-Path -LiteralPath $numpyWheel -PathType Leaf) -or
        -not (Test-Path -LiteralPath $pyavWheel -PathType Leaf)) {
        Invoke-Checked -FilePath $venvPython -ArgumentList @(
            "-m", "pip", "download",
            "--only-binary=:all:",
            "--no-deps",
            "--require-hashes",
            "--dest", $wheelhouse,
            "-r", $requirements
        ) -FailureMessage "Cannot download the pinned Windows wheels"
    }
    Invoke-Checked -FilePath $venvPython -ArgumentList @(
        "-m", "pip", "install",
        "--no-index",
        "--only-binary=:all:",
        "--no-deps",
        "--require-hashes",
        "--find-links", $wheelhouse,
        "-r", $requirements
    ) -FailureMessage "Cannot install the pinned Windows wheels"
    Invoke-Checked -FilePath $venvPython -ArgumentList @("-m", "pip", "check") -FailureMessage "pip dependency check failed"

    Write-Step "Checking the pinned Whisper runtime and large-v3 model"
    $whisperRuntimeRoot = Join-Path $projectRoot "third_party\whisper.cpp"
    $whisperCli = $null
    if (Test-Path -LiteralPath $whisperRuntimeRoot -PathType Container) {
        $whisperCli = Get-ChildItem -LiteralPath $whisperRuntimeRoot -Recurse -Filter "whisper-cli.exe" -File |
            Select-Object -First 1
    }
    $modelReady = Test-ExternalCommand -FilePath $venvPython -ArgumentList @(
        (Join-Path $projectRoot "scripts\verify_model.py"),
        $modelRoot
    )
    $runtimeReady = $null -ne $whisperCli -and
        (Test-ExternalCommand -FilePath $whisperCli.FullName -ArgumentList @("--version"))

    if (-not $modelReady -or -not $runtimeReady) {
        Write-Step "Downloading and verifying whisper.cpp plus Whisper large-v3 (about 3.8 GB)"
        Invoke-Checked -FilePath $venvPython -ArgumentList @(
            (Join-Path $projectRoot "scripts\prepare_whisper_cpp.py"),
            "--allow-network-download"
        ) -FailureMessage "Whisper staging failed"
        $whisperCli = Get-ChildItem -LiteralPath $whisperRuntimeRoot -Recurse -Filter "whisper-cli.exe" -File |
            Select-Object -First 1
        if ($null -eq $whisperCli) {
            throw "The verified whisper.cpp archive did not contain whisper-cli.exe."
        }
    }

    Invoke-Checked -FilePath $venvPython -ArgumentList @(
        (Join-Path $projectRoot "scripts\verify_model.py"),
        $modelRoot
    ) -FailureMessage "Whisper model verification failed"
    Invoke-Checked -FilePath $whisperCli.FullName -ArgumentList @("--version") -FailureMessage "whisper-cli cannot start"

    Write-Step "Running the complete local test suite"
    Invoke-Checked -FilePath $venvPython -ArgumentList @(
        "-m", "unittest", "discover", "-s", "tests", "-v"
    ) -FailureMessage "Automated tests failed"

    Write-Step "Applying outbound firewall rules (Windows will ask for administrator approval)"
    $firewallScript = Join-Path $projectRoot "security\windows-deny-network.ps1"
    $elevatedPowerShell = Join-Path $PSHOME "powershell.exe"
    $firewallArguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$firewallScript`""
    try {
        $firewallProcess = Start-Process -FilePath $elevatedPowerShell -Verb RunAs -ArgumentList $firewallArguments -Wait -PassThru
    }
    catch {
        throw "Administrator approval for the firewall was not granted. Setup is complete, but the worker was not started."
    }
    if ($firewallProcess.ExitCode -ne 0) {
        throw "Firewall setup failed with exit code $($firewallProcess.ExitCode). The worker was not started."
    }

    New-Item -ItemType Directory -Force -Path $inputRoot | Out-Null
    Write-Step "Setup complete"
    Write-Host "The worker will now watch: $inputRoot" -ForegroundColor Green
    Write-Host "Drop a synthetic or approved WAV, MP3, FLAC, OGG, or AAC file into that folder."
    Write-Host "Results will appear under: $(Join-Path $projectRoot 'data\calls')"
    Write-Host "Do not use real corporate calls until access, retention, and security review are approved."
    Write-Host "Keep this window open. Press Ctrl+C to stop the worker."

    Start-Process -FilePath "explorer.exe" -ArgumentList "`"$inputRoot`""
}
catch {
    Write-Host ""
    Write-Host "SETUP FAILED" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

& $venvPython (Join-Path $projectRoot "worker.py") --mode poll --engine whisper --decoder beam_search
exit $LASTEXITCODE
