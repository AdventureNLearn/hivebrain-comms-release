# Append text to a live bus file under the shared <file>.lock.
# Invoke with: powershell -NoProfile -ExecutionPolicy Bypass -File tools/Append-BusJob.ps1
# Windows PowerShell 5.1 compatible. Does not change the execution policy.
# A prose append that skips this helper can still lose text in the archive replace window (P5a).
# Exit 3 = ERROR (fail closed).
param(
    [Parameter(Mandatory = $true)][string]$File,
    [Parameter(Mandatory = $true)][string]$Text
)
$ErrorActionPreference = "Stop"
$ExitError = 3

function Write-FailClosed([string]$verdict) {
    if (-not $verdict) { $verdict = "REFUSED ERROR" }
    Write-Output $verdict
    exit $ExitError
}

function Test-Reparse([string]$path) {
    if (-not $path) { return $false }
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    $item = Get-Item -LiteralPath $path -Force
    return [bool]($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
}

function Get-AllowedParents {
    $hubRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    return @(
        [System.IO.Path]::GetFullPath((Join-Path $hubRoot "bus")),
        [System.IO.Path]::GetFullPath((Join-Path $hubRoot "handoff"))
    )
}

function Test-ParentAllowed([string]$path, $allowed) {
    if ([string]::IsNullOrWhiteSpace($path)) { return $false }
    $full = [System.IO.Path]::GetFullPath($path)
    $parent = Split-Path -Parent $full
    if (-not $parent) { return $false }
    $parent = [System.IO.Path]::GetFullPath($parent)
    foreach ($dir in $allowed) {
        if ($parent -ieq $dir) { return $true }
    }
    return $false
}

function Test-BusFile([string]$path, $allowed) {
    if ([string]::IsNullOrWhiteSpace($path)) { return $false }
    $parts = @($path -split '[\\/]')
    if ($parts -contains "..") { return $false }
    $leaf = Split-Path -Leaf $path
    if (-not $leaf -or -not $leaf.ToLower().EndsWith(".md")) { return $false }
    if (-not (Test-ParentAllowed $path $allowed)) { return $false }
    $full = [System.IO.Path]::GetFullPath($path)
    if (Test-Reparse $full) { return $false }
    $parent = [System.IO.Path]::GetFullPath((Split-Path -Parent $full))
    if (Test-Reparse $parent) { return $false }
    return $true
}

function Write-RandomBusTemp([string]$dir, [string]$content) {
    if (-not $dir -or (Test-Reparse $dir)) { Write-FailClosed "REFUSED ERROR" }
    $tries = 0
    while ($tries -lt 8) {
        $tries++
        $name = [guid]::NewGuid().ToString("N") + ".tmp"
        $candidate = Join-Path $dir $name
        if (Test-Path -LiteralPath $candidate) { continue }
        if (Test-Reparse $candidate) { continue }
        $stream = $null
        try {
            $stream = [System.IO.File]::Open(
                $candidate,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($content)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush()
            return $candidate
        } catch {
            if (Test-Path -LiteralPath $candidate) {
                Remove-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
            }
            continue
        } finally {
            if ($stream) { $stream.Dispose() }
        }
    }
    Write-FailClosed "REFUSED ERROR"
}

function Wait-BusLock([string]$path, [int]$timeoutSec = 5) {
    if (Test-Reparse $path) { throw "lock is a reparse point" }
    $deadline = [datetime]::UtcNow.AddSeconds($timeoutSec)
    while ($true) {
        try {
            $opened = [System.IO.File]::Open(
                $path,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            if (Test-Reparse $path) {
                $opened.Close()
                throw "lock is a reparse point"
            }
            return $opened
        } catch {
            if (Test-Reparse $path) { throw "lock is a reparse point" }
            if ([datetime]::UtcNow -ge $deadline) { throw "bus file lock timeout" }
            Start-Sleep -Milliseconds 50
        }
    }
}

$allowed = Get-AllowedParents
try {
    if (-not (Test-BusFile $File $allowed)) { Write-FailClosed "REFUSED ERROR" }

    $lockPath = "$File.lock"
    if (Test-Reparse $lockPath) { Write-FailClosed "REFUSED ERROR" }
    if (Test-Reparse $File) { Write-FailClosed "REFUSED ERROR" }

    $full = [System.IO.Path]::GetFullPath($File)
    $parent = [System.IO.Path]::GetFullPath((Split-Path -Parent $full))
    $lockStream = $null
    $tmp = $null
    $leaf = Split-Path -Leaf $File
    try {
        $lockStream = Wait-BusLock $lockPath
        if (Test-Reparse $lockPath) { Write-FailClosed "REFUSED ERROR" }
        if (Test-Reparse $File) { Write-FailClosed "REFUSED ERROR" }
        $current = ""
        if (Test-Path -LiteralPath $File) {
            $current = [System.IO.File]::ReadAllText($File)
        }
        $tmp = Write-RandomBusTemp $parent ($current + $Text)
        if (Test-Path -LiteralPath $File) {
            if (Test-Reparse $File) { Write-FailClosed "REFUSED ERROR" }
            [System.IO.File]::Replace($tmp, $File, ($File + ".bak"))
            if (Test-Path -LiteralPath ($File + ".bak")) {
                Remove-Item -LiteralPath ($File + ".bak") -Force
            }
        } else {
            [System.IO.File]::Move($tmp, $File)
        }
        $tmp = $null
        if (Test-Reparse $File) { Write-FailClosed "REFUSED ERROR" }
        if (-not (Test-ParentAllowed $File $allowed)) { Write-FailClosed "REFUSED ERROR" }
    } finally {
        if ($lockStream) { $lockStream.Close() }
        if ($tmp -and (Test-Path -LiteralPath $tmp)) {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Output "APPENDED $leaf"
} catch {
    Write-FailClosed "REFUSED ERROR"
}
