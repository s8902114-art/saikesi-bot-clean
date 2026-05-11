import subprocess
import time
import os

WATCH_FILE = "main.py"
POLL_INTERVAL = 10   # 每 10 秒檢查一次


def push():
    result = subprocess.run(
        ["bash", "push.sh"],
        capture_output=True,
        text=True,
        env={**os.environ}
    )
    if result.returncode == 0:
        out = result.stdout.strip()
        print(f"[auto-push] ✅ 推送成功" + (f"\n{out}" if out else ""), flush=True)
    else:
        err = (result.stderr or result.stdout).strip()
        print(f"[auto-push] ❌ 失敗：{err}", flush=True)


if __name__ == "__main__":
    last_mtime = os.stat(WATCH_FILE).st_mtime
    print(f"[auto-push] 監控 {WATCH_FILE}（每 {POLL_INTERVAL} 秒輪詢）", flush=True)

    while True:
        time.sleep(POLL_INTERVAL)
        try:
            mtime = os.stat(WATCH_FILE).st_mtime
            if mtime != last_mtime:
                print(f"[auto-push] 偵測到 {WATCH_FILE} 變更，推送中...", flush=True)
                last_mtime = mtime
                time.sleep(2)   # 等檔案寫完
                push()
        except Exception as e:
            print(f"[auto-push] 錯誤：{e}", flush=True)
