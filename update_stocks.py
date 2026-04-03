import requests
import json
import os
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

DB_FILE = "historical_prices.json"
OUTPUT_FILE = "all_stocks_data.json"
TW_TZ = pytz.timezone("Asia/Taipei")


def get_today_quotes():
    today_data = {}
    today_str = datetime.now(tz=TW_TZ).strftime("%Y-%m-%d")

    try:
        res = requests.get(
            "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
            timeout=15
        )
        res.raise_for_status()
        for item in res.json():
            code = str(item.get("Code", "")).strip()
            close = str(item.get("ClosingPrice", "")).replace(",", "").strip()
            vol = str(item.get("TradeVolume", "")).replace(",", "").strip()

            if close and vol and close.replace(".", "", 1).isdigit() and len(code) == 4:
                today_data[code] = {
                    "close": float(close),
                    "volume": float(vol) / 1000
                }
    except Exception as e:
        print(f"獲取上市今日行情失敗: {e}")

    try:
        res = requests.get(
            "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
            timeout=15
        )
        res.raise_for_status()
        for item in res.json():
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            close = str(item.get("Close", "")).replace(",", "").strip()
            vol = str(item.get("TradingShares", "")).replace(",", "").strip()

            if close and vol and close.replace(".", "", 1).isdigit() and len(code) == 4:
                today_data[code] = {
                    "close": float(close),
                    "volume": float(vol) / 1000
                }
    except Exception as e:
        print(f"獲取上櫃今日行情失敗: {e}")

    return today_data, today_str


def infer_actual_data_date(db):
    latest_dates = []
    for _, info in db.items():
        history = info.get("history", [])
        if history:
            last_date = history[-1].get("date")
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
    low_min = df["close"].rolling(window=n, min_periods=1).min()
    high_max = df["close"].rolling(window=n, min_periods=1).max()

    rsv = (df["close"] - low_min) / (high_max - low_min + 1e-8) * 100

    k_values = np.zeros(len(df))
    d_values = np.zeros(len(df))

    for i in range(len(df)):
        if i == 0:
            k_values[i] = 50
            d_values[i] = 50
        else:
            k_values[i] = k_values[i - 1] * 2 / 3 + rsv.iloc[i] * 1 / 3
            d_values[i] = d_values[i - 1] * 2 / 3 + k_values[i] * 1 / 3

    return pd.Series(k_values, index=df.index), pd.Series(d_values, index=df.index)


def is_valid_trading_day(today_quotes, db, min_ratio=0.3, min_count=300):
    valid_db_codes = {code for code, info in db.items() if len(info.get("history", [])) > 0}
    matched_codes = valid_db_codes & set(today_quotes.keys())

    enough_by_ratio = len(matched_codes) >= int(len(valid_db_codes) * min_ratio) if valid_db_codes else False
    enough_by_count = len(matched_codes) >= min_count

    return enough_by_ratio or enough_by_count, len(matched_codes), len(valid_db_codes)


def build_stock_result(code, info):
    history = info.get("history", [])
    if len(history) < 220:
        return None

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
        if pd.notna(last_10_vol_ma20.iloc[i])
    )

    pct_change = closes.pct_change() * 100
    has_price_burst = bool((pct_change.iloc[-10:] > 5.0).any())

    high5 = closes.rolling(window=5).max()
    bias20 = abs(closes.iloc[-1] - ma20_today) / ma20_today * 100 if ma20_today > 0 else 0

    vol_ma5 = volumes.rolling(window=5).mean()
    max_vol_10 = volumes.iloc[-10:].max()

    k_series, d_series = calculate_kd(df)
    k_value = k_series.iloc[-1]

    return {
        "code": code,
        "name": info.get("name", ""),
        "market": info.get("market", ""),
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
    }


def main():
    print("=== 開始每日極速增量更新 ===")

    if not os.path.exists(DB_FILE):
        print(f"找不到 {DB_FILE}，請先上傳歷史資料庫！")
        return

    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    before_data_date = infer_actual_data_date(db)
    print(f"更新前最新交易日: {before_data_date}")

    today_quotes, today_str = get_today_quotes()
    print(f"程式執行日期: {today_str}")
    print(f"今日抓到報價筆數: {len(today_quotes)}")

    if not today_quotes:
        print("今日無資料或 API 異常，沿用舊資料。")
    else:
        valid_trading_day, matched_count, total_db_codes = is_valid_trading_day(today_quotes, db)
        print(f"對應資料庫股票數: {matched_count}/{total_db_codes}")
        print(f"是否判定為有效交易日: {valid_trading_day}")

        updated_count = 0

        if valid_trading_day:
            for code, info in db.items():
                if code in today_quotes:
                    new_quote = today_quotes[code]
                    history = info.get("history", [])

                    if history and history[-1]["date"] == today_str:
                        history[-1] = {
                            "date": today_str,
                            "close": new_quote["close"],
                            "volume": new_quote["volume"]
                        }
                    else:
                        history.append({
                            "date": today_str,
                            "close": new_quote["close"],
                            "volume": new_quote["volume"]
                        })

                    info["history"] = history[-250:]
                    updated_count += 1

            print(f"今天共更新 {updated_count} 檔股票價格")
        else:
            print("今日判定非有效交易日，不寫入今天日期，沿用上一個交易日資料。")

    actual_data_date = infer_actual_data_date(db)
    print(f"更新後最新交易日: {actual_data_date}")

    all_stocks_result = []
    for code, info in db.items():
        stock_result = build_stock_result(code, info)
        if stock_result:
            all_stocks_result.append(stock_result)

    all_stocks_result = sorted(all_stocks_result, key=lambda x: int(x["code"]))

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)

    tw_now = datetime.now(tz=TW_TZ)
    output_data = {
        "updated_at": tw_now.strftime("%Y-%m-%d %H:%M:%S CST"),
        "data_date": actual_data_date,
        "total_valid_stocks": len(all_stocks_result),
        "stocks": all_stocks_result
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("=== 更新完成 ===")
    print(f"實際資料日期: {actual_data_date}")
    print(f"成功儲存 {len(all_stocks_result)} 檔符合天數的股票指標！")


if __name__ == "__main__":
    main()
