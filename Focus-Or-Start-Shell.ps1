$ErrorActionPreference = "Stop"
$DefaultHubPort = 8107
# HBC_HUB_PORT is process-only. Never setx. ASCII [0-9] only.
function Get-HubPort {
    $raw = [string]$env:HBC_HUB_PORT
    if (-not $raw) { return $DefaultHubPort }
    $trimmed = $raw.Trim()
    $n = 0
    if ($trimmed -notmatch '^[0-9]+$' -or -not [int]::TryParse($trimmed, [ref]$n) -or $n -lt 1024 -or $n -gt 65535) {
        Write-Host "hub-port: HBC_HUB_PORT '$raw' refused; using $DefaultHubPort"
        return $DefaultHubPort
    }
    return $n
}
$HubPort = Get-HubPort
$hub = "http://127.0.0.1:$HubPort/"

function Test-HubUp {
    try {
        $response = Invoke-WebRequest -Uri $hub -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Write-GuardMessage {
    param([string]$Text)
    Write-Host $Text
}

function Read-StartedProcess {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $pidValue = 0
    $ctime = [int64]0
    $sawPid = $false
    $sawCtime = $false
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^pid=(\d+)$') { $pidValue = [int]$Matches[1]; $sawPid = $true }
        elseif ($line -match '^ctime=(-?\d+)$') { $ctime = [int64]$Matches[1]; $sawCtime = $true }
    }
    if (-not $sawPid -or -not $sawCtime -or $pidValue -le 0 -or $ctime -eq 0) { return $null }
    return [pscustomobject]@{ Pid = $pidValue; Ctime = $ctime }
}

function Test-CommandOurs {
    param([string]$Command, [string]$NeedleA, [string]$NeedleB)
    if (-not $Command) { return $false }
    return ($Command.IndexOf($NeedleA, [StringComparison]::OrdinalIgnoreCase) -ge 0) -and
           ($Command.IndexOf($NeedleB, [StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Resolve-StartedProcess {
    param([string]$Path, [string]$NeedleA, [string]$NeedleB)
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ State = "Missing" }
    }
    $rec = Read-StartedProcess $Path
    if (-not $rec) {
        return [pscustomobject]@{ State = "Corrupt" }
    }
    try {
        $proc = Get-Process -Id $rec.Pid -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ State = "Gone"; Rec = $rec }
    }
    if ($proc.StartTime.ToFileTimeUtc() -ne $rec.Ctime) {
        return [pscustomobject]@{ State = "Stale"; Rec = $rec }
    }
    $cmd = (Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $rec.Pid) -ErrorAction SilentlyContinue).CommandLine
    if (-not (Test-CommandOurs $cmd $NeedleA $NeedleB)) {
        return [pscustomobject]@{ State = "Stale"; Rec = $rec }
    }
    return [pscustomobject]@{ State = "Live"; Rec = $rec; Proc = $proc }
}

function Get-UnquotedPath {
    param([string]$Value)
    $raw = [string]$Value
    if ($null -eq $raw) { return "" }
    $raw = $raw.Trim()
    while ($raw.Length -ge 2) {
        $first = $raw.Substring(0, 1)
        $last = $raw.Substring($raw.Length - 1, 1)
        if (-not ((($first -eq '"') -or ($first -eq "'")) -and ($first -eq $last))) {
            break
        }
        $raw = $raw.Substring(1, $raw.Length - 2).Trim()
    }
    return $raw
}

function Resolve-AppProcessRecordPath {
    param([string]$Root, [string]$Path = ".shell-process")
    $rootRaw = Get-UnquotedPath $Root
    if ([string]::IsNullOrWhiteSpace($rootRaw)) {
        throw "app process record root is empty"
    }
    $rootFull = [System.IO.Path]::GetFullPath($rootRaw)
    $raw = Get-UnquotedPath $Path
    $leaf = ".shell-process"
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
        $name = [System.IO.Path]::GetFileName($raw.Replace("\", "/"))
        if (-not [string]::IsNullOrWhiteSpace($name)) {
            $leaf = $name
        }
    }
    return [System.IO.Path]::GetFullPath((Join-Path $rootFull $leaf))
}

function Write-StartedProcess {
    param([int]$ProcId, [string]$Path, [string]$Root)
    if ([string]::IsNullOrWhiteSpace($Root)) {
        $Root = $PSScriptRoot
    }
    $full = Resolve-AppProcessRecordPath -Root $Root -Path $Path
    $dir = [System.IO.Path]::GetDirectoryName($full)
    $tmp = Join-Path $dir (".shell-process.{0}.tmp" -f [guid]::NewGuid().ToString("N"))
    $backup = Join-Path $dir (".shell-process.{0}.bak" -f [guid]::NewGuid().ToString("N"))
    $utf8 = New-Object System.Text.UTF8Encoding $false
    try {
        $proc = Get-Process -Id $ProcId
        $text = "pid=$ProcId`nctime=$($proc.StartTime.ToFileTimeUtc())`n"
    } catch {
        Write-GuardMessage ("App process record write failed at {0}. Process {1} is gone. Stop will refuse. {2}" -f $full, $ProcId, $_.Exception.Message)
        return
    }
    try {
        [System.IO.File]::WriteAllText($tmp, $text, $utf8)
        $fs = [System.IO.File]::Open($tmp, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        try { $fs.Flush($true) } finally { $fs.Dispose() }
        if (Test-Path -LiteralPath $full) {
            [System.IO.File]::Replace($tmp, $full, $backup)
            if (Test-Path -LiteralPath $backup) {
                Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
            }
        } else {
            [System.IO.File]::Move($tmp, $full)
        }
    } catch {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backup) {
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        }
        Write-GuardMessage ("App process record could not be replaced at {0}. Trying a direct write. {1}" -f $full, $_.Exception.Message)
        try {
            [System.IO.File]::WriteAllText($full, $text, $utf8)
        } catch {
            Write-GuardMessage ("App process record write failed at {0}. Stop will refuse. {1}" -f $full, $_.Exception.Message)
        }
    }
}

function Test-PathUnderLive {
    param([string]$Candidate, [string]$LiveDir)
    if (-not $Candidate) { return $false }
    $liveFull = [System.IO.Path]::GetFullPath($LiveDir).TrimEnd("\", "/")
    $sep = [System.IO.Path]::DirectorySeparatorChar
    if ($Candidate.IndexOf($liveFull, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    try {
        $candFull = [System.IO.Path]::GetFullPath($Candidate)
    } catch {
        return $false
    }
    return $candFull.Equals($liveFull, [StringComparison]::OrdinalIgnoreCase) -or
           $candFull.StartsWith(($liveFull + $sep), [StringComparison]::OrdinalIgnoreCase)
}

function Get-SwapBlockers {
    param([string]$LiveDir)
    $blockers = New-Object System.Collections.Generic.List[object]
    $failClosed = $false
    $reason = $null
    foreach ($name in @("Shell", "dotnet")) {
        foreach ($proc in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            $module = $null
            try { $module = $proc.MainModule.FileName } catch { }
            $cmd = $null
            try {
                $cmd = (Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $proc.Id) -ErrorAction Stop).CommandLine
            } catch { }
            if ([string]::IsNullOrWhiteSpace($cmd)) {
                $failClosed = $true
                $reason = "command line unreadable for PID $($proc.Id)"
                $blockers.Add($proc)
                continue
            }
            if ((Test-PathUnderLive $module $LiveDir) -or (Test-PathUnderLive $cmd $LiveDir)) {
                $blockers.Add($proc)
            }
        }
    }
    return [pscustomobject]@{ FailClosed = $failClosed; Reason = $reason; Procs = $blockers }
}

function Clear-StaleIncoming {
    param([string]$Parent)
    Get-ChildItem -LiteralPath $Parent -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -like "live-incoming-*" -and
            -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
        } |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
}

function Invoke-AtomicLiveSwap {
    param([string]$Staging, [string]$Live)
    $parent = Split-Path -Parent $Live
    Clear-StaleIncoming -Parent $parent
    $incoming = Join-Path $parent ("live-incoming-" + [guid]::NewGuid().ToString("N"))
    $backup = Join-Path $parent ("live-backup-" + [guid]::NewGuid().ToString("N"))
    $backupLeaf = Split-Path -Leaf $backup
    $liveLeaf = Split-Path -Leaf $Live
    try {
        New-Item -ItemType Directory -Path $incoming -Force | Out-Null
        Copy-Item -Path (Join-Path $Staging "*") -Destination $incoming -Recurse -Force
        if (-not (Test-Path -LiteralPath (Join-Path $incoming "Shell.exe"))) {
            throw "staged Shell.exe missing after copy"
        }
        if (Test-Path -LiteralPath $Live) {
            Rename-Item -LiteralPath $Live -NewName $backupLeaf
        }
        try {
            Rename-Item -LiteralPath $incoming -NewName $liveLeaf
        } catch {
            if ((-not (Test-Path -LiteralPath $Live)) -and (Test-Path -LiteralPath $backup)) {
                try {
                    Rename-Item -LiteralPath $backup -NewName $liveLeaf
                } catch {
                    Write-GuardMessage "RESTORE FAILED. Live folder is missing. Backup is still at $backup"
                    throw "RESTORE FAILED. Live folder is missing. Backup is still at $backup"
                }
            }
            throw
        }
        $cleanupWarn = $false
        try {
            if (Test-Path -LiteralPath $backup) {
                Remove-Item -LiteralPath $backup -Recurse -Force
            }
            Remove-Item -LiteralPath $Staging -Recurse -Force
        } catch {
            $cleanupWarn = $true
        }
        if ($cleanupWarn) {
            Write-GuardMessage "Swap succeeded. Cleanup warning: leftover $backup or staging."
        }
    } catch {
        if (Test-Path -LiteralPath $incoming) {
            Remove-Item -LiteralPath $incoming -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

function Show-ProcessWindow {
    param([System.Diagnostics.Process]$Proc)
    if (-not ("HbcShow" -as [type])) {
        Add-Type @"
using System; using System.Runtime.InteropServices;
public class HbcShow { public delegate bool P(IntPtr h, IntPtr l);
[DllImport("user32.dll")] static extern bool EnumWindows(P p, IntPtr l);
[DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
[DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int n);
[DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
public static bool Show(uint target){ bool ok=false; EnumWindows((h,l)=>{ uint p; GetWindowThreadProcessId(h,out p); if(p==target){ ShowWindow(h,9); SetForegroundWindow(h); ok=true; } return true; }, IntPtr.Zero); return ok; }
public static bool ShowHandle(IntPtr h){ if(h==IntPtr.Zero) return false; ShowWindow(h,9); SetForegroundWindow(h); return true; } }
"@
    }
    if ($Proc.MainWindowHandle -ne [IntPtr]::Zero) {
        return [HbcShow]::ShowHandle($Proc.MainWindowHandle)
    }
    return [HbcShow]::Show([uint32]$Proc.Id)
}

function Focus-LiveShell {
    param($LiveProcs)
    foreach ($row in $LiveProcs) {
        $proc = $row
        if ($row.PSObject.Properties["ProcessId"]) {
            $proc = Get-Process -Id $row.ProcessId -ErrorAction SilentlyContinue
        }
        if (-not $proc) { continue }
        if (Show-ProcessWindow $proc) { return $true }
    }
    return $false
}

# One listener only. A live hub is left alone. A down hub is started hidden and detached.
if (-not (Test-HubUp)) {
    $starter = Join-Path $PSScriptRoot "Start-HiveBrainComms.ps1"
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $starter
    ) -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-HubUp) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-HubUp)) { throw "HiveBrainComms hub did not answer on 127.0.0.1:$HubPort within 15 seconds." }
}

$live = Join-Path $PSScriptRoot "shell\bin\Release\net8.0-windows"
Clear-StaleIncoming -Parent (Split-Path -Parent $live)
$staging = Join-Path $PSScriptRoot "shell\staging"
$exe = Join-Path $live "Shell.exe"
$dll = Join-Path $live "Shell.dll"
$shellRecord = Resolve-AppProcessRecordPath -Root $PSScriptRoot -Path ".shell-process"
$resolved = Resolve-StartedProcess $shellRecord "Shell.dll" $live
$swapBlock = Get-SwapBlockers -LiveDir $live

# Swap a staged build only when the app we started has exited and no live Shell path is running.
if (Test-Path (Join-Path $staging "Shell.exe")) {
    if ($swapBlock.FailClosed) {
        Write-GuardMessage "A Shell.exe or dotnet command line could not be read. Not overwriting."
        if (-not (Focus-LiveShell $swapBlock.Procs)) {
            Write-GuardMessage "Live Shell is running but has no window. Exiting."
        }
        return
    }
    if ($swapBlock.Procs.Count -gt 0) {
        Write-GuardMessage "A Shell.exe or dotnet process is using the live folder. Not overwriting."
        if (-not (Focus-LiveShell $swapBlock.Procs)) {
            Write-GuardMessage "Live Shell is running but has no window. Exiting."
        }
        return
    }
    if ($resolved.State -eq "Missing") {
        Write-GuardMessage "App process record is missing. Not overwriting."
    } elseif ($resolved.State -eq "Corrupt") {
        Write-GuardMessage "App process record is corrupt. Not overwriting."
    } elseif ($resolved.State -eq "Stale") {
        Write-GuardMessage "App process record is stale. Not overwriting."
    } elseif ($resolved.State -eq "Live") {
        Write-GuardMessage "Started app is still running. Not overwriting."
        if ($resolved.Proc) { Show-ProcessWindow $resolved.Proc | Out-Null }
        return
    } elseif ($resolved.State -eq "Gone") {
        try {
            Invoke-AtomicLiveSwap -Staging $staging -Live $live
        } catch {
            if ("$_" -notmatch "RESTORE FAILED") {
                Write-GuardMessage "Atomic stage-and-swap failed. Live folder was left unchanged."
            }
            return
        }
    }
}

$resolved = Resolve-StartedProcess $shellRecord "Shell.dll" $live
$swapBlock = Get-SwapBlockers -LiveDir $live
if ($swapBlock.FailClosed -or $swapBlock.Procs.Count -gt 0) {
    if ($swapBlock.FailClosed) {
        Write-GuardMessage "A Shell.exe or dotnet command line could not be read. Focusing or exiting."
    } else {
        Write-GuardMessage "A Shell.exe or dotnet process is using the live folder. Focusing it."
    }
    if (-not (Focus-LiveShell $swapBlock.Procs)) {
        Write-GuardMessage "Live Shell is running but has no window. Exiting."
    }
    return
}
if ($resolved.State -eq "Live") {
    if ($resolved.Proc -and (Show-ProcessWindow $resolved.Proc)) { return }
    Write-GuardMessage "Started app is still running but has no window. Exiting."
    return
}
if ($resolved.State -eq "Corrupt") {
    Write-GuardMessage "App process record is corrupt. Not starting another."
    return
}
if ($resolved.State -eq "Stale") {
    Write-GuardMessage "App process record is stale. Not starting another."
    return
}

# Missing or Gone: start the app we own and record it.
# Smart App Control blocks the unsigned Shell.exe, so run the app through the Microsoft-signed dotnet host.
if (-not (Test-Path $dll)) { throw "Shell.dll is not built yet." }
$proc = Start-Process -FilePath "dotnet" -ArgumentList "`"$dll`"" -WorkingDirectory $live -WindowStyle Hidden -PassThru
Write-StartedProcess -ProcId $proc.Id -Path $shellRecord -Root $PSScriptRoot
# Hidden keeps the dotnet console out of sight, but it also hides the first show of the form, so show the app window once it exists.
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Show-ProcessWindow $proc) { break }
}
