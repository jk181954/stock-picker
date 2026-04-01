import requests
import json
import os
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

DB_FILE = "historical_prices.json"
OUTPUT_FILE = "all_stocks_data.json"

def roc_to_ad_date(roc_date_str):
    s = str(roc_date_str).strip()
    if len(s) == 7 and s.isdigit():
        year = int(s[:3]) + 1911
        month = s[3:5]
        day = s[5:7]
        return f"{year}-{month}-{day}"
    return None

def get_today_quotes():
    today_data = {}
    api_data_date = None

    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        for item in res.json():
            code = str(item.get("Code", "")).strip()
            close = str(item.get("ClosingPrice", "")).replace(',', '')
            vol = str(item.get("TradeVolume", "")).replace(',', '')
            roc_date = str(item.get("Date", "")).strip()
            ad_date = roc_to_ad_date(roc_date)

            if ad_date and api_data_date is None:
                api_data_date = ad_date

            if close and vol and close.replace('.', '', 1).isdigit() and len(code) == 4 and ad_date:
                today_data[code] = {
                    "close": float(close),
                    "volume": float(vol) / 1000,
                    "date": ad_date
                }
    except Exception as e:
        print(f"獲取上市今日行情失敗: {e}")

    try:
        res = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes", timeout=15)
        tpex_data = res.json()
        taipei_today = datetime.now(tz=pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")

        for item in tpex_data:
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            close = str(item.get("Close", "")).replace(',', '')
            vol = str(item.get("TradingShares", "")).replace(',', '')

            if close and vol and close.replace('.', '', 1).isdigit() and len(code) == 4:
                today_data[code] = {
                    "close": float(close),
                    "volume": float(vol) / 1000,
                    "date": taipei_today
                }
    except Exception as e:
        print(f"獲取上櫃今日行情失敗: {e}")

    return today_data, api_data_date

def infer_actual_data_date(db):
    latest_dates = []
    for code, info in db.items():
        if "history" in info and info["history"]:
            last_date = info["history"][-1].get("date")
            if last_date:
                latest_dates.append(last_date)
    return max(latest_dates) if latest_dates else None

def is_ma200_up_10days(ma200_list):
    if len(ma200_list) < 10:
        return False
    last_10 = ma200_list[-10:]
    for i in range(1, 10):
        if last_10[i] <= last_10[i - 1]:
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
            K[i] = K[i - 1] * 2 / 3 + rsv.iloc[i] * 1 / 3
            D[i] = D[i - 1] * 2 / 3 + K[i] * 1 / 3

    return pd.Series(K, index=df.index), pd.Series(D, index=df.index)

def main():
    print("=== 開始每日極速增量更新 ===")

    if not os.path.exists(DB_FILE):
        print(f"找不到 {DB_FILE}，請先上傳歷史資料庫！")
        return

    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    today_quotes, api_data_date = get_today_quotes()
    if not today_quotes:
        print("今日無資料或 API 異常，結束更新。")
        return

    taipei_today = datetime.now(tz=pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
    print(f"台北今天日期: {taipei_today}")
    print(f"TWSE API 資料日期: {api_data_date}")

    all_stocks_result = []
    updated_count = 0
    updated_dates = set()

    for code, info in db.items():
        if code in today_quotes:
            new_quote = today_quotes[code]
            quote_date = new_quote["date"]

            if info["history"] and info["history"][-1]["date"] == quote_date:
                info["history"][-1] = {
                    "date": quote_date,
                    "close": new_quote["close"],
                    "volume": new_quote["volume"]
                }
            else:
                info["history"].append({
                    "date": quote_date,
                    "close": new_quote["close"],
                    "volume": new_quote["volume"]
                })

            info["history"] = info["history"][-250:]
            updated_count += 1
            updated_dates.add(quote_date)

        history = info["history"]
        if len(history) < 220:
            continue

        df = pd.DataFrame(history)
        closes = df["close"]
        volumes = df["volume"]

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
        has_vol_burst = any(
            last_10_vols.iloc[i] > (last_10_vol_ma20.iloc[i] * 2)
            for i in range(len(last_10_vols))
        )

        pct_change = closes.pct_change() * 100
        has_price_burst = any(pct_change.iloc[-10:] > 5.0)

        high5 = closes.rolling(window=5).max()
        bias20 = abs(closes.iloc[-1] - ma20_today) / ma20_today * 100 if ma20_today > 0 else 0
        vol_ma5 = volumes.rolling(window=5).mean()
        max_vol_10 = volumes.iloc[-10:].max()

        K, D = calculate_kd(df)
        k_value = K.iloc[-1]

        last_date = history[-1]["date"] if history else None

        all_stocks_result.append({
            "code": code,
            "name": info["name"],
            "market": info["market"],
            "date": last_date,
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

    actual_data_date = max(updated_dates) if updated_dates else infer_actual_data_date(db)

    output_data = {
        "updated_at": tw_now.strftime("%Y-%m-%d %H:%M:%S CST"),
        "data_date": actual_data_date,
        "today_updated_count": updated_count,
        "total_valid_stocks": len(all_stocks_result),
        "stocks": all_stocks_result
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("=== 更新完成 ===")
    print(f"更新股票數: {updated_count}")
    print(f"實際資料日期: {actual_data_date}")
    print(f"成功儲存 {len(all_stocks_result)} 檔符合天數的股票指標！")

if __name__ == "__main__":
    main()
