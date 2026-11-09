# SpeakEasy mobile build script (Capacitor -> Android APK)
# ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI, so non-ASCII
# characters here would corrupt the parser.
#
# Usage:
#   .\mobile\build-apk.ps1 -ApiBase http://47.104.24.236:8000            # cloud (default)
#   .\mobile\build-apk.ps1 -ApiBase http://192.168.0.108:8000 -Mode debug # LAN test
#   .\mobile\build-apk.ps1 -ApiBase http://127.0.0.1:8000 -AllowLocalhost # dev only
#
# Output: deliverables/SpeakEasy-v<version>-<mode>.apk
param(
    [string]$ApiBase = "http://47.104.24.236:8000",
    [ValidateSet("debug", "release")][string]$Mode = "release",
    [switch]$AllowLocalhost,
    [int]$VersionCode = 0,
    [string]$VersionName = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Fail([string]$msg) { Write-Error $msg; exit 1 }

# Guard: an APK must never default to localhost
$isLocal = ($ApiBase -match "localhost") -or ($ApiBase -match "127\.0\.0\.1")
if ($isLocal -and (-not $AllowLocalhost)) {
    Fail "APK API base must not be localhost/127.0.0.1. Pass -AllowLocalhost only for local dev."
}
if ($ApiBase -notmatch "^https?://") { Fail "ApiBase must start with http(s)://, got: $ApiBase" }

Write-Host "==> 1/6 build frontend (NEXT_PUBLIC_API_BASE=$ApiBase)"
Set-Location "$Root/frontend"
$env:NEXT_PUBLIC_API_BASE = $ApiBase
npm run build
if ($LASTEXITCODE -ne 0) { Fail "frontend build failed" }
if (-not (Test-Path "$Root/frontend/out/index.html")) { Fail "missing frontend/out/index.html" }

Write-Host "==> 2/6 copy static assets to mobile/www"
Set-Location $Root
if (Test-Path "$Root/mobile/www") { Remove-Item -Recurse -Force "$Root/mobile/www" }
New-Item -ItemType Directory -Path "$Root/mobile/www" | Out-Null
Copy-Item -Recurse -Force "$Root/frontend/out/*" "$Root/mobile/www/"

Write-Host "==> 2.5/6 inject remote-bootstrap shell"
$shellBase = $ApiBase.TrimEnd("/")
$bootstrap = Get-Content "$Root/mobile/bootstrap/index.html" -Raw -Encoding UTF8
$bootstrap = $bootstrap.Replace("__API_BASE__", $shellBase)
[IO.File]::WriteAllText("$Root/mobile/www/index.html", $bootstrap, (New-Object System.Text.UTF8Encoding($false)))
$offline = Get-Content "$Root/mobile/bootstrap/offline.html" -Raw -Encoding UTF8
$offline = $offline.Replace("__API_BASE__", $shellBase)
[IO.File]::WriteAllText("$Root/mobile/www/offline.html", $offline, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "    shell -> $shellBase"

$updaterFile = "$Root/mobile/android/app/src/main/java/com/speakeasy/app/UpdateChecker.java"
if (Test-Path $updaterFile) {
    $updaterText = [IO.File]::ReadAllText($updaterFile)
    $updaterText = [regex]::Replace($updaterText, 'String BASE = "[^"]*"', "String BASE = `"$shellBase`"")
    [IO.File]::WriteAllText($updaterFile, $updaterText, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "    updater BASE -> $shellBase"
}

Write-Host "==> 3/6 capacitor sync"
Set-Location "$Root/mobile"
npx cap sync android
if ($LASTEXITCODE -ne 0) { Fail "cap sync failed" }

Write-Host "==> 3.5/6 ensure android sdk/jdk"
# Android SDK: prefer ANDROID_HOME, else the default per-user location.
$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = "$env:LOCALAPPDATA\Android\Sdk" }
if (-not (Test-Path $sdk)) { Fail "Android SDK not found (set ANDROID_HOME). Looked at: $sdk" }
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk
$lp = "$Root/mobile/android/local.properties"
if (-not (Test-Path $lp)) {
    $sdkEscaped = $sdk -replace '\\', '\\\\' -replace ':', '\:'
    "sdk.dir=$sdkEscaped" | Out-File -Encoding ascii $lp
    Write-Host "    wrote local.properties (sdk.dir)"
}
if (-not $env:JAVA_HOME) {
    $javaCmd = Get-Command java -ErrorAction SilentlyContinue
    if ($javaCmd) { $env:JAVA_HOME = Split-Path (Split-Path $javaCmd.Source -Parent) -Parent }
}
Write-Host "    ANDROID_HOME=$env:ANDROID_HOME"
Write-Host "    JAVA_HOME=$env:JAVA_HOME"

if ($VersionCode -gt 0 -or $VersionName -ne "") {
    Write-Host "==> 3.6/6 set versionCode/versionName"
    $gradleFile = "$Root/mobile/android/app/build.gradle"
    $gradleText = [IO.File]::ReadAllText($gradleFile)
    if ($VersionCode -gt 0) {
        $gradleText = [regex]::Replace($gradleText, 'versionCode\s+\d+', "versionCode $VersionCode")
    }
    if ($VersionName -ne "") {
        $gradleText = [regex]::Replace($gradleText, 'versionName\s+"[^"]*"', "versionName `"$VersionName`"")
    }
    [IO.File]::WriteAllText($gradleFile, $gradleText, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "    versionCode=$VersionCode versionName=$VersionName"
}

Write-Host "==> 4/6 gradle assemble ($Mode)"
Set-Location "$Root/mobile/android"
$task = if ($Mode -eq "release") { "assembleRelease" } else { "assembleDebug" }
# NOTE: 2>&1 is required - javac/gradle write warnings to stderr, and with
# $ErrorActionPreference=Stop Windows PowerShell would abort on them.
& .\gradlew.bat $task --console=plain 2>&1 | ForEach-Object { "$_" } | Select-Object -Last 40
if ($LASTEXITCODE -ne 0) { Fail "gradle build failed" }

Write-Host "==> 5/6 collect apk"
$apkDir = if ($Mode -eq "release") { "app/build/outputs/apk/release" } else { "app/build/outputs/apk/debug" }
$apk = Get-ChildItem "$apkDir/*.apk" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $apk) { Fail "apk not found in $apkDir" }
New-Item -ItemType Directory -Force -Path "$Root/deliverables" | Out-Null
$gradle = Get-Content "app/build.gradle" -Raw
$versionMatch = [regex]::Match($gradle, 'versionName\s+"([^"]+)"')
if (-not $versionMatch.Success) { $versionMatch = [regex]::Match($gradle, 'versionName\s+["'']([^"'']+)["'']') }
$version = if ($versionMatch.Success) { $versionMatch.Groups[1].Value } else { "0.0.0" }
$dest = "$Root/deliverables/SpeakEasy-v$version-$Mode.apk"
Copy-Item -Force $apk.FullName $dest

Write-Host "==> 6/6 done"
Write-Host "    API base : $ApiBase"
Write-Host "    APK      : $dest ($([math]::Round((Get-Item $dest).Length/1MB,2)) MB)"
Write-Host "    install   : adb install -r `"$dest`""
Write-Host "    verify    : adb shell dumpsys package com.speakeasy.app"
