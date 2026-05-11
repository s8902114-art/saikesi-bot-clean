#!/bin/bash
REPO="https://${GITHUB_TOKEN}@github.com/s8902114-art/saikesi-bot-clean.git"

# 設定 git 身份
git config user.email "bot@saikesi.local"
git config user.name "Saikesi Bot"

# 等待 git 鎖定解除
for i in 1 2 3 4 5; do
    if [ ! -f .git/index.lock ] && [ ! -f .git/config.lock ]; then
        break
    fi
    echo "[push] 等待 git 鎖定（第 $i 次）..."
    sleep 3
done

# Stage 並 commit 新變更
git add main.py backtest.py auto_push.py push.sh requirements.txt Procfile runtime.txt 2>&1

if git diff --cached --quiet; then
    echo "[push] 無新變更，直接推送現有 commit..."
else
    git commit -m "更新：$(date '+%Y-%m-%d %H:%M')" 2>&1
fi

# 強制推送到新乾淨 repo
PUSH_OUT=$(git push "$REPO" main --force 2>&1)
if [ $? -eq 0 ]; then
    echo "[push] ✅ 推送完成 → saikesi-bot-clean"
else
    echo "[push] ❌ 失敗：$PUSH_OUT"
fi
