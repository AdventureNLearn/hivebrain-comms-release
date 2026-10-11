# Approve one watcher job through localhost /api/decide. Never signs. Never prints the token or signature.
# Shows exactly what the hub will approve: GET /api/job/<id> (Host-checked, token header).
# Invoke with: powershell -NoProfile -ExecutionPolicy Bypass -File tools/Approve-BusJob.ps1 -Id <id>
# -Hub must be http://127.0.0.1 with DefaultHubPort / HBC_HUB_PORT. localhost gets HTTP 421.
# Windows PowerShell 5.1 compatible. Does not change the execution policy.
# Exit 0 = approved. Exit 2 = declined or NO-GO. Exit 3 = ERROR (fail closed).
param(
    [Parameter(Mandatory = $true)][string]$Id,
    [string]$File,
    [string]$Hub,
    [string]$Token,
    [string]$TokenFile
)
$ErrorActionPreference = "Stop"
if ($PSScriptRoot) {
    $ScriptDir = $PSScriptRoot
} else {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ExitOk = 0
$ExitNo = 2
$ExitError = 3
$DefaultHubPort = 8107

# HBC_HUB_PORT is process-only. Never setx. ASCII [0-9] only.
function Get-HubBase {
    $port = $DefaultHubPort
    $raw = [string]$env:HBC_HUB_PORT
    if ($raw) {
        $trimmed = $raw.Trim()
        $n = 0
        if ($trimmed -notmatch '^[0-9]+$' -or -not [int]::TryParse($trimmed, [ref]$n) -or $n -lt 1024 -or $n -gt 65535) {
            Write-Host "hub-port: HBC_HUB_PORT '$raw' refused; using $DefaultHubPort"
        } else {
            $port = $n
        }
    }
    return "http://127.0.0.1:$port"
}

function Test-LoopbackHub([string]$url) {
    $norm = ([string]$url).Trim().TrimEnd("/")
    return $norm -match '^http://127\.0\.0\.1(:\d+)?$'
}

function Write-FailClosed([string]$verdict, [int]$code) {
    if (-not $verdict) { $verdict = "REFUSED ERROR" }
    Write-Output $verdict
    exit $code
}

function Get-HubToken {
    if ($Token) { return $Token }
    $candidates = @()
    if ($TokenFile) { $candidates += $TokenFile }
    $root = Split-Path -Parent $ScriptDir
    $candidates += (Join-Path $root ".session-token")
    foreach ($item in $candidates) {
        if ($item -and (Test-Path -LiteralPath $item)) {
            return ([System.IO.File]::ReadAllText($item)).Trim()
        }
    }
    return ""
}

function Get-HubJob([string]$hubBase, [string]$jobId, [string]$hubToken) {
    $uri = $hubBase.TrimEnd("/") + "/api/job/" + [uri]::EscapeDataString($jobId)
    $headers = @{ "X-Hbc-Token" = $hubToken }
    $resp = Invoke-WebRequest -Uri $uri -Method GET -Headers $headers -UseBasicParsing -TimeoutSec 5 -MaximumRedirection 0
    $statusCode = [int]$resp.StatusCode
    if ($statusCode -eq 404) { Write-FailClosed "REFUSED NOT_FOUND" $ExitError }
    if ($statusCode -ne 200) { Write-FailClosed "REFUSED ERROR" $ExitError }
    $text = [string]$resp.Content
    if ($hubToken -and $text.Contains($hubToken)) { Write-FailClosed "REFUSED ERROR" $ExitError }
    $job = $text | ConvertFrom-Json
    if (-not $job -or -not $job.id) { Write-FailClosed "REFUSED NOT_FOUND" $ExitError }
    if (-not $job.hash) { Write-FailClosed "REFUSED NOT_FOUND" $ExitError }
    return $job
}

try {
    if (-not $Hub) { $Hub = Get-HubBase }
    if (-not (Test-LoopbackHub $Hub)) { Write-FailClosed "REFUSED HUB" $ExitError }
    $hubToken = Get-HubToken
    if (-not $hubToken) { Write-FailClosed "REFUSED TOKEN" $ExitError }
    if ([Console]::IsInputRedirected) { Write-FailClosed "REFUSED REDIRECT" $ExitError }
    if (-not [Environment]::UserInteractive) { Write-FailClosed "REFUSED INTERACTIVE" $ExitError }

    $job = Get-HubJob $Hub $Id $hubToken
    Write-Output ("id: " + $job.id)
    Write-Output ("from: " + $job.from)
    Write-Output ("lane: " + $job.lane)
    Write-Output ("summary: " + $job.summary)
    Write-Output ("hash: " + $job.hash)

    $ans = Read-Host "Approve this job? y/n"
    if ($ans -ne "y" -and $ans -ne "Y") { Write-FailClosed "DECLINED" $ExitNo }

    $uri = $Hub.TrimEnd("/") + "/api/decide"
    $bodyObj = @{ id = $Id; decision = "approve"; hash = $job.hash }
    $body = $bodyObj | ConvertTo-Json -Compress
    $headers = @{
        "Content-Type" = "application/json"
        "X-Hbc-Token" = $hubToken
    }
    $resp = Invoke-WebRequest -Uri $uri -Method POST -Headers $headers -Body $body -UseBasicParsing -TimeoutSec 5 -MaximumRedirection 0
    $text = [string]$resp.Content
    $statusCode = [int]$resp.StatusCode
    if ($statusCode -ne 200) { Write-FailClosed "REFUSED ERROR" $ExitError }
    if ($hubToken -and $text.Contains($hubToken)) { Write-FailClosed "REFUSED ERROR" $ExitError }
    Write-Output $text
    exit $ExitOk
} catch {
    Write-FailClosed "REFUSED ERROR" $ExitError
}
