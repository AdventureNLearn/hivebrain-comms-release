# Sign or verify one bus job. The secret never leaves this process and is never printed.
param(
    [Parameter(Mandatory = $true)][string]$Owner,
    [Parameter(Mandatory = $true)][string]$File,
    [Parameter(Mandatory = $true)][string]$Id,
    [switch]$Verify,
    [string]$EnforceAt,
    [string]$TestKeyPath
)
$ErrorActionPreference = "Stop"
$fields = @("v", "id", "from", "to", "lane", "priority", "time", "routed", "pane", "job", "done-when")
# Optional EnforceAt: full ISO time with offset. Missing, unreadable, or past means enforce now. A future value only softens this agent-side check. See rule 13 in the user's bus protocol file.
# -TestKeyPath is a test-only seam. A -Verify OK with -TestKeyPath proves nothing about a real owner key.
if ($TestKeyPath) { Write-Output "TEST-KEY" }

function Get-LaneTable {
    $builtIn = @(
        @{ Id = "Hub"; Owner = "Hub"; Key = "bus-hub.bin" },
        @{ Id = "Builder"; Owner = "Builder"; Key = "bus-builder.bin" },
        @{ Id = "Lead"; Owner = "Lead"; Key = "bus-lead.bin" },
        @{ Id = "GrokBot"; Owner = "Grok Bot"; Key = "bus-grokbot.bin" }
    )
    $path = $env:HBC_LANES_FILE
    if (-not $path) {
        $path = Join-Path (Split-Path -Parent $PSScriptRoot) "lanes.local.json"
    }
    if ($path -and (Test-Path -LiteralPath $path)) {
        try {
            $data = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            $rows = @()
            foreach ($lane in @($data.lanes)) {
                $rows += @{
                    Id = [string]$lane.id
                    Owner = [string]$lane.owner
                    Key = [string]$lane.key
                }
            }
            if ($rows.Count -gt 0) { return $rows }
        } catch { }
    }
    return $builtIn
}

$laneTable = Get-LaneTable
$lane = $null
foreach ($row in $laneTable) {
    if ($row.Id -ceq $Owner -or $row.Owner -ceq $Owner) { $lane = $row; break }
}
if ($null -eq $lane) { throw "unknown owner" }
$keyName = $lane.Key
$routerName = $lane.Owner
$keyPath = $null
if (-not $TestKeyPath) {
    $root = $env:USERPROFILE
    if (-not $root) { throw "signing key is missing" }
    $keyPath = Join-Path $root ".grok\keys\$keyName"
    if (-not (Test-Path $keyPath)) { throw "signing key is missing" }
}

function Get-Secret {
    if ($TestKeyPath) {
        if (-not (Test-Path -LiteralPath $TestKeyPath)) { throw "signing key is missing" }
        return [System.IO.File]::ReadAllBytes($TestKeyPath)
    }
    Add-Type -AssemblyName System.Security
    $protected = [System.IO.File]::ReadAllBytes($keyPath)
    return [System.Security.Cryptography.ProtectedData]::Unprotect(
        $protected, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
}

function Test-SoftWarn([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value)) { return $false }
    if ($value -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$') {
        return $false
    }
    try {
        $when = [datetimeoffset]::Parse($value)
        return [datetimeoffset]::Now -lt $when
    } catch {
        return $false
    }
}

function Get-Canonical([hashtable]$map) {
    $lines = foreach ($name in $fields) {
        $value = ""
        if ($map.ContainsKey($name)) { $value = [string]$map[$name] }
        $value = ([regex]::Replace($value.Trim(), "\s+", " "))
        "$name=$value"
    }
    return ($lines -join "`n")
}

