<#
    diskwatcher-proc.ps1 - process discovery shared by the start/stop PowerShell
    scripts. PowerShell port of lib/diskwatcher-proc.sh.

    Dot-source, never execute:  . "$PSScriptRoot\lib\diskwatcher-proc.ps1"

    Provides:
      Get-DwPidFromFile FILE      the live pid recorded in FILE, if any
      Get-DwRunningPids DIR       every diskwatcher.py pid launched from DIR
      Stop-DwPids TIMEOUT PIDS    graceful stop, then a forced kill after TIMEOUT

    As on POSIX, a pid is only believed once it is confirmed to be a live
    Python process whose command line is this application: pid numbers are
    recycled, so a stale pid file may name an unrelated process.
#>

# True if $ProcId is a live process whose command line is this application.
function Test-DwProcess {
    param([int]$ProcId, [string]$Dir)
    if ($ProcId -le 0) { return $false }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    $cmd = $p.CommandLine
    if (-not $cmd) { return $false }
    # Command line must reference the app...
    if ($cmd -notmatch 'diskwatcher\.py|mu2edaq_diskwatcher|mu2edaq-diskwatcher') { return $false }
    # ...and be a python process (mirrors the *[Pp]ython* check on POSIX).
    if ($cmd -notmatch '[Pp]ython') { return $false }
    # Scope to the launching directory when one is given: the start script
    # always launches with absolute paths under $Dir (the diskwatcher.py path
    # and/or --pid-file), so two checkouts on one host don't stop each other.
    if ($Dir) {
        $escaped = [regex]::Escape((Resolve-Path $Dir).Path)
        if ($cmd -notmatch $escaped) { return $false }
    }
    return $true
}

# The live pid recorded in $PidFile, if it is a diskwatcher; otherwise nothing.
function Get-DwPidFromFile {
    param([string]$PidFile, [string]$Dir)
    if (-not (Test-Path $PidFile)) { return }
    $raw = (Get-Content $PidFile -Raw) -replace '[^0-9]', ''
    if (-not $raw) { return }
    $ProcId = [int]$raw
    if (Test-DwProcess -ProcId $ProcId -Dir $Dir) { $ProcId }
}

# Every pid running diskwatcher.py out of directory $Dir.
function Get-DwRunningPids {
    param([string]$Dir)
    $me = $PID
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $me -and (Test-DwProcess -ProcId $_.ProcessId -Dir $Dir) } |
        ForEach-Object { $_.ProcessId }
}

# Graceful stop of every pid in $ProcIds, escalating to a forced kill after
# $TimeoutSec seconds.
function Stop-DwPids {
    param([int]$TimeoutSec, [int[]]$ProcIds)
    foreach ($ProcId in $ProcIds) {
        $p = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
        if ($p) { $p.CloseMainWindow() | Out-Null }
    }
    for ($i = 0; $i -lt $TimeoutSec; $i++) {
        $alive = $false
        foreach ($ProcId in $ProcIds) {
            if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) { $alive = $true }
        }
        if (-not $alive) { return }
        Start-Sleep -Seconds 1
    }
    foreach ($ProcId in $ProcIds) {
        if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) {
            Write-Host "  pid $ProcId did not exit within ${TimeoutSec}s; forcing"
            Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 1
}
