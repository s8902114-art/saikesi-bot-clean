import subprocess
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

WATCH_FILE = "main.py"
cooldown   = 0  # 防止短時間內重複推送

class MainFileHandler(FileSystemEventHandler):
    def on_modified(self, event):
        global cooldown
        if event.src_path.endswith(WATCH_FILE):
            now = time.time()
            if now - cooldown < 10:  # 10 秒冷卻，避免連續觸發
                return
            cooldown = now
            print(f"[auto-push] 偵測到 {WATCH_FILE} 變更，推送中...")
            result = subprocess.run(["bash", "push.sh"], capture_output=True, text=True)
            if result.returncode == 0:
                print("[auto-push] ✅ 推送成功")
            else:
                print(f"[auto-push] ❌ 推送失敗：{result.stderr.strip()}")

if __name__ == "__main__":
    print(f"[auto-push] 監控 {WATCH_FILE} 變更中...")
    handler  = MainFileHandler()
    observer = Observer()
    observer.schedule(handler, path=".", recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
