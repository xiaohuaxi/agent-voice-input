param(
    [string]$Mode = "bridge", [string]$Hotkey = "RControl", [string]$TriggerMode = "tap_toggle",
    [int]$TapMaxMs = 500, [int]$DebounceMs = 180, [int]$Seconds = 10, [int]$Beep = 0,
    [Alias("AllowedTerminals")][string]$AllowedTerminalsCsv = "",
    [string]$CacheDir = "",
    [int]$HoldCancelMs = 800, [int]$HoldHintMs = 250,
    [switch]$RealClipboard
)
$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function Write-JsonLine($obj) { try { [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 5)); [Console]::Out.Flush() } catch { } }
function Write-InitFailureAndExit($errObj) {
    $msg = if ($null -ne $errObj -and $null -ne $errObj.Exception) { [string]$errObj.Exception.Message } else { [string]$errObj }
    if ($Mode -eq "bridge" -or $Mode -eq "probe" -or $Mode -eq "key-test") {
        Write-JsonLine @{ ok = $false; error = $msg }
    } else {
        [Console]::Error.WriteLine("win_shim init failed: " + $msg)
    }
    exit 1
}
try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
} catch {
    Write-InitFailureAndExit $_
}
$csCacheHit = $false
if (-not ('VoiceWinShim' -as [type])) {
    $csNames = $null
    $csSources = $null
    $csCompileSources = $null
    $csHash = ""
    try {
        $scriptDir = if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
            $PSScriptRoot
        } elseif (-not [string]::IsNullOrWhiteSpace($PSCommandPath)) {
            Split-Path -Parent $PSCommandPath
        } else {
            (Get-Location).Path
        }
        $winShimDir = Join-Path $scriptDir "win_shim"
        if (-not (Test-Path -LiteralPath $winShimDir -PathType Container)) {
            throw ("win_shim C# source directory not found: " + $winShimDir)
        }
        $csRawPaths = @([System.IO.Directory]::GetFiles($winShimDir, "*.cs"))
        if ($csRawPaths.Count -eq 0) {
            throw ("no *.cs sources found in " + $winShimDir)
        }
        $csNames = New-Object string[] $csRawPaths.Count
        for ($i = 0; $i -lt $csRawPaths.Count; $i++) {
            $csNames[$i] = [System.IO.Path]::GetFileName($csRawPaths[$i])
        }
        [System.Array]::Sort($csNames, [System.StringComparer]::Ordinal)
        $csSources = New-Object string[] $csNames.Length
        $csCompileSources = New-Object string[] $csNames.Length
        $ms = New-Object System.IO.MemoryStream
        try {
            $hdrBytes = [System.Text.Encoding]::UTF8.GetBytes("v2|" + $PSVersionTable.PSVersion.ToString() + "|" + $PSVersionTable.CLRVersion.ToString() + "|" + [IntPtr]::Size + "|debug=0|")
            $ms.Write($hdrBytes, 0, $hdrBytes.Length)
            for ($i = 0; $i -lt $csNames.Length; $i++) {
                $fullPath = Join-Path $winShimDir $csNames[$i]
                $rawBytes = [System.IO.File]::ReadAllBytes($fullPath)
                $srcText = [System.IO.File]::ReadAllText($fullPath, [System.Text.Encoding]::ASCII)
                $csSources[$i] = $srcText
                $csCompileSources[$i] = "#line 1 `"win_shim/" + $csNames[$i] + "`"`n" + $srcText
                $nameBytes = [System.Text.Encoding]::UTF8.GetBytes("win_shim/" + $csNames[$i])
                $lenBytes = [System.Text.Encoding]::ASCII.GetBytes($rawBytes.Length.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                $ms.Write($nameBytes, 0, $nameBytes.Length)
                $ms.WriteByte(0)
                $ms.Write($lenBytes, 0, $lenBytes.Length)
                $ms.WriteByte(0)
                if ($rawBytes.Length -gt 0) {
                    $ms.Write($rawBytes, 0, $rawBytes.Length)
                }
                $ms.WriteByte(0)
            }
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try {
                $hashBytes = $sha.ComputeHash($ms.ToArray())
            } finally {
                $sha.Dispose()
            }
            $csHash = ([System.BitConverter]::ToString($hashBytes, 0, 8) -replace '-', '').ToLowerInvariant()
        } finally {
            $ms.Dispose()
        }
    } catch {
        Write-InitFailureAndExit $_
    }
    $csCompileError = ""
    try {
        $resolvedCacheDir = ""
        if (-not [string]::IsNullOrWhiteSpace($CacheDir)) {
            $resolvedCacheDir = $CacheDir.Trim()
        } elseif (-not [string]::IsNullOrWhiteSpace($env:AGENT_VOICE_INPUT_WIN_CACHE_DIR)) {
            $resolvedCacheDir = $env:AGENT_VOICE_INPUT_WIN_CACHE_DIR.Trim()
        } elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            $resolvedCacheDir = Join-Path $env:LOCALAPPDATA "agent-voice-input\cache"
        }
        if (-not [string]::IsNullOrWhiteSpace($resolvedCacheDir)) {
            $dllName = "VoiceWinShim_$csHash.dll"
            $dllPath = Join-Path $resolvedCacheDir $dllName
            if (Test-Path -LiteralPath $dllPath) {
                try {
                    Add-Type -LiteralPath $dllPath -ErrorAction Stop
                    if ('VoiceWinShim' -as [type]) { $csCacheHit = $true }
                } catch {
                    if (-not ('VoiceWinShim' -as [type])) {
                        try { [System.IO.File]::Move($dllPath, "$dllPath.bad.$PID") } catch { }
                    }
                }
            }
            if (-not ('VoiceWinShim' -as [type])) {
                try { [void][System.IO.Directory]::CreateDirectory($resolvedCacheDir) } catch { }
                $randHex = [Guid]::NewGuid().ToString("N").Substring(0, 8)
                $tmpDll = "$dllPath.$PID.$randHex.tmp.dll"
                $provider = New-Object Microsoft.CSharp.CSharpCodeProvider
                try {
                    $cp = New-Object System.CodeDom.Compiler.CompilerParameters
                    $cp.GenerateInMemory = $false
                    $cp.GenerateExecutable = $false
                    $cp.IncludeDebugInformation = $false
                    $cp.OutputAssembly = $tmpDll
                    $refLocs = @(
                        [System.Uri].Assembly.Location,
                        [System.Linq.Enumerable].Assembly.Location,
                        [System.Drawing.Bitmap].Assembly.Location,
                        [System.Windows.Forms.Form].Assembly.Location
                    )
                    foreach ($loc in $refLocs) {
                        if ((-not [string]::IsNullOrWhiteSpace($loc)) -and (-not $cp.ReferencedAssemblies.Contains($loc))) {
                            [void]$cp.ReferencedAssemblies.Add($loc)
                        }
                    }
                    $cr = $provider.CompileAssemblyFromSource($cp, [string[]]$csCompileSources)
                    if ($cr.Errors.HasErrors) {
                        $errMsgs = New-Object 'System.Collections.Generic.List[string]'
                        $hasNonWarningErr = $false
                        $onlyOutputWriteErr = $true
                        foreach ($ce in $cr.Errors) {
                            if (-not $ce.IsWarning) {
                                $hasNonWarningErr = $true
                                $isOutWriteErr = [string]::Equals($ce.ErrorNumber, "CS0016", [StringComparison]::OrdinalIgnoreCase) -or [string]::Equals($ce.ErrorNumber, "CS1567", [StringComparison]::OrdinalIgnoreCase)
                                if (-not $isOutWriteErr) {
                                    $onlyOutputWriteErr = $false
                                }
                                if ($errMsgs.Count -lt 8) {
                                    $errFile = if (-not [string]::IsNullOrWhiteSpace($ce.FileName)) { $ce.FileName } else { "win_shim/*.cs" }
                                    $errMsgs.Add(("{0}({1},{2}): error {3}: {4}" -f $errFile, $ce.Line, $ce.Column, $ce.ErrorNumber, $ce.ErrorText))
                                }
                            }
                        }
                        if ($hasNonWarningErr -and $onlyOutputWriteErr) {
                            [Console]::Error.WriteLine("win_shim cache write failed (CS0016/CS1567), falling back to uncached Add-Type: " + ($errMsgs.ToArray() -join "; "))
                        } else {
                            $csCompileError = ($errMsgs.ToArray() -join "; ")
                        }
                    } elseif (Test-Path -LiteralPath $tmpDll) {
                        try {
                            [System.IO.File]::Move($tmpDll, $dllPath)
                        } catch [System.IO.IOException] {
                            try { [System.IO.File]::Delete($tmpDll) } catch { }
                        }
                        if (Test-Path -LiteralPath $dllPath) {
                            Add-Type -LiteralPath $dllPath -ErrorAction Stop
                        }
                    }
                } finally {
                    $provider.Dispose()
                    if (Test-Path -LiteralPath $tmpDll) {
                        try { [System.IO.File]::Delete($tmpDll) } catch { }
                    }
                }
                try {
                    $staleCutoff = [DateTime]::UtcNow.AddMinutes(-5)
                    foreach ($f in [System.IO.Directory]::GetFiles($resolvedCacheDir, "VoiceWinShim_*")) {
                        $fn = [System.IO.Path]::GetFileName($f)
                        if ($fn.Equals($dllName, [StringComparison]::OrdinalIgnoreCase)) { continue }
                        if ($fn.EndsWith(".tmp.dll", [StringComparison]::OrdinalIgnoreCase)) {
                            try {
                                if ([System.IO.File]::GetLastWriteTimeUtc($f) -gt $staleCutoff) { continue }
                            } catch { continue }
                        }
                        try { [System.IO.File]::Delete($f) } catch { }
                    }
                } catch { }
            }
        }
    } catch { }
    if (-not ('VoiceWinShim' -as [type])) {
        if (-not [string]::IsNullOrWhiteSpace($csCompileError)) {
            Write-InitFailureAndExit ("C# compile failed: " + $csCompileError)
        }
        try {
            $usingSeen = @{}
            $usingLines = New-Object 'System.Collections.Generic.List[string]'
            $bodyParts = New-Object 'System.Collections.Generic.List[string]'
            for ($i = 0; $i -lt $csSources.Length; $i++) {
                $srcLines = $csSources[$i] -split "`r?`n"
                $bodyLines = New-Object 'System.Collections.Generic.List[string]'
                $inHeader = $true
                $bodyStartLine = 1
                for ($li = 0; $li -lt $srcLines.Length; $li++) {
                    $ln = $srcLines[$li]
                    if ($inHeader) {
                        $trimmed = $ln.Trim()
                        if ($trimmed -match '^using\s+[A-Za-z0-9_.]+\s*;$') {
                            if (-not $usingSeen.ContainsKey($trimmed)) {
                                $usingSeen[$trimmed] = $true
                                $usingLines.Add($trimmed)
                            }
                            continue
                        }
                        if ($trimmed -eq '') { continue }
                        $inHeader = $false
                        $bodyStartLine = $li + 1
                    }
                    $bodyLines.Add($ln)
                }
                $bodyParts.Add(("#line " + $bodyStartLine + " `"win_shim/" + $csNames[$i] + "`"`n" + ($bodyLines.ToArray() -join "`n")))
            }
            $mergedCsSource = ($usingLines.ToArray() -join "`n") + "`n`n" + ($bodyParts.ToArray() -join "`n`n")
            Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition $mergedCsSource
        } catch {
            Write-InitFailureAndExit $_
        }
    }
} else {
    $csCacheHit = $true
}

function Invoke-Tone([string]$kind) { [VoiceWinShim]::PlayTone($kind, [bool]$Beep) }
function Get-ClipFailReason([long]$startTick, [int]$deadlineMs) {
    if ($deadlineMs -gt 0 -and [VoiceWinShim]::IsDeadlineExceeded($startTick, $deadlineMs)) { return "paste_deadline_exceeded" }
    return "clipboard_failed"
}
function Set-ClipboardSafe([string]$val, [long]$startTick = 0L, [int]$deadlineMs = 0) {
    for ($i = 0; $i -lt 3; $i++) {
        if ($deadlineMs -gt 0 -and [VoiceWinShim]::IsDeadlineExceeded($startTick, $deadlineMs)) { return $false }
        try {
            if ([VoiceWinShim]::WriteClipTextOnce($val, $false, $startTick, $deadlineMs)) { return $true }
        } catch { }
        if ($deadlineMs -gt 0 -and [VoiceWinShim]::IsDeadlineExceeded($startTick, $deadlineMs)) { return $false }
        if ($i -lt 2) { Start-Sleep -Milliseconds 10 }
    }
    return $false
}
function Set-ClipboardTransient([string]$val, [long]$startTick = 0L, [int]$deadlineMs = 0) {
    for ($i = 0; $i -lt 3; $i++) {
        if ($deadlineMs -gt 0 -and [VoiceWinShim]::IsDeadlineExceeded($startTick, $deadlineMs)) { return $false }
        try {
            if ([VoiceWinShim]::WriteClipTextOnce($val, $true, $startTick, $deadlineMs)) { return $true }
        } catch { }
        if ($deadlineMs -gt 0 -and [VoiceWinShim]::IsDeadlineExceeded($startTick, $deadlineMs)) { return $false }
        if ($i -lt 2) { Start-Sleep -Milliseconds 10 }
    }
    return $false
}
function Get-TitleSessionToken([string]$title) {
    if ([string]::IsNullOrWhiteSpace($title)) { return "" }
    $clean = [regex]::Replace($title, '\s*\[@[^\]]*\]', '').Trim()
    if ([string]::IsNullOrWhiteSpace($clean)) { return "" }
    $m1 = [regex]::Matches($clean, '(?i)\[(agy|claude|codex):#?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8,36})\]')
    if ($m1.Count -gt 0) { return $m1[$m1.Count - 1].Groups[2].Value.ToLowerInvariant() }
    $m2 = [regex]::Matches($clean, '(?i)\[[#\u00a7]([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{6,36})\](?=$|[^0-9A-Za-z_])')
    if ($m2.Count -gt 0) { return $m2[$m2.Count - 1].Groups[1].Value.ToLowerInvariant() }
    $m3 = [regex]::Matches($clean, '(?i)(?:^|[^0-9a-f-])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=$|[^0-9a-f-])')
    if ($m3.Count -gt 0) { return $m3[$m3.Count - 1].Groups[1].Value.ToLowerInvariant() }
    return ""
}

try {
    [VoiceWinShim]::EnableDpiAwareness()
} catch {
    Write-InitFailureAndExit $_
}

if ($Mode -eq "self-check") {
    try {
        $parseErrors = $null
        if (-not [string]::IsNullOrWhiteSpace($PSCommandPath)) {
            [void][System.Management.Automation.Language.Parser]::ParseFile($PSCommandPath, [ref]$null, [ref]$parseErrors)
            if ($null -ne $parseErrors -and $parseErrors.Count -gt 0) {
                throw ("AST parse errors: " + $parseErrors.Count)
            }
        }
        $hotkeyFsmChecks = 0
        $hudLayoutCases = [VoiceWinShim]::RunHeadlessSelfCheck([ref]$hotkeyFsmChecks)
        $t1 = "agy " + ([char]0x00B7) + " ~/repo [#a1b2c3d4]"
        if ((Get-TitleSessionToken $t1) -ne "a1b2c3d4") { throw "Get-TitleSessionToken hash tag failed" }
        $tSec = "claude [" + ([char]0x00A7) + "AbCdEf] [@devbox]"
        if ((Get-TitleSessionToken $tSec) -ne "abcdef") { throw "Get-TitleSessionToken section 6-hex tag failed" }
        if ((Get-TitleSessionToken "codex [#1a2b3c][@devbox]") -ne "1a2b3c") { throw "Get-TitleSessionToken 6-hex host suffix failed" }
        if ((Get-TitleSessionToken "claude [claude:#DEADBEEF]") -ne "deadbeef") { throw "Get-TitleSessionToken prefixed tag failed" }
        if ((Get-TitleSessionToken "feat/codex-config") -ne "") { throw "Get-TitleSessionToken false positive" }
        if ((Get-TitleSessionToken "old [#11112222] new [#33334444]") -ne "33334444") { throw "Get-TitleSessionToken multi-tag tail failed" }
        $protoSample = (@{ type = "reply"; reply = "ping"; seq_id = 1; ok = $true } | ConvertTo-Json -Compress) | ConvertFrom-Json
        if ((-not $protoSample.ok) -or ($protoSample.reply -ne "ping") -or ([int]$protoSample.seq_id -ne 1)) {
            throw "JSON roundtrip check failed"
        }
        $realClipOk = $false
        if ($RealClipboard) {
            [VoiceWinShim]::RunRealWin32ClipStoreSelfCheck()
            $realClipOk = $true
        }
        Write-JsonLine @{ ok = $true; mode = "self-check"; ps_version = $PSVersionTable.PSVersion.ToString(); clr_version = $PSVersionTable.CLRVersion.ToString(); cache_hit = [bool]$csCacheHit; hud_layout_cases = [int]$hudLayoutCases; hotkey_fsm_checks = [int]$hotkeyFsmChecks; real_clipboard_ok = [bool]$realClipOk }
        exit 0
    } catch {
        [Console]::Error.WriteLine("win_shim self-check failed: " + [string]$_.Exception.Message)
        exit 1
    }
}
if ($Mode -eq "clip-self-check") {
    [VoiceWinShim]::RunClipStateSelfCheck()
    Write-JsonLine @{ ok = $true; mode = "clip-self-check" }
    exit 0
}
if ($Mode -eq "probe") {
    $hwnd = 0L; $proc = ""; $title = ""; [VoiceWinShim]::QueryForeground([ref]$hwnd, [ref]$proc, [ref]$title)
    $wasapiState = ""; $wasapiOk = [VoiceWinShim]::ProbeDefaultWasapiCapture([ref]$wasapiState)
    Write-JsonLine @{ ok = $true; ps_version = $PSVersionTable.PSVersion.ToString(); wasapi_ok = [bool]$wasapiOk; wasapi_state = [string]$wasapiState; wave_in_devs = [VoiceWinShim]::waveInGetNumDevs(); hwnd = $hwnd; process = $proc; title = $title; ui_culture = [VoiceWinShim]::GetHostUiCulture(); dpi = [VoiceWinShim]::GetTargetDpi([IntPtr]$hwnd) }
    exit 0
}

try {
    $targetVk = [VoiceWinShim]::ResolveKeyName($Hotkey)
} catch {
    Write-JsonLine @{ ok = $false; error = [string]$_.Exception.Message }
    exit 1
}
$isPtt = ($TriggerMode -eq "ptt" -or $TriggerMode -eq "push_to_talk")
$AllowedTerminals = @("windowsterminal", "openconsole", "mintty", "wezterm", "wezterm-gui", "alacritty", "ghostty", "conhost", "putty", "tabby", "hyper", "kitty", "rio", "warp")
if (-not [string]::IsNullOrWhiteSpace($AllowedTerminalsCsv)) {
    $AllowedTerminals = @($AllowedTerminalsCsv.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ -ne "" })
}
$bridgeReady = $false
try {
    if ($Mode -eq "key-test") {
        [VoiceWinShim]::StartHook($targetVk, $isPtt, $TapMaxMs, $DebounceMs, $true, ($Seconds * 1000), $HoldCancelMs, $HoldHintMs)
        Write-JsonLine @{ event = "key_test_ready"; hotkey = $Hotkey; target_vk = ("0x{0:X2}" -f $targetVk); mode = $TriggerMode; seconds = $Seconds; tap_max_ms = $TapMaxMs; hold_cancel_ms = $HoldCancelMs; hold_hint_ms = $HoldHintMs }
        $bridgeReady = $true
        while ([VoiceWinShim]::PumpOnce()) {
            $ev = New-Object ShimEvent
            while ([VoiceWinShim]::EvtQueue.TryDequeue([ref]$ev)) {
                $kname = try { ([System.Windows.Forms.Keys][int]$ev.Vk).ToString() } catch { "VK_$($ev.Vk)" }
                if ($ev.Kind -eq "hold_cancel") {
                    Write-JsonLine @{ event = $ev.Kind; hold_ms = [int]$ev.HoldMs; vk = ("0x{0:X2}" -f $ev.Vk); key_name = $kname; scan = $ev.Scan; extended = $ev.Extended; is_down = $ev.IsDown; tainted = $ev.Tainted }
                } else {
                    Write-JsonLine @{ event = $ev.Kind; vk = ("0x{0:X2}" -f $ev.Vk); key_name = $kname; scan = $ev.Scan; extended = $ev.Extended; is_down = $ev.IsDown; tainted = $ev.Tainted }
                }
            }
        }
        exit 0
    }
    [VoiceWinShim]::StartHook($targetVk, $isPtt, $TapMaxMs, $DebounceMs, $false, 0, $HoldCancelMs, $HoldHintMs)
    [VoiceWinShim]::StartStdinReader()
    Write-JsonLine @{ event = "ready"; target_vk = ("0x{0:X2}" -f $targetVk); mode = $TriggerMode; wave_in_devs = [VoiceWinShim]::waveInGetNumDevs(); ui_culture = [VoiceWinShim]::GetHostUiCulture() }
    $bridgeReady = $true
    :bridgeLoop while ([VoiceWinShim]::PumpOnce()) {
        try {
            $ev = New-Object ShimEvent
            while ([VoiceWinShim]::EvtQueue.TryDequeue([ref]$ev)) {
                if ($ev.Kind -eq "rec_lost") {
                    Write-JsonLine @{ event = "rec_lost"; reason = [string]$ev.Reason; rec_gen = [long]$ev.RecGen }
                } elseif ($ev.Kind -eq "rec_recovered") {
                    Write-JsonLine @{ event = "rec_recovered"; gap_ms = [int]$ev.GapMs; rec_gen = [long]$ev.RecGen }
                } elseif ($ev.Kind -eq "hold_cancel") {
                    Write-JsonLine @{ event = "hold_cancel"; hold_ms = [int]$ev.HoldMs; hwnd = $ev.Hwnd; process = $ev.Process; title = $ev.Title; ts_ms = $ev.TsMs }
                } elseif ($ev.Kind -eq "power_resume") {
                    Write-JsonLine @{ event = "power_resume"; ts_ms = $ev.TsMs }
                } else {
                    Write-JsonLine @{ event = $ev.Kind; hwnd = $ev.Hwnd; process = $ev.Process; title = $ev.Title; ts_ms = $ev.TsMs }
                }
            }
        } catch { }
        $cmdItem = $null
        while ([VoiceWinShim]::CmdQueue.TryDequeue([ref]$cmdItem)) {
            $rawCmd = if ($null -ne $cmdItem) { [string]$cmdItem.Line } else { $null }
            $cmdEnqueuedTick = if ($null -ne $cmdItem) { [long]$cmdItem.EnqueuedTick } else { [System.Diagnostics.Stopwatch]::GetTimestamp() }
            if ([string]::IsNullOrWhiteSpace($rawCmd)) { continue }
            $seq = 0; $op = "unknown"
            try {
                $cmdObj = $rawCmd | ConvertFrom-Json; $op = [string]$cmdObj.cmd; $seq = if ($null -ne $cmdObj.seq_id) { [int]$cmdObj.seq_id } else { 0 }
                if ($op -eq "ping") {
                    $pingReply = @{ type = "reply"; reply = "ping"; seq_id = $seq; ok = $true; wave_in_devs = [VoiceWinShim]::waveInGetNumDevs() }
                    if ($null -ne $cmdObj.echo_b64) { $pingReply.echo_b64 = [string]$cmdObj.echo_b64 }
                    Write-JsonLine $pingReply
                }
                elseif ($op -eq "fg_window") { $h = 0L; $p = ""; $t = ""; [VoiceWinShim]::QueryForeground([ref]$h, [ref]$p, [ref]$t); Write-JsonLine @{ type = "reply"; reply = "fg_window"; seq_id = $seq; ok = $true; hwnd = $h; process = $p; title = $t } }
                elseif ($op -eq "key_state") { Write-JsonLine @{ type = "reply"; reply = "key_state"; seq_id = $seq; ok = $true; hotkey_held = [bool][VoiceWinShim]::IsAnyPasteBlockingKeyDown() } }
                elseif ($op -eq "beep") { Invoke-Tone ([string]$cmdObj.kind); Write-JsonLine @{ type = "reply"; reply = "beep"; seq_id = $seq; ok = $true } }
                elseif ($op -eq "hud") {
                    $dms = if ($null -ne $cmdObj.duration_ms) { [int]$cmdObj.duration_ms } else { 0 }
                    $maxDur = if ($null -ne $cmdObj.max_duration_sec) { [int]$cmdObj.max_duration_sec } else { 0 }
                    $warnRem = if ($null -ne $cmdObj.warn_remain_sec) { [int]$cmdObj.warn_remain_sec } else { 0 }
                    $lyr = if ($null -ne $cmdObj.layer) { [string]$cmdObj.layer } else { "" }
                    $clrMain = if ($null -ne $cmdObj.clear_main) { [bool]$cmdObj.clear_main } else { $null }
                    $clrToast = if ($null -ne $cmdObj.clear_toast) { [bool]$cmdObj.clear_toast } else { $null }
                    $bgCnt = if ($null -ne $cmdObj.bg_count) { [int]$cmdObj.bg_count } else { 0 }
                    [VoiceWinShim]::UpdateHud([string]$cmdObj.state, [string]$cmdObj.text, $dms, [string]$cmdObj.position, $maxDur, $warnRem, $lyr, $clrMain, $clrToast, $bgCnt)
                    Write-JsonLine @{ type = "reply"; reply = "hud"; seq_id = $seq; ok = $true }
                } elseif ($op -eq "hud_level") {
                    $lvl = if ($null -ne $cmdObj.level) { [float]$cmdObj.level } else { 0.0 }
                    [VoiceWinShim]::SetExternalAudioLevel($lvl)
                    if ($seq -gt 0) { Write-JsonLine @{ type = "reply"; reply = "hud_level"; seq_id = $seq; ok = $true } }
                } elseif ($op -eq "paste") {
                    $pasteDeadlineMs = if ($null -ne $cmdObj.deadline_ms) { [int]$cmdObj.deadline_ms } else { 0 }
                    if ($pasteDeadlineMs -gt 0 -and [VoiceWinShim]::IsDeadlineExceeded($cmdEnqueuedTick, $pasteDeadlineMs)) {
                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $false; reason = "paste_deadline_exceeded"; fg_process = "" }
                    } else {
                        $txt = [string]$cmdObj.text
                        $fbTxt = if ($null -ne $cmdObj.fallback_text -and ([string]$cmdObj.fallback_text).Length -gt 0) { [string]$cmdObj.fallback_text } else { $txt }
                        $forceClipOnly = if ($null -ne $cmdObj.force_clipboard_only) { [bool]$cmdObj.force_clipboard_only } else { $false }
                        $suppressBeep = if ($null -ne $cmdObj.suppress_beep) { [bool]$cmdObj.suppress_beep } else { $false }
                        $expectTerm = if ($null -ne $cmdObj.expect_terminal) { [bool]$cmdObj.expect_terminal } else { $true }
                        $expectProc = if ($null -ne $cmdObj.expect_process) { ([string]$cmdObj.expect_process -replace '\.exe$','').Trim().ToLowerInvariant() } else { "" }
                        $expectTok = if ($null -ne $cmdObj.expect_session_token) { ([string]$cmdObj.expect_session_token).Trim().ToLowerInvariant() } else { "" }
                        $pasteChord = if ($null -ne $cmdObj.paste_chord -and ([string]$cmdObj.paste_chord).Length -gt 0) { [string]$cmdObj.paste_chord } else { "ctrl_v" }
                        $thwnd = if ($null -ne $cmdObj.target_hwnd) { [long]$cmdObj.target_hwnd } else { 0L }
                        $h = 0L; $p = ""; $t = ""; [VoiceWinShim]::QueryForeground([ref]$h, [ref]$p, [ref]$t)
                        $pNorm = ($p -replace '\.exe$','').ToLowerInvariant()
                        $curTok = if ($expectTok -ne "") { Get-TitleSessionToken $t } else { "" }
                        $tokConflict = ($expectTok -ne "") -and ($curTok -ne $expectTok)
                        $winMatch = (($thwnd -le 0) -or ($h -eq $thwnd)) -and (($expectProc -eq "") -or ($pNorm -eq $expectProc)) -and ((-not $expectTerm) -or ($AllowedTerminals -contains $pNorm)) -and (-not $tokConflict)
                        if ($winMatch -and (-not $forceClipOnly)) {
                            if ([VoiceWinShim]::IsAnyPasteBlockingKeyDown()) {
                                $clipOk = Set-ClipboardSafe $fbTxt $cmdEnqueuedTick $pasteDeadlineMs
                                if ($clipOk -and (-not $suppressBeep)) { Invoke-Tone "focus" }
                                Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "hotkey_held" } else { Get-ClipFailReason $cmdEnqueuedTick $pasteDeadlineMs }); fg_process = $p }
                            } else {
                                $clipOk = Set-ClipboardTransient $txt $cmdEnqueuedTick $pasteDeadlineMs
                                if (-not $clipOk) {
                                    Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $false; reason = (Get-ClipFailReason $cmdEnqueuedTick $pasteDeadlineMs); fg_process = $p }
                                } else {
                                    $h2 = 0L; $p2 = ""; $t2 = ""; [VoiceWinShim]::QueryForeground([ref]$h2, [ref]$p2, [ref]$t2)
                                    $p2Norm = ($p2 -replace '\.exe$','').ToLowerInvariant()
                                    $curTok2 = if ($expectTok -ne "") { Get-TitleSessionToken $t2 } else { "" }
                                    $tokConflict2 = ($expectTok -ne "") -and ($curTok2 -ne $expectTok)
                                    $winMatch2 = (($thwnd -le 0) -or ($h2 -eq $thwnd)) -and (($expectProc -eq "") -or ($p2Norm -eq $expectProc)) -and ((-not $expectTerm) -or ($AllowedTerminals -contains $p2Norm)) -and (-not $tokConflict2)
                                    if (-not $winMatch2) {
                                        if ($fbTxt -ne $txt) { $clipOk = Set-ClipboardSafe $fbTxt $cmdEnqueuedTick $pasteDeadlineMs }
                                        else { $clipOk = Set-ClipboardSafe $txt $cmdEnqueuedTick $pasteDeadlineMs }
                                        if ($clipOk -and (-not $suppressBeep)) { Invoke-Tone "focus" }
                                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "focus_changed" } else { Get-ClipFailReason $cmdEnqueuedTick $pasteDeadlineMs }); fg_process = $p2 }
                                    } elseif ([VoiceWinShim]::IsAnyPasteBlockingKeyDown()) {
                                        $clipOk = Set-ClipboardSafe $fbTxt $cmdEnqueuedTick $pasteDeadlineMs
                                        if ($clipOk -and (-not $suppressBeep)) { Invoke-Tone "focus" }
                                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "hotkey_held" } else { Get-ClipFailReason $cmdEnqueuedTick $pasteDeadlineMs }); fg_process = $p2 }
                                    } elseif ($pasteDeadlineMs -gt 0 -and [VoiceWinShim]::IsDeadlineExceeded($cmdEnqueuedTick, $pasteDeadlineMs)) {
                                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $false; reason = "paste_deadline_exceeded"; fg_process = $p2 }
                                    } else {
                                        $sent = [VoiceWinShim]::SendPasteChord($pasteChord)
                                        if ($sent) {
                                            Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $true; clipboard = $true; reason = "ok"; fg_process = $p2 }
                                        } else {
                                            if ($fbTxt -ne $txt) { $clipOk = Set-ClipboardSafe $fbTxt $cmdEnqueuedTick $pasteDeadlineMs }
                                            else { $clipOk = Set-ClipboardSafe $txt $cmdEnqueuedTick $pasteDeadlineMs }
                                            if ($clipOk -and (-not $suppressBeep)) { Invoke-Tone "focus" }
                                            Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "send_input_failed" } else { Get-ClipFailReason $cmdEnqueuedTick $pasteDeadlineMs }); fg_process = $p2 }
                                        }
                                    }
                                }
                            }
                        } else {
                            $clipOk = Set-ClipboardSafe $fbTxt $cmdEnqueuedTick $pasteDeadlineMs
                            if ($clipOk -and (-not $suppressBeep)) { Invoke-Tone "focus" }
                            Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "focus_changed" } else { Get-ClipFailReason $cmdEnqueuedTick $pasteDeadlineMs }); fg_process = $p }
                        }
                    }
                } elseif ($op -eq "rec_start") {
                    $loopback = if ($null -ne $cmdObj.loopback) { [bool]$cmdObj.loopback } else { $false }
                    $recGen = if ($null -ne $cmdObj.rec_gen) { [long]$cmdObj.rec_gen } else { 0L }
                    $recErr = ""; $okStart = [VoiceWinShim]::StartRecording($loopback, $recGen, [ref]$recErr)
                    Write-JsonLine @{ type = "reply"; reply = "rec_start"; seq_id = $seq; ok = [bool]$okStart; rec_error = [string]$recErr }
                } elseif ($op -eq "rec_stop") {
                    $recGen = if ($null -ne $cmdObj.rec_gen) { [long]$cmdObj.rec_gen } else { 0L }
                    $discard = ($null -ne $cmdObj.discard -and [bool]$cmdObj.discard)
                    if ($discard) {
                        [VoiceWinShim]::CancelRecording($recGen)
                        Write-JsonLine @{ type = "reply"; reply = "rec_stop"; seq_id = $seq; ok = $false; rec_gen = $recGen; pcm_bytes = 0; wav_b64 = ""; rec_error = "" }
                    } else {
                        $pcmBytes = 0; $recErr = ""; $wavBytes = [VoiceWinShim]::StopRecording($recGen, [ref]$pcmBytes, [ref]$recErr, $true)
                        $b64 = if ($wavBytes -and $wavBytes.Length -gt 0) { [Convert]::ToBase64String($wavBytes) } else { "" }
                        $wavBytes = $null
                        $okStop = ($b64.Length -gt 0)
                        Write-JsonLine @{ type = "reply"; reply = "rec_stop"; seq_id = $seq; ok = $okStop; rec_gen = $recGen; pcm_bytes = $pcmBytes; wav_b64 = $b64; rec_error = [string]$recErr }
                        $b64 = $null
                    }
                } elseif ($op -eq "rec_release" -or $op -eq "rec_ack") {
                    $recGen = if ($null -ne $cmdObj.rec_gen) { [long]$cmdObj.rec_gen } else { 0L }
                    [VoiceWinShim]::ReleaseStoppedRecording($recGen)
                    if ($seq -gt 0) { Write-JsonLine @{ type = "reply"; reply = $op; seq_id = $seq; ok = $true; rec_gen = $recGen } }
                } elseif ($op -eq "rec_cancel") {
                    $recGen = if ($null -ne $cmdObj.rec_gen) { [long]$cmdObj.rec_gen } else { 0L }
                    [VoiceWinShim]::CancelRecording($recGen)
                    Write-JsonLine @{ type = "reply"; reply = "rec_cancel"; seq_id = $seq; ok = $true; rec_gen = $recGen }
                } elseif ($op -eq "quit") { break bridgeLoop }
                else { Write-JsonLine @{ type = "reply"; reply = $op; seq_id = $seq; ok = $false; error = "unknown_cmd:$op" } }
            } catch {
                if ($seq -gt 0 -or $op -ne "hud_level") { Write-JsonLine @{ type = "reply"; reply = $op; seq_id = $seq; ok = $false; error = [string]$_.Exception.Message } }
            }
        }
    }
} catch {
    if (-not $bridgeReady) {
        Write-JsonLine @{ ok = $false; error = [string]$_.Exception.Message }
    }
    exit 1
} finally {
    [VoiceWinShim]::StopHook()
}
