#!/bin/bash
# Chay MOT LAN tren VPS (Ubuntu) de gan Git vao /root/pos
#   export GIT_REPO="https://github.com/tinkien-ship-it/SME.git"
#   bash /root/pos/scripts/setup_git_vps.sh

set -e

APP_DIR="/root/pos"
GIT_REPO="${GIT_REPO:-}"

if [ -z "$GIT_REPO" ]; then
  echo "Thieu GIT_REPO. Vi du:"
  echo '  export GIT_REPO="https://github.com/tinkien-ship-it/SME.git"'
  echo "  bash $0"
  exit 1
fi

echo "=== Backup toan bo /root/pos truoc khi gan Git ==="
tar -czf "/root/pos_before_git_$(date +%Y%m%d_%H%M).tar.gz" \
  --exclude='pos/venv' \
  --exclude='pos/__pycache__' \
  -C /root pos

cd "$APP_DIR"

if [ -d .git ]; then
  echo "Da co .git — chi cap nhat remote"
  git remote set-url origin "$GIT_REPO" 2>/dev/null || git remote add origin "$GIT_REPO"
else
  echo "Khoi tao Git, giu file local (.env, *.db)"
  git init
  git remote add origin "$GIT_REPO"

  cat >> .git/info/exclude << 'EOF'
.env
*.db
venv/
pos_env/
logs/
EOF
fi

git fetch origin

if git show-ref --verify --quiet refs/remotes/origin/main; then
  BR=main
elif git show-ref --verify --quiet refs/remotes/origin/master; then
  BR=master
else
  echo "Repo remote chua co commit. Push tu local truoc, roi chay lai."
  exit 1
fi

echo "=== Pull code tu origin/$BR (giu .env va *.db local) ==="
git checkout -B "$BR" "origin/$BR" 2>/dev/null || git pull origin "$BR" --allow-unrelated-histories || true

[ -f .env ] || echo "Canh bao: chua co .env tren VPS"
ls *.db 2>/dev/null | head -3 || true

if [ -f scripts/deploy_pos.sh ]; then
  sed -i 's/\r$//' scripts/deploy_pos.sh 2>/dev/null || true
  chmod +x scripts/deploy_pos.sh
  cp scripts/deploy_pos.sh /root/deploy_pos.sh
  chmod +x /root/deploy_pos.sh
fi

echo ""
echo "=== Xong. Lan sau chi can: ==="
echo "  Local: git push"
echo "  VPS:   /root/deploy_pos.sh"
