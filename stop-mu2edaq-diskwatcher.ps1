<#
.SYNOPSIS
    Standardized Mu2e control-room stop script (PowerShell port of
    stop-mu2edaq-diskwatcher.sh) for the DAQ Disk Watcher.

.DESCRIPTION
    Stops every copy running out of this directory (not just the one named in
    the pid file): a process whose pid file was deleted, or a copy started by
    hand, would otherwise survive a stop and hold the port against the next
    start. Graceful close, then a forced kill after a timeout.

.PARAMETER PidFile
    Path to the pid file. Defaults to diskwatcher.pid next to this script.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$PidFile
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib\diskwatcher-proc.ps1')

if (-not $PidFile) { $PidFile = Join-Path $ScriptDir 'diskwatcher.pid' }
$Timeout = if ($env:CRS_STOP_TIMEOUT) { [int]$env:CRS_STOP_TIMEOUT } else { 10 }

$running = @()
$running += Get-DwPidFromFile -PidFile $PidFile -Dir $ScriptDir
$running += Get-DwRunningPids -Dir $ScriptDir
$running = $running | Sort-Object -Unique

if ($running.Count -eq 0) {
    if (Test-Path $PidFile) {
        Write-Host 'DAQ Disk Watcher not running (stale pid file); cleaning up'
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "DAQ Disk Watcher not running (no pid file: $PidFile)"
    }
    exit 0
}

Write-Host "Stopping DAQ Disk Watcher (pid $($running -join ' '))..."
Stop-DwPids -TimeoutSec $Timeout -ProcIds $running
Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Write-Host 'DAQ Disk Watcher stopped'
