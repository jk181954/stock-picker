import requests
import json
import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

DB_FILE = "historical_prices.json"
OUTPUT_FILE = "all_stocks_data.json"

def parse_tpex_date(date_str):
    """將 TPEX 民國日期 1150402 轉為西元 2026-04-02"""
    date_str = str(date_str).strip()
    if len(date_str) == 7 and date_str.isdigit():
        year = int(date_str[:3]) + 1911   # 115 + 1911 = 2026
        month = date_str[3:5]             # 04
        day = date_str[5:7]               # 02
        return f"{year}-{month}-{day}"
    return None

def get_today_quotes():
    today_data = {}
    tw_today = datetime.now(tz=pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
    actual_date = None

    # ✅ 先抓 TPEX，加入 retry 與 JSON 驗證
    try:
        tpex_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
        headers = {"User-Agent": "Mozilla/5.0"}
        tpex_json = None

        for attempt in range(3):
            res = requests.get(tpex_url, headers=headers, timeout=20)
            if res.status_code == 200 and res.text.strip():
                try:
                    tpex_json = res.json()
                    break
                except Exception:
                    pass
            time.sleep(2 * (attempt + 1))

        if tpex_json is None:
            raise ValueError("TPEX API returned empty or invalid JSON")

        tpex_count = 0
        latest_tpex_date = None
        for item in tpex_json:
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            close = str(item.get("Close", "")).replace(',', '')
            vol = str(item.get("TradingShares", "")).replace(',', '')
            date_str = str(item.get("Date", "")).strip()
            if close and vol and close.replace('.', '', 1).isdigit() and len(code) == 4:
                parsed_date = parse_tpex_date(date_str)
                today_data[code] = {"close": float(close), "volume": float(vol) / 1000}
                if actual_date is None:
                    actual_date = parsed_date
                if parsed_date and (latest_tpex_date is None or parsed_date > latest_tpex_date):
                    latest_tpex_date = parsed_date
                tpex_count += 1
        print(f"TPEX: {tpex_count} 檔（日期: {latest_tpex_date}）")
    except Exception as e:
        print(f"獲取上櫃今日行情失敗: {e}")

    # 再抓 TWSE（無日期欄位）
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        for item in res.json():
            code = str(item.get("Code", "")).strip()
            close = str(item.get("ClosingPrice", "")).replace(',', '')
            vol = str(item.get("TradeVolume", "")).replace(',', '')
            if close and vol and close.replace('.', '', 1).isdigit() and len(code) == 4:
                today_data[code] = {"close": float(close), "volume": float(vol) / 1000}
    except Exception as e:
        print(f"獲取上市今日行情失敗: {e}")

    if actual_date is None:
        actual_date = tw_today
        print(f"⚠️ 無法從 API 取得交易日，使用程式執行日: {actual_date}")

    return today_data, actual_date

def is_ma200_up_10days(ma200_list):
    if len(ma200_list) < 10: return False
    last_10 = ma200_list[-10:]
    for i in range(1, 10):
        if last_10[i] <= last_10[i-1]:
            return False
    return True

def calculate_kd(df, n=9):
    low_min = df['close'].rolling(window=n, min_periods=1).min()
    high_max = df['close'].rolling(window=n, min_periods=1).max()
    rsv = (df['close'] - low_min) / (high_max - low_min + 1e-8) * 100
    K = np.zeros(len(df))
    D = np.zeros(len(df))
    for i in range(len(df)):
        if i == 0:
            K[i] = 50
            D[i] = 50
        else:
            K[i] = K[i-1] * 2/3 + rsv.iloc[i] * 1/3
            D[i] = D[i-1] * 2/3 + K[i] * 1/3
    return pd.Series(K, index=df.index), pd.Series(D, index=df.index)

def main():
    print("=== 開始每日極速增量更新 ===")

    if not os.path.exists(DB_FILE):
        print(f"找不到 {DB_FILE}，請先上傳歷史資料庫！")
        return

    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    today_quotes, actual_data_date = get_today_quotes()
    if not today_quotes:
        print("今日無資料或 API 異常，結束更新。")
        return

    print(f"API 實際交易日期: {actual_data_date}")

    all_stocks_result = []
    updated_count = 0

    for code, info in db.items():
        if code in today_quotes:
            new_quote = today_quotes[code]
            if info["history"] and info["history"][-1]["date"] == actual_data_date:
                info["history"][-1] = {"date": actual_data_date, "close": new_quote["close"], "volume": new_quote["volume"]}
            else:
                info["history"].append({"date": actual_data_date, "close": new_quote["close"], "volume": new_quote["volume"]})
            info["history"] = info["history"][-250:]
            updated_count += 1

        history = info["history"]
        if len(history) < 220:
            continue

        df = pd.DataFrame(history)
        closes = df['close']
        volumes = df['volume']

        ma5 = closes.rolling(window=5).mean()
        ma20 = closes.rolling(window=20).mean()
        ma60 = closes.rolling(window=60).mean()
        ma200 = closes.rolling(window=200).mean()
        low20 = closes.rolling(window=20).min()

        ma200_up = is_ma200_up_10days(ma200.dropna().tolist())

        ma20_today = ma20.iloc[-1]
        ma20_yesterday = ma20.iloc[-2] if len(ma20) > 1 else ma20_today

        vol_ma20 = volumes.rolling(window=20).mean()
        last_10_vols = volumes.iloc[-10:]
        last_10_vol_ma20 = vol_ma20.iloc[-10:]
        has_vol_burst = any(last_10_vols.iloc[i] > (last_10_vol_ma20.iloc[i] * 2) for i in range(len(last_10_vols)))

        pct_change = closes.pct_change() * 100
        has_price_burst = any(pct_change.iloc[-10:] > 5.0)

        high5 = closes.rolling(window=5).max()

        bias20 = abs(closes.iloc[-1] - ma20_today) / ma20_today * 100 if ma20_today > 0 else 0

        vol_ma5 = volumes.rolling(window=5).mean()

        max_vol_10 = volumes.iloc[-10:].max()

        K, D = calculate_kd(df)
        k_value = K.iloc[-1]

        all_stocks_result.append({
            "code": code,
            "name": info["name"],
            "market": info["market"],
            "close": round(float(closes.iloc[-1]), 2),
            "volume": round(float(volumes.iloc[-1]), 2),
            "ma5": round(float(ma5.iloc[-1]), 2),
            "ma20": round(float(ma20_today), 2),
            "ma60": round(float(ma60.iloc[-1]), 2),
            "ma200": round(float(ma200.iloc[-1]), 2),
            "lowestClose20": round(float(low20.iloc[-2] if len(low20) >= 2 else low20.iloc[-1]), 2),
            "ma200_up_10days": ma200_up,
            "ma20_yesterday": round(float(ma20_yesterday), 2),
            "has_vol_burst_10d": bool(has_vol_burst),
            "has_price_burst_10d": bool(has_price_burst),
            "highestClose5": round(float(high5.iloc[-1]), 2),
            "bias20": round(float(bias20), 2),
            "vol_ma5": round(float(vol_ma5.iloc[-1]), 2),
            "max_vol_10d": round(float(max_vol_10), 2),
            "k_value": round(float(k_value), 2)
        })

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)

    tw_tz = pytz.timezone("Asia/Taipei")
    tw_now = datetime.now(tz=tw_tz)

    output_data = {
        "updated_at": tw_now.strftime("%Y-%m-%d %H:%M:%S CST"),
        "data_date": actual_data_date,
        "total_valid_stocks": len(all_stocks_result),
        "stocks": all_stocks_result
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"=== 更新完成 ===")
    print(f"今天共更新 {updated_count} 檔股票價格")
    print(f"實際資料日期: {actual_data_date}")
    print(f"成功儲存 {len(all_stocks_result)} 檔符合天數的股票指標！")

if __name__ == "__main__":
    main()
