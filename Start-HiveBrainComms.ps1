# HiveBrainComms — 127.0.0.1 only. Default port is $DefaultHubPort.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
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
$Url = "http://127.0.0.1:$HubPort/"
$stagingFlag = [string]$env:HBC_STAGING
if ($stagingFlag.Trim() -eq "1" -and -not (Test-Path -LiteralPath (Join-Path $Root ".hbc-staging"))) {
  Write-Host "WARNING: HBC_STAGING=1 without .hbc-staging; refuse to start"
  throw "HBC_STAGING=1 requires .hbc-staging"
}
function Start-DetachedPython {
  param([string]$Python, [string]$WorkDir)
  $code = @'
using System;
using System.Runtime.InteropServices;
public class HbcDetached {
  [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  static extern bool CreateProcess(string app, string cmd, IntPtr pa, IntPtr ta, bool inherit,
    uint flags, IntPtr env, string dir, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  struct STARTUPINFO {
    public int cb; public string reserved, desktop, title;
    public int x, y, xSize, ySize, xCountChars, yCountChars, fillAttribute, flags;
    public short showWindow, reserved2; public IntPtr reserved3, stdIn, stdOut, stdErr;
  }
  [StructLayout(LayoutKind.Sequential)]
  struct PROCESS_INFORMATION { public IntPtr process, thread; public int processId, threadId; }
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool CloseHandle(IntPtr handle);
  public static int Run(string python, string dir) {
    var si = new STARTUPINFO();
    si.cb = Marshal.SizeOf(si);
    PROCESS_INFORMATION pi;
    string cmd = "\"" + python + "\" server.py";
    // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP. Breakaway is omitted; this process cannot use it.
    if (!CreateProcess(null, cmd, IntPtr.Zero, IntPtr.Zero, false, 0x00000208, IntPtr.Zero, dir, ref si, out pi)) return 0;
    CloseHandle(pi.process);
    CloseHandle(pi.thread);
    return pi.processId;
  }
}
'@
  if (-not ("HbcDetached" -as [type])) { Add-Type -TypeDefinition $code }
  return [HbcDetached]::Run($Python, $WorkDir)
}
function Test-Up {
  try {
    $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
    return $r.StatusCode -eq 200
  } catch { return $false }
}
if (-not (Test-Up)) {
  $py = (Get-Command python -ErrorAction SilentlyContinue).Source
  if (-not $py) { throw "python not on PATH" }
  # Break away from the launcher's job so the server stays up after this script exits.
  $hubPid = Start-DetachedPython -Python $py -WorkDir $Root
  if ($hubPid -le 0) { throw "python server.py did not start" }
  $n = 0
  while (-not (Test-Up) -and $n -lt 20) { Start-Sleep -Milliseconds 250; $n++ }
}
if (-not (Test-Up)) { throw "HiveBrainComms did not start" }
