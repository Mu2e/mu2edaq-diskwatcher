<#
.SYNOPSIS
    Initialize a Disk Watcher install directory on Windows (PowerShell port of
    bootstrap_diskwatcher.sh): create a venv, install dependencies and the
    package, and create the data/logs/config directories.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

# Prefer 'python'; fall back to the py launcher ('python3' on Windows is the
# Microsoft Store alias stub, so it is not used here).
$Python = $env:PYTHON
if (-not $Python) {
    if (Get-Command python -ErrorAction SilentlyContinue) { $Python = 'python' }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { $Python = 'py' }
    else { Write-Error 'Python 3.9+ not found on PATH. Install it first.'; exit 1 }
}

& $Python -m venv venv
$VenvPy = Join-Path $Here 'venv\Scripts\python.exe'

& $VenvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error 'dependency installation failed; environment is NOT usable.'; exit 1 }

& $VenvPy -m pip install -e .
if ($LASTEXITCODE -ne 0) { Write-Error 'could not install mu2edaq-diskwatcher; environment is NOT usable.'; exit 1 }

# mu2edaq-discovery (auto-discovery protocol) -- best effort.
& $VenvPy -c 'import mu2edaq_discovery' 2>$null
if ($LASTEXITCODE -ne 0) {
    $sibling = Join-Path $Here '..\mu2edaq-discovery'
    if (Test-Path $sibling) {
        & $VenvPy -m pip install -e $sibling
        if ($LASTEXITCODE -eq 0) { Write-Host 'Installed mu2edaq-discovery from sibling checkout' }
    } else {
        & $VenvPy -m pip install 'git+https://github.com/Mu2e/mu2edaq-discovery' 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Host 'Installed mu2edaq-discovery from GitHub' }
        else { Write-Host 'note: mu2edaq-discovery not installed; auto-discovery disabled' }
    }
}

New-Item -ItemType Directory -Force -Path data, logs, config | Out-Null
Write-Host 'Diskwatcher server environment initialized successfully.'
