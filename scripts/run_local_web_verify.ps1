# Local Web verification orchestrator.
# PowerShell launches Chrome (Python cannot launch it under the DSH job object),
# then Python attaches over CDP and performs real interactions/assertions.
#
# Each scenario gets a fresh profile dir and a free CDP port; Chrome is killed with
# its whole process tree afterwards so a stale instance can never be reused.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_local_web_verify.ps1
param(
  [string]$Base = "http://127.0.0.1:8000",
  [string]$Python = ".venv\Scripts\python.exe",
  [string]$Chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$profilesRoot = Join-Path $root ".tmp\cdp-profiles\$stamp"
New-Item -ItemType Directory -Force -Path $profilesRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root "artifacts\qa-reports") | Out-Null

try {
  Invoke-WebRequest -Uri "$Base/api/v3/health" -TimeoutSec 10 -UseBasicParsing | Out-Null
  Write-Host "[ok] backend health"
} catch {
  throw "backend not ready ($Base/api/v3/health): start uvicorn first"
}

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
  throw "no free CDP port found"
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

function Stop-CDP {
  param($Instance)
  if (-not $Instance) { return }
  # Python 已用 CDP Browser.close 优雅关闭（localStorage 已落盘）；
  # 先等它自然退出，超时再按「命令行里带我们 profile 的 chrome」强杀，绝不误杀用户浏览器。
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

function Clear-StaleVerifyChrome {
  param([string]$Root)
  try {
    $all = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction Stop
  } catch {
    return
  }
  $stale = @($all | Where-Object { ([string]$_.CommandLine) -like "*--user-data-dir=$Root\*" })
  foreach ($item in $stale) {
    & cmd /c "taskkill /PID $($item.ProcessId) /T /F >nul 2>&1"
  }
  if ($stale.Count -gt 0) { Write-Host "[ok] cleaned $($stale.Count) stale headless chrome process(es)" }
  Start-Sleep -Milliseconds 500
}

function Invoke-Py {
  param([string[]]$PyArgs)
  & $Python @PyArgs 2>&1 | ForEach-Object { Write-Host $_ }
  $code = $LASTEXITCODE
  if ($code -ne 0) { Write-Host "[warn] python exit=$code ($($PyArgs -join ' '))" }
  return $code
}

$failures = 0
$script:RunId = $stamp

# 清掉上次验证可能残留的 headless chrome（只匹配本仓库 .tmp\cdp-profiles 下的 profile）
Clear-StaleVerifyChrome -Root (Join-Path $root ".tmp\cdp-profiles")

Write-Host "`n== scenario: main (fresh user, desktop 1440x900) =="
$inst = Start-CDP -Name "main" -Size "1440,900"
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "main", "--port", "$($inst.Port)")) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== scenario: reopen (same profile dir, browser restarted) =="
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
Write-Host "[ok] chrome pid=$($proc.Id) port=$port profile=$($inst.Profile) (reopen)"
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "reopen", "--port", "$port")) -ne 0) { $failures++ }
& cmd /c "taskkill /PID $($proc.Id) /T /F >nul 2>&1"
Start-Sleep -Milliseconds 700

Write-Host "`n== scenario: legacy (existing completed account) =="
$inst = Start-CDP -Name "legacy" -Size "1280,800"
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "legacy", "--port", "$($inst.Port)")) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== scenario: mobile (390 / 320 viewports) =="
$inst = Start-CDP -Name "mobile" -Size "1440,900"
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "mobile", "--port", "$($inst.Port)")) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== scenario: offline save failure + retry =="
$inst = Start-CDP -Name "offline" -Size "1280,800"
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "offline", "--port", "$($inst.Port)")) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== scenario: isolation (UserAlpha) =="
$inst = Start-CDP -Name "alpha" -Size "1024,768"
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "isolation", "--port", "$($inst.Port)", "--nickname", "UserAlpha")) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== scenario: isolation (UserBeta) =="
$inst = Start-CDP -Name "beta" -Size "1024,768"
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "isolation", "--port", "$($inst.Port)", "--nickname", "UserBeta")) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== scenario: chat (real browser + real LLM reply) =="
$inst = Start-CDP -Name "chat" -Size "1440,900"
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "chat", "--port", "$($inst.Port)")) -ne 0) { $failures++ }
Stop-CDP $inst

Write-Host "`n== scenario: isolation-check =="
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "isolation-check")) -ne 0) { $failures++ }

Write-Host "`n== summary =="
if ((Invoke-Py @("scripts\verify_local_web.py", "--scenario", "summary")) -ne 0) { $failures++ }

Write-Host "`n[local web verify] failed scenarios: $failures (run $stamp)"
exit $failures
