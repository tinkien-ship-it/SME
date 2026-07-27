# Chạy trên Windows (PowerShell) SAU KHI cài Git for Windows
# https://git-scm.com/download/win
#
#   cd C:\SME
#   powershell -ExecutionPolicy Bypass -File scripts\setup_git_windows.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Chua cai Git. Tai: https://git-scm.com/download/win" -ForegroundColor Red
    Write-Host "Sau khi cai, mo lai PowerShell va chay lai script nay."
    exit 1
}

$RepoUrl = Read-Host "Nhap URL GitHub repo (vd: https://github.com/USER/SME.git)"

if (-not (Test-Path .git)) {
    git init
    git branch -M main
}

git remote remove origin 2>$null
git remote add origin $RepoUrl

git add .
git status

$msg = Read-Host "Commit message (Enter = 'Cap nhat POS SME')"
if ([string]::IsNullOrWhiteSpace($msg)) { $msg = "Cap nhat POS SME" }

git commit -m $msg
git push -u origin main

Write-Host ""
Write-Host "Da push len GitHub." -ForegroundColor Green
Write-Host "Tren VPS chay:"
Write-Host '  export GIT_REPO="' + $RepoUrl + '"'
Write-Host "  bash /root/pos/scripts/setup_git_vps.sh"
Write-Host "Lan sau: git push  (local)  +  /root/deploy_pos.sh  (VPS)"