function Get-Mac([byte[]]$secret, [string]$text) {
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    try {
        $hmac.Key = $secret
        $hash = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($text))
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

function Get-EasternZone {
    foreach ($name in @("America/New_York", "Eastern Standard Time")) {
        try { return [TimeZoneInfo]::FindSystemTimeZoneById($name) } catch { }
    }
    throw "Eastern time zone is missing"
}

function Get-EasternIsoNow {
    $tz = Get-EasternZone
    $utc = [DateTimeOffset]::UtcNow
    $local = [TimeZoneInfo]::ConvertTime($utc, $tz)
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    return $local.ToString("yyyy-MM-ddTHH:mm:sszzz", $inv)
}

function Get-BannedVerbsPath {
    $root = Split-Path -Parent $PSScriptRoot
    return Join-Path (Join-Path $root "hub") "banned-verbs.txt"
}

function Normalize-VerbText([string]$text) {
    if ([string]::IsNullOrEmpty($text)) { return "" }
    $norm = $text.Normalize([Text.NormalizationForm]::FormKC)
    $norm = $norm.ToLowerInvariant()
    $norm = [regex]::Replace($norm, "[\u200b\u200c\u200d\u2060\ufeff]", "")
    $norm = [regex]::Replace($norm, "\s+", " ").Trim()
    return $norm
}

function Get-BannedVerbs {
    $path = Get-BannedVerbsPath
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $items = New-Object System.Collections.ArrayList
    try {
        foreach ($line in [System.IO.File]::ReadAllLines($path)) {
            $raw = $line.Trim()
            if (($raw.Length -eq 0) -or $raw.StartsWith("#")) { continue }
            [void]$items.Add($raw)
        }
    } catch {
        return $null
    }
    if ($items.Count -eq 0) { return $null }
    return $items
}

function Get-P4cRefuse([string[]]$blockLines, [hashtable]$map, [string]$ownerRouter) {
    $current = $null
    foreach ($line in $blockLines) {
        if ($line -cmatch "^([a-z][a-z0-9-]*): ?(.*)$") {
            $current = $Matches[1]
        } elseif ($current -and $current -ne "job" -and $current -ne "done-when") {
            if (-not [string]::IsNullOrWhiteSpace($line)) {
                return "BAD_FIELD"
            }
        }
    }
    foreach ($line in $blockLines) {
        if ($line -cmatch "^pane:(.*)$") {
            $rest = [string]$Matches[1]
            $value = if ($rest.StartsWith(" ")) { $rest.Substring(1) } else { $rest }
            if ($value -cne "" -and $value -cne "1" -and $value -cne "2") {
                return "BAD_PANE"
            }
        }
    }
    $from = ""
    if ($map.ContainsKey("from")) { $from = [string]$map["from"] }
    if ($from.Length -gt 0 -and $from -cne $ownerRouter) {
        return "OWNER_MISMATCH"
    }
    foreach ($name in @("v", "id", "from", "to", "lane", "job", "done-when")) {
        $val = ""
        if ($map.ContainsKey($name)) { $val = [string]$map[$name] }
        if ([string]::IsNullOrWhiteSpace($val)) { return "MISSING_FIELD" }
    }
    $pri = ""
    if ($map.ContainsKey("priority")) { $pri = [string]$map["priority"] }
    if ($pri -cne "urgent" -and $pri -cne "normal") { return "BAD_PRIORITY" }
    $job = ""
    if ($map.ContainsKey("job")) { $job = [string]$map["job"] }
    $verbs = Get-BannedVerbs
    if ($null -eq $verbs) { return "VERBS_UNAVAILABLE" }
    $jobNorm = Normalize-VerbText $job
    foreach ($verb in @($verbs)) {
        $needle = Normalize-VerbText ([string]$verb)
        if (($needle.Length -gt 0) -and $jobNorm.Contains($needle)) { return "BAD_VERB" }
    }
    return $null
}

function Get-TimeRoutedRefuse([string[]]$blockLines) {
    foreach ($line in $blockLines) {
        if ($line -cmatch "^([a-z][a-z0-9-]*):(.*)$") {
            $key = $Matches[1]
            $rest = $Matches[2]
            if (($key -eq "time" -or $key -eq "routed") -and (($rest.Length -eq 0) -or -not $rest.StartsWith(" "))) {
                return "${key}: line must have a space after the colon"
            }
        }
    }
    return $null
}

function Test-HubIso([string]$value) {
    # Same shape as hub parse_iso_time: ISO-8601 with Eastern offset only.
    if ([string]::IsNullOrEmpty($value) -or $value.Length -gt 64) { return $false }
    if ($value -cnotmatch "^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]{1,6})?([+-][0-9]{2}:[0-9]{2})$") {
        return $false
    }
    $off = $Matches[3]
    if ($off -ne "-04:00" -and $off -ne "-05:00") { return $false }
    try {
        [void][datetimeoffset]::Parse($value, [System.Globalization.CultureInfo]::InvariantCulture)
        return $true
    } catch {
        return $false
    }
}

function Get-RoutedFill([string]$existing, [string]$ownerRouter, [string]$iso, [string[]]$routers) {
    # At least as strict as the hub. No empty, bare name, datetime-only, or GB alias.
    # Owner → router mapping comes from lanes.local.json or the built-in table
    # (Hub/Builder defaults; Lead/GrokBot remain recognized CLI owners).
    $raw = if ($null -eq $existing) { "" } else { ([string]$existing).Trim() }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    $names = @($routers | Sort-Object { $_.Length } -Descending)
    $name = $null
    $rest = $null
    foreach ($router in $names) {
        $prefix = "$router "
        if ($raw.StartsWith($prefix)) {
            $name = $router
            $rest = $raw.Substring($prefix.Length)
            break
        }
    }
    if ($null -eq $name -or $null -eq $rest) {
        return $null
    }
    if ($name -cne $ownerRouter) {
        return $null
    }
    if (-not (Test-HubIso $rest)) {
        return $null
    }
    return "$name $iso"
}

function Get-BlockMap([string[]]$blockLines) {
    $map = @{}
    $current = $null
    foreach ($line in $blockLines) {
        if ($line -cmatch "^([a-z][a-z0-9-]*): ?(.*)$") {
            $current = $Matches[1]
            $map[$current] = $Matches[2]
        } elseif ($current) {
            $map[$current] = ($map[$current] + " " + $line).Trim()
        }
    }
    return $map
}

function Wait-BusLock([string]$path, [int]$timeoutSec = 5) {
    $deadline = [datetime]::UtcNow.AddSeconds($timeoutSec)
    while ($true) {
        try {
            return [System.IO.File]::Open(
                $path,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        } catch {
            if ([datetime]::UtcNow -ge $deadline) { throw "bus file lock timeout" }
            Start-Sleep -Milliseconds 50
        }
    }
}

$lockPath = "$File.lock"
$lockStream = $null
try {
$lockStream = Wait-BusLock $lockPath
$text = [System.IO.File]::ReadAllText($File)
$lines = $text -split "`r?`n"
$start = -1
for ($i = 0; $i -lt $lines.Length; $i++) {
    if ($lines[$i] -match "^id: $( [regex]::Escape($Id) )$") { $start = $i; break }
}
if ($start -lt 0) { throw "job id not found" }
$end = $lines.Length
for ($i = $start + 1; $i -lt $lines.Length; $i++) {
    if ($lines[$i] -match "^(id: |## )") { $end = $i; break }
}
$block = $lines[$start..($end - 1)]
if (-not $Verify) {
    $refuse = Get-TimeRoutedRefuse @($block)
    if ($refuse) {
        Write-Output $refuse
        exit 3
    }
}
$map = Get-BlockMap $block
$filledIso = $null
$routerNames = @($laneTable | ForEach-Object { $_.Owner } | Select-Object -Unique)
if (-not $Verify) {
    $p4c = Get-P4cRefuse @($block) $map $routerName
    if ($p4c) {
        Write-Output $p4c
        exit 3
    }
    $map["v"] = "2"
    $filledIso = Get-EasternIsoNow
    $map["time"] = $filledIso
    $existingRouted = ""
    if ($map.ContainsKey("routed")) { $existingRouted = [string]$map["routed"] }
    $filledRouted = Get-RoutedFill $existingRouted $routerName $filledIso $routerNames
    if ($null -eq $filledRouted) {
        Write-Output "routed: must be Grok Bot or Lead plus ISO-8601 with offset, match -Owner, and have no trailing junk"
        exit 3
    }
    $map["routed"] = $filledRouted
}
$secret = Get-Secret
try {
    $mac = Get-Mac $secret (Get-Canonical $map)
} finally {
    [Array]::Clear($secret, 0, $secret.Length)
}

if ($Verify) {
    $given = if ($map.ContainsKey("sig")) { [string]$map["sig"] } else { "" }
    $ok = (Test-SameHex $mac $given) -and $given.Length -gt 0
    if ($ok) { Write-Output "OK $Id"; exit 0 }
    $warn = Test-SoftWarn $EnforceAt
    $verdict = if ($warn) { "SPOOFED-WARN" } else { "SPOOFED" }
    $logRoot = $env:TEMP
    if ($logRoot) {
        "$(Get-Date -Format o) $verdict $Id" | Add-Content (Join-Path $logRoot "bus-sig.log")
    }
    Write-Output "$verdict $Id"
    exit $(if ($warn) { 0 } else { 3 })
}

function Set-BlockField([string]$name, [string]$value) {
    $script:lines = $script:lines
    $line = "${name}: $value"
    $found = $false
    for ($i = $script:start; $i -lt $script:end; $i++) {
        if ($script:lines[$i] -cmatch ("^" + [regex]::Escape($name) + ": ")) {
            $script:lines[$i] = $line
            $found = $true
        }
    }
    if (-not $found) {
        $head = @()
        if ($script:start -gt 0) { $head = $script:lines[0..($script:start - 1)] }
        $idLine = @($script:lines[$script:start])
        $rest = @()
        if (($script:start + 1) -le ($script:end - 1)) { $rest = @($script:lines[($script:start + 1)..($script:end - 1)]) }
        $tail = @()
        if ($script:end -lt $script:lines.Length) { $tail = $script:lines[$script:end..($script:lines.Length - 1)] }
        $script:lines = $head + $idLine + @($line) + $rest + $tail
        $script:end = $script:end + 1
    }
}

$vLine = "v: 2"
$replacedV = $false
for ($i = $start; $i -lt $end; $i++) {
    if ($lines[$i] -match "^v: ") { $lines[$i] = $vLine; $replacedV = $true }
}
if (-not $replacedV) {
    $head = @()
    if ($start -gt 0) { $head = $lines[0..($start - 1)] }
    $idLine = @($lines[$start])
    $rest = @()
    if (($start + 1) -le ($end - 1)) { $rest = @($lines[($start + 1)..($end - 1)]) }
    $tail = @()
    if ($end -lt $lines.Length) { $tail = $lines[$end..($lines.Length - 1)] }
    $lines = $head + $idLine + @($vLine) + $rest + $tail
    $end = $end + 1
}
if ($filledIso) {
    Set-BlockField "time" $filledIso
    Set-BlockField "routed" ([string]$map["routed"])
}

$sigLine = "sig: $mac"
$replaced = $false
for ($i = $start; $i -lt $end; $i++) {
    if ($lines[$i] -match "^sig: ") { $lines[$i] = $sigLine; $replaced = $true }
}
if (-not $replaced) {
    $head = @()
    if ($start -gt 0) { $head = $lines[0..($start - 1)] }
    $mid = @($lines[$start..($end - 1)]) + @($sigLine)
    $tail = @()
    if ($end -lt $lines.Length) { $tail = $lines[$end..($lines.Length - 1)] }
    $lines = $head + $mid + $tail
}
[System.IO.File]::WriteAllText($File, ($lines -join "`r`n"))
Write-Output "SIGNED $Id"
} finally {
    if ($lockStream) { $lockStream.Close() }
}
