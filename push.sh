#!/bin/bash
REPO="https://${GITHUB_TOKEN}@github.com/s8902114-art/saikesi-bot-.git"

# 等待 git 鎖定解除（Replit 會自動跑 git checkpoint，可能衝突）
for i in 1 2 3 4 5; do
    if [ ! -f .git/index.lock ] && [ ! -f .git/config.lock ]; then
        break
    fi
    echo "[push] 等待 git 鎖定解除（第 $i 次）..."
    sleep 3
done

git add main.py backtest.py auto_push.py push.sh requirements.txt Procfile runtime.txt

# 如果沒有變更就不 commit
if git diff --cached --quiet; then
    echo "[push] 無變更，略過"
    exit 0
fi

git commit -m "更新：$(date '+%Y-%m-%d %H:%M')"
git push "$REPO" main && echo "[push] 推送完成" || echo "[push] 推送失敗"
