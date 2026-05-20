import requests
import os

# 1. 定義 Claude 原始程式碼的 API 網址
ARTIFACT_URL = "https://claude.ai/api/public/artifacts/8e8d9794-18f4-4c32-8b60-38cfc7ff4f70"

def sync_code():
    print("⏳ 正在從 Claude Artifacts 下載最新版交易系統程式碼...")
    try:
        response = requests.get(ARTIFACT_URL, timeout=15)
        if response.status_code == 200:
            # 取得純文字程式碼
            latest_code = response.text

            # 安全檢查：確保抓到的不是空檔案或錯誤訊息
            if "#!/usr/bin/env python" in latest_code or "import" in latest_code:
                # 覆寫 Replit 的 main.py
                with open("main.py", "w", encoding="utf-8") as f:
                    f.write(latest_code)
                print("🟢 成功！最新程式碼已寫入 main.py。")
            else:
                print("❌ 警告：下載的內容似乎不是正確的 Python 程式碼，放棄覆寫。")
        else:
            print(f"❌ 錯誤：無法抓取網頁，狀態碼：{response.status_code}")
    except Exception as e:
        print(f"❌ 發生異常：{e}")

if __name__ == "__main__":
    sync_code()

