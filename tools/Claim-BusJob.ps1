# Claim one bus job through localhost /api/claim. Never signs. Never prints the signature or the session token.
# Invoke with: powershell -NoProfile -ExecutionPolicy Bypass -File tools/Claim-BusJob.ps1
# -Hub must be http://127.0.0.1 with DefaultHubPort / HBC_HUB_PORT. localhost gets HTTP 421.
# Windows PowerShell 5.1 compatible. Does not change the execution policy.
# Exit 0 = GO. Exit 2 = NO-GO (HTTP 200 and a valid HMAC, not GO). Exit 3 = ERROR (fail closed).
param(
    [Parameter(Mandatory = $true)][string]$Id,
    [string]$File,
    [string]$Sig,
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
$ExitGo = 0
$ExitNoGo = 2
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

function Get-HubPortFromUrl([string]$url) {
    $norm = ([string]$url).Trim().TrimEnd("/")
    if ($norm -match ':(\d+)$') {
        $n = 0
        if ([int]::TryParse($Matches[1], [ref]$n)) { return $n }
    }
    return $DefaultHubPort
}

function Test-StagingHub([string]$hubUrl) {
    # Port never decides. Both process HBC_STAGING=1 and ROOT/.hbc-staging.
    $flag = [string]$env:HBC_STAGING
    if (-not $flag -or $flag.Trim() -ne "1") { return $false }
    $root = Split-Path -Parent $ScriptDir
    $marker = Join-Path $root ".hbc-staging"
    if (Test-Path -LiteralPath $marker) { return $true }
    return $false
}

function Test-LiveHandoffFile([string]$path) {
    if (-not $path) { return $false }
    $userProfile = [Environment]::GetFolderPath("UserProfile")
    if (-not $userProfile) { $userProfile = $env:USERPROFILE }
    if (-not $userProfile) { $userProfile = $env:HOME }
    if (-not $userProfile) { return $false }
    $live = Join-Path (Join-Path $userProfile ".grok") "handoff"
    try {
        $full = [System.IO.Path]::GetFullPath($path)
        $liveFull = [System.IO.Path]::GetFullPath($live)
        $prefix = $liveFull.TrimEnd('\', '/')
        if ($full.Equals($liveFull, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        return $full.StartsWith($prefix + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($prefix + [IO.Path]::AltDirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Get-JobSig([string]$path, [string]$jobId) {
    if (-not (Test-Path -LiteralPath $path)) { throw "bus file is missing" }
    $lines = [System.IO.File]::ReadAllLines($path)
    $i = 0
    while ($i -lt $lines.Length) {
        if ($lines[$i] -eq ("id: " + $jobId) -or $lines[$i].StartsWith("id: " + $jobId + " ")) {
            $i++
            while ($i -lt $lines.Length -and -not $lines[$i].StartsWith("id: ") -and -not $lines[$i].StartsWith("## ")) {
                if ($lines[$i].StartsWith("sig: ")) {
                    return $lines[$i].Substring(5).Trim()
                }
                $i++
            }
            return ""
        }
        $i++
    }
    return ""
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

function Get-ClaimMac([string]$key, [string]$jobId, [string]$nonce, [string]$verdict) {
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    try {
        $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($key)
        $msg = $jobId + "`n" + $nonce + "`n" + $verdict
        $hash = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($msg))
    } finally {
        $hmac.Dispose()
    }
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Test-SameHex([string]$left, [string]$right) {
    $a = [System.Text.Encoding]::ASCII.GetBytes($left)
    $b = [System.Text.Encoding]::ASCII.GetBytes($right)
    $diff = $a.Length -bxor $b.Length
    $count = [Math]::Max($a.Length, $b.Length)
    for ($i = 0; $i -lt $count; $i++) {
        $ba = if ($i -lt $a.Length) { $a[$i] } else { 0 }
        $bb = if ($i -lt $b.Length) { $b[$i] } else { 0 }
        $diff = $diff -bor ($ba -bxor $bb)
    }
    return $diff -eq 0
}

function Write-FailClosed([string]$verdict, [int]$code) {
    if (-not $verdict) { $verdict = "REFUSED ERROR" }
    [Console]::Out.WriteLine($verdict)
    exit $code
}

try {
    if (-not $Hub) { $Hub = Get-HubBase }
    if (-not (Test-LoopbackHub $Hub)) { Write-FailClosed "REFUSED HUB" $ExitError }
    $posted = $Sig
    if (-not $posted) {
        if (-not $File) { Write-FailClosed "REFUSED ERROR" $ExitError }
        $posted = Get-JobSig -path $File -jobId $Id
    }
    if (-not $posted) { Write-FailClosed "REFUSED ERROR" $ExitError }
    if ((Test-StagingHub $Hub) -and $File -and (Test-LiveHandoffFile $File)) {
        Write-FailClosed -verdict "REFUSED LIVE_HANDOFF" -code $ExitNoGo
    }

    $hubToken = Get-HubToken
    if (-not $hubToken) { Write-FailClosed "REFUSED TOKEN" $ExitError }
    $nonce = [guid]::NewGuid().ToString("N")
    $uri = $Hub.TrimEnd("/") + "/api/claim"
    $bodyObj = @{ id = $Id; sig = $posted; nonce = $nonce }
    $body = $bodyObj | ConvertTo-Json -Compress
    $headers = @{
        "Content-Type" = "application/json"
        "X-Hbc-Token" = $hubToken
    }

    $text = ""
    $statusCode = 0
    try {
        $resp = Invoke-WebRequest -Uri $uri -Method POST -Headers $headers -Body $body -UseBasicParsing -TimeoutSec 5
        $text = [string]$resp.Content
        $statusCode = [int]$resp.StatusCode
    } catch {
        $text = ""
        $statusCode = 0
        if ($_.Exception.Response) {
            try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { $statusCode = 0 }
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $text = [string]$_.ErrorDetails.Message
            }
            if (-not $text) {
                try {
                    $stream = $_.Exception.Response.GetResponseStream()
                    $reader = New-Object System.IO.StreamReader($stream)
                    $text = $reader.ReadToEnd()
                    $reader.Close()
                } catch {
                    $text = ""
                }
            }
        }
        if (-not $text) { Write-FailClosed "REFUSED HUB_DOWN" $ExitError }
    }

    $verdict = ""
    $mac = ""
    try {
        $json = $text | ConvertFrom-Json
        if ($json.verdict) { $verdict = [string]$json.verdict }
        if ($json.mac) { $mac = [string]$json.mac }
    } catch {
        $verdict = ""
        $mac = ""
    }
    if ($verdict -eq "REFUSED TOKEN") { Write-FailClosed "REFUSED TOKEN" $ExitError }
    if ($statusCode -ne 200 -or -not $verdict -or -not $mac) { Write-FailClosed "REFUSED ERROR" $ExitError }
    $expected = Get-ClaimMac $hubToken $Id $nonce $verdict
    if (-not (Test-SameHex $expected $mac)) { Write-FailClosed "REFUSED ERROR" $ExitError }

    # Never leak the signature or the session token in output.
    if ($posted -and $verdict.Contains($posted)) { Write-FailClosed "REFUSED ERROR" $ExitError }
    if ($hubToken -and $verdict.Contains($hubToken)) { Write-FailClosed "REFUSED ERROR" $ExitError }
    if ($verdict -ceq "GO") {
        Write-Output $verdict
        exit $ExitGo
    }
    Write-FailClosed $verdict $ExitNoGo
} catch {
    Write-FailClosed "REFUSED ERROR" $ExitError
}
