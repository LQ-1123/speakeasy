# Remote Web verification orchestrator (public production URL).
# PowerShell launches Chrome; Python attaches over CDP and performs real interactions.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_remote_web_verify.ps1
#        powershell ... -Base https://47-104-24-236.sslip.io
param(
  [string]$Base = "https://47-104-24-236.sslip.io",
  [string]$Python = ".venv\Scripts\python.exe",
  [string]$Chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$profilesRoot = Join-Path $root ".tmp\cdp-profiles-remote\$stamp"
New-Item -ItemType Directory -Force -Path $profilesRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root "artifacts\remote-web-reports") | Out-Null

try {
  Invoke-WebRequest -Uri "$Base/api/v3/health" -TimeoutSec 20 -UseBasicParsing | Out-Null
  Write-Host "[ok] remote health: $Base"
} catch {
  throw "remote not reachable: $Base/api/v3/health"
}

$common = @(
  "--base", $Base,
  "--prefix", "remote-web",
  "--expected", "main,reopen,legacy,mobile,chat,compat"
)

function Get-FreePort {
  for ($i = 0; $i -lt 50; $i++) {
    $candidate = Get-Random -Minimum 9400 -Maximum 9900
    $client = New-Object System.Net.Sockets.TcpClient
    try {
      $client.Connect("127.0.0.1", $candidate)
      $client.Close()
    } catch {
      return $candidate
    }
  }
  throw "no free CDP port"
}

function Get-ProfileChromeProcesses {
  param([string]$Profile)
  try {
    $all = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction Stop
  } catch {
    return @()
  }
  return @($all | Where-Object {
      $cmd = [string]$_.CommandLine
      $cmd -and ($cmd -like "*--user-data-dir=$Profile*" -or $cmd -like "*--user-data-dir=`"$Profile`"*")
    })
}

function Start-CDP {
  param([string]$Name, [string]$Size = "1440,900")
  $port = Get-FreePort
  $profile = Join-Path $profilesRoot $Name
  New-Item -ItemType Directory -Force -Path $profile | Out-Null
  $chromeArgs = @(
    "--headless=new",
    "--remote-debugging-port=$port",
    "--user-data-dir=$profile",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-extensions",
    "--disable-gpu",
    "--hide-scrollbars",
    "--mute-audio",
    "--window-size=$Size",
    "about:blank"
  )
  $proc = Start-Process -FilePath $Chrome -ArgumentList $chromeArgs -PassThru
  $ready = $false
  for ($i = 0; $i -lt 80; $i++) {
    try {
      Invoke-WebRequest -Uri "http://127.0.0.1:$port/json/version" -TimeoutSec 2 -UseBasicParsing | Out-Null
      $ready = $true
      break
    } catch {
      Start-Sleep -Milliseconds 250
    }
  }
  if (-not $ready) { throw "Chrome CDP not ready (port=$port)" }
  Write-Host "[ok] chrome pid=$($proc.Id) port=$port profile=$Name"
  return [pscustomobject]@{ Proc = $proc; Port = $port; Profile = $profile }
}

function Stop-CDP {
  param($Instance)
  if (-not $Instance) { return }
  for ($wait = 0; $wait -lt 16; $wait++) {
    $alive = Get-ProfileChromeProcesses -Profile $Instance.Profile
    if (-not $alive -or $alive.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
  }
  for ($round = 0; $round -lt 5; $round++) {
    $procs = Get-ProfileChromeProcesses -Profile $Instance.Profile
    if (-not $procs -or $procs.Count -eq 0) { break }
    foreach ($item in $procs) {
      & cmd /c "taskkill /PID $($item.ProcessId) /T /F >nul 2>&1"
    }
    Start-Sleep -Milliseconds 500
  }
  if ($Instance.Proc -and -not $Instance.Proc.HasExited) {
    & cmd /c "taskkill /PID $($Instance.Proc.Id) /T /F >nul 2>&1"
  }
  Start-Sleep -Milliseconds 400
}

function Invoke-Py {
  param([string[]]$PyArgs)
  & $Python @PyArgs 2>&1 | ForEach-Object { Write-Host $_ }
  $code = $LASTEXITCODE
  if ($code -ne 0) { Write-Host "[warn] python exit=$code" }
  return $code
}

$failures = 0

Write-Host "`n== remote: main (fresh user onboarding, desktop) =="
$inst = Start-CDP -Name "main" -Size "1440,900"
if ((Invoke-Py (@("scripts\verify_local_web.py", "--scenario", "main", "--port", "$($inst.Port)") + $common)) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== remote: reopen (same profile, browser restarted) =="
$port = Get-FreePort
$chromeArgs = @(
  "--headless=new",
  "--remote-debugging-port=$port",
  "--user-data-dir=$($inst.Profile)",
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-gpu",
  "--hide-scrollbars",
  "--mute-audio",
  "--window-size=1440,900",
  "about:blank"
)
$proc = Start-Process -FilePath $Chrome -ArgumentList $chromeArgs -PassThru
for ($i = 0; $i -lt 80; $i++) {
  try { Invoke-WebRequest -Uri "http://127.0.0.1:$port/json/version" -TimeoutSec 2 -UseBasicParsing | Out-Null; break } catch { Start-Sleep -Milliseconds 250 }
}
if ((Invoke-Py (@("scripts\verify_local_web.py", "--scenario", "reopen", "--port", "$port") + $common)) -ne 0) { $failures++ }
& cmd /c "taskkill /PID $($proc.Id) /T /F >nul 2>&1"
Start-Sleep -Milliseconds 700

Write-Host "`n== remote: legacy (existing completed account) =="
$inst = Start-CDP -Name "legacy" -Size "1280,800"
if ((Invoke-Py (@("scripts\verify_local_web.py", "--scenario", "legacy", "--port", "$($inst.Port)") + $common)) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== remote: mobile (390 viewport onboarding) =="
$inst = Start-CDP -Name "mobile" -Size "1440,900"
if ((Invoke-Py (@("scripts\verify_local_web.py", "--scenario", "mobile", "--port", "$($inst.Port)") + $common)) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== remote: chat (real production LLM reply) =="
$inst = Start-CDP -Name "chat" -Size "1440,900"
if ((Invoke-Py (@("scripts\verify_local_web.py", "--scenario", "chat", "--port", "$($inst.Port)") + $common)) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== remote: compat (feed / persona / translate / moments / TTS) =="
$inst = Start-CDP -Name "compat" -Size "1440,900"
if ((Invoke-Py (@("scripts\verify_local_web.py", "--scenario", "compat", "--port", "$($inst.Port)") + $common)) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== remote: summary =="
if ((Invoke-Py (@("scripts\verify_local_web.py", "--scenario", "summary") + $common)) -ne 0) { $failures++ }

Write-Host "`n[remote web verify] failed scenarios: $failures (run $stamp)"
exit $failures
