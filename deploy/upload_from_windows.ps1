# 从 Windows 本机把当前构建部署到云服务器（需 SSH 凭据）
#
# 用法：
#   .\deploy\upload_from_windows.ps1
#   .\deploy\upload_from_windows.ps1 -Host 47.104.24.236 -User root -Key "C:\Users\Administrator\.ssh\id_ed25519_bilimusic"
#
# 前置：
#   1) 本地已构建前端：cd frontend; npm run build:backend
#   2) 已打包：见 dist\speakeasy-v3-deploy-YYYYMMDD-HHMM.tar.gz
#   3) 本机公钥已加入服务器 /root/.ssh/authorized_keys

param(
  [string]$Host = "47.104.24.236",
  [string]$User = "root",
  [string]$Key = "$env:USERPROFILE\.ssh\id_ed25519_bilimusic",
  [string]$Tar = "",
  [switch]$SkipDeploy
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not $Tar) {
  $latest = Get-ChildItem dist\*.tar.gz | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $latest) { throw "找不到 dist\*.tar.gz，请先打包" }
  $Tar = $latest.FullName
}
if (-not (Test-Path $Tar)) { throw "找不到部署包: $Tar" }
Write-Host "部署包: $Tar ($([math]::Round((Get-Item $Tar).Length / 1MB, 1)) MB)"

$remoteTar = "/root/speakeasy-deploy.tar.gz"
Write-Host "上传到 ${User}@${Host}:$remoteTar ..."
& scp -i $Key -o StrictHostKeyChecking=accept-new $Tar "${User}@${Host}:$remoteTar"
if ($LASTEXITCODE -ne 0) { throw "scp 失败（检查 SSH 凭据/网络）" }

if ($SkipDeploy) { Write-Host "已上传，跳过部署"; exit 0 }

$remote = @'
set -e
mkdir -p /root/speakeasy
tar -xzf /root/speakeasy-deploy.tar.gz -C /root/speakeasy
cd /root/speakeasy
echo "==> 当前目录: $(pwd)"
echo "==> 数据目录保留: $(du -sh data 2>/dev/null | cut -f1 || echo '无 data/')"
bash deploy/deploy_v3.sh
'@

Write-Host "远程解压并执行 deploy/deploy_v3.sh ..."
& ssh -i $Key -o StrictHostKeyChecking=accept-new "${User}@${Host}" $remote
if ($LASTEXITCODE -ne 0) { throw "远程部署失败（见上方输出）" }

Write-Host "完成。验证：http://${Host}:8000/api/v3/health"
