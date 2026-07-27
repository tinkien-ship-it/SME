# Copy code len VPS khi CHUA co Git (hoac can hotfix nhanh)
#   powershell -ExecutionPolicy Bypass -File scripts\sync_to_vps.ps1
#
# Sua $VpsHost neu can

$ErrorActionPreference = "Stop"
$VpsHost = "root@14.225.224.29"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$Paths = @(
    "app.py", "auth.py", "db_utils.py", "helpers.py", "tenant_middleware.py",
    "tenant.py", "scheduler.py", "requirements.txt",
    "routes", "Services", "templates", "db", "static", "config", "scripts"
)

Write-Host "Dong bo code len $VpsHost:/root/pos/ ..."
Write-Host "KHONG copy .env, *.db, venv" -ForegroundColor Yellow

foreach ($p in $Paths) {
    $full = Join-Path $Root $p
    if (-not (Test-Path $full)) { continue }
    if (Test-Path $full -PathType Container) {
        scp -r $full "${VpsHost}:/root/pos/"
    } else {
        scp $full "${VpsHost}:/root/pos/"
    }
}

Write-Host ""
Write-Host "Tren VPS chay:" -ForegroundColor Green
Write-Host @"
  cd /root/pos
  source venv/bin/activate
  grep -v pywin32 requirements.txt | pip install -r /dev/stdin -q
  systemctl restart pos
  journalctl -u pos -n 20 --no-pager
"@
