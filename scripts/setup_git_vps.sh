#!/bin/bash
# Chạy MỘT LẦN trên VPS (Ubuntu) để gắn Git vào /root/pos
# Thay YOUR_GITHUB_REPO bằng URL repo thật trước khi chạy.
#
#   export GIT_REPO="https://github.com/USER/SME.git"
#   bash /root/pos/scripts/setup_git_vps.sh

set -e

APP_DIR="/root/pos"
GIT_REPO="${GIT_REPO:-}"

if [ -z "$GIT_REPO" ]; then
  echo "Thiếu GIT_REPO. Ví dụ:"
  echo '  export GIT_REPO="https://github.com/USER/SME.git"'
  echo "  bash $0"
  exit 1
fi

echo "=== Backup toàn bộ /root/pos trước khi gắn Git ==="
tar -czf "/root/pos_before_git_$(date +%Y%m%d_%H%M).tar.gz" \
  --exclude='pos/venv' \
  --exclude='pos/__pycache__' \
  -C /root pos

cd "$APP_DIR"

if [ -d .git ]; then
  echo "Đã có .git — chỉ cập nhật remote"
  git remote set-url origin "$GIT_REPO" 2>/dev/null || git remote add origin "$GIT_REPO"
else
  echo "Khởi tạo Git, giữ file local (.env, *.db) ==="
  git init
  git remote add origin "$GIT_REPO"

  # Bảo vệ file nhạy cảm — không commit nếu lỡ add
  cat >> .git/info/exclude << 'EOF'
.env
*.db
venv/
pos_env/
logs/
EOF
fi

git fetch origin

# Thử branch main, fallback master
if git show-ref --verify --quiet refs/remotes/origin/main; then
  BR=main
elif git show-ref --verify --quiet refs/remotes/origin/master; then
  BR=master
else
  echo "Repo remote chưa có commit. Push từ local trước, rồi chạy lại."
  exit 1
fi

echo "=== Pull code từ origin/$BR (giữ .env và *.db local) ==="
git checkout -B "$BR" "origin/$BR" 2>/dev/null || git pull origin "$BR" --allow-unrelated-histories || true

# Đảm bảo file nhạy cảm không bị xóa
[ -f .env ] || echo "Cảnh báo: chưa có .env trên VPS"
ls *.db 2>/dev/null | head -3 || true

chmod +x scripts/deploy_pos.sh 2>/dev/null || true
cp scripts/deploy_pos.sh /root/deploy_pos.sh 2>/dev/null || true
chmod +x /root/deploy_pos.sh 2>/dev/null || true

echo ""
echo "=== Xong. Lần sau chỉ cần: ==="
echo "  Local: git push"
echo "  VPS:   /root/deploy_pos.sh"
