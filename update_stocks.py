import requests
import json
import os
import pandas as pd
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo

DB_FILE = "historical_prices.json"
OUTPUT_FILE = "all_stocks_data.json"

def main():
    print("開始執行每日選股更新...")

    if not os.path.exists(DB_FILE):
        print(f"❌ 找不到 {DB_FILE}")
        return

    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    today_quotes = get_today_quotes()
    if not today_quotes:
        print("⚠️ 今日 API 無資料")
        return

    tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
    today_str = tw_now.strftime("%Y-%m-%d")

    all_stocks_result = []
    updated_count = 0

    for code, info in db.items():
        if code in today_quotes:
            new_quote = today_quotes[code]

            if info["history"] and info["history"][-1]["date"] == today_str:
                info["history"][-1] = {
                    "date": today_str,
                    "close": new_quote["close"],
                    "volume": new_quote["volume"]
                }
            else:
                info["history"].append({
                    "date": today_str,
                    "close": new_quote["close"],
                    "volume": new_quote["volume"]
                })

            info["history"] = info["history"][-250:]
            updated_count += 1

        history = info["history"]
        if len(history) < 220:
            continue

        # 你原本的指標計算照舊 ...

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)

    output_data = {
        "updatedat": tw_now.strftime("%Y-%m-%d %H:%M:%S"),
        "totalvalidstocks": len(all_stocks_result),
        "stocks": all_stocks_result
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"✅ 更新完成：{updated_count} 檔")
    print(f"✅ 輸出完成：{len(all_stocks_result)} 檔")

if __name__ == "__main__":
    main()
