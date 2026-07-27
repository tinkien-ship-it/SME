#!/bin/bash
# Chạy trên VPS Ubuntu sau khi git push từ local:
#   /root/deploy_pos.sh
# hoặc: bash /root/pos/scripts/deploy_pos.sh

set -e

APP_DIR="/root/pos"
BRANCH="${DEPLOY_BRANCH:-main}"
VENV="$APP_DIR/venv/bin/activate"

cd "$APP_DIR"

echo "=== [1/4] Backup nhanh (không gồm venv) ==="
tar -czf "/root/pos_backup_$(date +%Y%m%d_%H%M).tar.gz" \
  --exclude='pos/venv' \
  --exclude='pos/pos_env' \
  --exclude='pos/__pycache__' \
  --exclude='pos/**/__pycache__' \
  -C /root pos

echo "=== [2/4] Git pull ($BRANCH) ==="
if [ ! -d .git ]; then
  echo "Lỗi: /root/pos chưa có Git. Chạy scripts/setup_git_vps.sh trước."
  exit 1
fi

git fetch origin
git pull origin "$BRANCH" || git pull origin master

echo "=== [3/4] Cài dependency (bỏ pywin32 trên Linux) ==="
# shellcheck disable=SC1090
source "$VENV"
grep -v pywin32 requirements.txt | pip install -r /dev/stdin -q

echo "=== [4/4] Restart pos.service ==="
systemctl restart pos
sleep 2
systemctl status pos --no-pager -l | head -20

HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/login || echo "000")
echo "HTTP /login => $HTTP"
echo "=== Deploy xong ==="
