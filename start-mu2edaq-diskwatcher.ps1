<#
.SYNOPSIS
    Standardized Mu2e control-room start script (PowerShell port of
    start-mu2edaq-diskwatcher.sh) for the DAQ Disk Watcher.

.DESCRIPTION
    Starts the Disk Watcher web server, forwarding CRS_PORT_HTTP to
    diskwatcher.py as --port. Any copy already running out of this directory is
    stopped first (use -NoReplace to refuse to start instead).

    Note: POSIX daemon mode uses os.fork(), which is unavailable on Windows, so
    this launches the server as a background process via Start-Process and
    records its PID rather than passing --daemon.

    Port precedence: $env:CRS_PORT_HTTP > built-in default (5002).

    DW_DRY_RUN (env) is a test hook: it validates argument handling and exits
    before creating a venv or starting anything.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Config,
    [Alias('p')][int]$Port,
    [switch]$NoReplace,
    [string]$PidFile,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Extra
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
. (Join-Path $ScriptDir 'lib\diskwatcher-proc.ps1')

if (-not $Port) {
    $Port = if ($env:CRS_PORT_HTTP) { [int]$env:CRS_PORT_HTTP } else { 5002 }
}
if (-not $PidFile) { $PidFile = Join-Path $ScriptDir 'diskwatcher.pid' }
$StopTimeout = if ($env:CRS_STOP_TIMEOUT) { [int]$env:CRS_STOP_TIMEOUT } else { 10 }
if (-not $Config) { $Config = Join-Path $ScriptDir 'config\mu2edaq-diskwatcher.yaml' }

if (-not (Test-Path $Config)) {
    Write-Error "config file not found: $Config"
    exit 1
}

# ---- stop any copy already running out of this directory -------------------
$running = @()
$running += Get-DwPidFromFile -PidFile $PidFile -Dir $ScriptDir
$running += Get-DwRunningPids -Dir $ScriptDir
$running = $running | Sort-Object -Unique

if ($running.Count -gt 0) {
    if ($NoReplace) {
        Write-Error "DAQ Disk Watcher already running (pid $($running -join ' ')); not starting (-NoReplace)"
        exit 1
    }
    Write-Host "Found running DAQ Disk Watcher (pid $($running -join ' ')); stopping it first"
    if (-not $env:DW_DRY_RUN) {
        Stop-DwPids -TimeoutSec $StopTimeout -ProcIds $running
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host 'Previous instance stopped'
    }
}

# DW_DRY_RUN: validate args without a venv or a running daemon (test hook).
if ($env:DW_DRY_RUN) {
    Write-Host "Starting DAQ Disk Watcher (http=$Port, config: $Config)"
    exit 0
}

$VenvPy = Join-Path $ScriptDir 'venv\Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Write-Error 'virtual environment not found; run .\bootstrap-diskwatcher.ps1 first'
    exit 1
}
$env:PYTHONPATH = (Join-Path $ScriptDir 'src') + ';' + $env:PYTHONPATH

Write-Host "Starting DAQ Disk Watcher (http=$Port, config: $Config)"
$argList = @((Join-Path $ScriptDir 'diskwatcher.py'), '--config', $Config, '--port', "$Port")
if ($Extra) { $argList += $Extra }
$log = Join-Path $ScriptDir 'logs\diskwatcher.out'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
$proc = Start-Process -FilePath $VenvPy -ArgumentList $argList -WorkingDirectory $ScriptDir `
    -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru
Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii
Write-Host "DAQ Disk Watcher started (PID $($proc.Id), http=$Port); pid file: $PidFile"
