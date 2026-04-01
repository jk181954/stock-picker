import requests
import json
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz

DB_FILE = "historical_prices.json"
OUTPUT_FILE = "all_stocks_data.json"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")


def roc_to_ad_date(roc_date_str):
    s = str(roc_date_str).strip()
    if len(s) == 7 and s.isdigit():
        year = int(s[:3]) + 1911
        month = s[3:5]
        day = s[5:7]
        return f"{year}-{month}-{day}"
    return None


def is_valid_number_string(v):
    s = str(v).strip().replace(',', '')
    if s in ['', '--', '---', 'X', '除權息', 'null', 'None']:
        return False
    try:
        float(s)
        return True
    except:
        return False


def get_twse_quotes():
    today_data = {}
    api_data_date = None
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=20)
        res.raise_for_status()
        for item in res.json():
            code = str(item.get("Code", "")).strip()
            close = str(item.get("ClosingPrice", "")).strip().replace(',', '')
            vol = str(item.get("TradeVolume", "")).strip().replace(',', '')
            roc_date = str(item.get("Date", "")).strip()
            ad_date = roc_to_ad_date(roc_date)

            if ad_date and api_data_date is None:
                api_data_date = ad_date

            if len(code) == 4 and ad_date and is_valid_number_string(close) and is_valid_number_string(vol):
                today_data[code] = {
                    "close": float(close),
                    "volume": float(vol) / 1000,
                    "date": ad_date,
                    "source": "TWSE"
                }
    except Exception as e:
        print(f"獲取上市今日行情失敗: {e}")
    return today_data, api_data_date


def get_tpex_quotes():
    today_data = {}
    try:
        res = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes", timeout=20)
        res.raise_for_status()
        tpex_data = res.json()
        taipei_today = datetime.now(tz=pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")

        for item in tpex_data:
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            close = str(item.get("Close", "")).strip().replace(',', '')
            vol = str(item.get("TradingShares", "")).strip().replace(',', '')

            if len(code) == 4 and is_valid_number_string(close) and is_valid_number_string(vol):
                today_data[code] = {
                    "close": float(close),
                    "volume": float(vol) / 1000,
                    "date": taipei_today,
                    "source": "TPEX"
                }
    except Exception as e:
        print(f"獲取上櫃今日行情失敗: {e}")
    return today_data


def get_stock_from_finmind(stock_id, start_date, end_date):
    if not FINMIND_TOKEN:
        return None
    try:
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": stock_id,
            "start_date": start_date,
            "end_date": end_date,
            "token": FINMIND_TOKEN
        }
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("data", [])
        if not rows:
            return None

        df = pd.DataFrame(rows)
        if df.empty:
            return None

        df = df.sort_values("date")
        last_row = df.iloc[-1]
        return {
            "date": str(last_row["date"]),
            "close": float(last_row["close"]),
            "volume": float(last_row["Trading_Volume"]) / 1000,
            "source": "FinMind"
        }
    except Exception as e:
        print(f"FinMind 補資料失敗 {stock_id}: {e}")
        return None


def infer_actual_data_date(db):
    latest_dates = []
    for code, info in db.items():
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
        if pd.isna(last_10[i]) or pd.isna(last_10[i - 1]) or last_10[i] <= last_10[i - 1]:
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


def upsert_history(info, quote):
    history = info.setdefault("history", [])
    quote_date = quote["date"]
    payload = {
        "date": quote_date,
        "close": quote["close"],
        "volume": quote["volume"]
    }

    if history and history[-1].get("date") == quote_date:
        history[-1] = payload
    else:
        history.append(payload)

    info["history"] = history[-250:]


def main():
    print("=== 開始每日極速增量更新 ===")

    if not os.path.exists(DB_FILE):
        print(f"找不到 {DB_FILE}，請先上傳歷史資料庫！")
        return

    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    tw_tz = pytz.timezone("Asia/Taipei")
    taipei_now = datetime.now(tz=tw_tz)
    taipei_today = taipei_now.strftime("%Y-%m-%d")
    fallback_start = (taipei_now - timedelta(days=7)).strftime("%Y-%m-%d")

    twse_quotes, twse_api_date = get_twse_quotes()
    tpex_quotes = get_tpex_quotes()

    today_quotes = {}
    today_quotes.update(twse_quotes)
    today_quotes.update(tpex_quotes)

    if not today_quotes and not FINMIND_TOKEN:
        print("今日無資料且未設定 FINMIND_TOKEN，結束更新。")
        return

    print(f"台北今天日期: {taipei_today}")
    print(f"TWSE API 資料日期: {twse_api_date}")

    updated_count = 0
    fallback_count = 0
    updated_dates = set()
    source_stats = {"TWSE": 0, "TPEX": 0, "FinMind": 0}

    for code, info in db.items():
        quote = today_quotes.get(code)
        use_finmind = False

        if quote is None:
            use_finmind = True
        elif quote.get("date") != taipei_today:
            use_finmind = True

        if use_finmind:
            finmind_quote = get_stock_from_finmind(code, fallback_start, taipei_today)
            if finmind_quote and finmind_quote.get("date") == taipei_today:
                quote = finmind_quote
                fallback_count += 1

        if quote:
            upsert_history(info, quote)
            updated_count += 1
            updated_dates.add(quote["date"])
            src = quote.get("source", "unknown")
            if src in source_stats:
                source_stats[src] += 1

    all_stocks_result = []

    for code, info in db.items():
        history = info.get("history", [])
        if len(history) < 220:
            continue

        df = pd.DataFrame(history)
        if df.empty or 'close' not in df.columns or 'volume' not in df.columns:
            continue

        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        df = df.dropna(subset=['close', 'volume']).reset_index(drop=True)

        if len(df) < 220:
            continue

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
            pd.notna(last_10_vols.iloc[i]) and pd.notna(last_10_vol_ma20.iloc[i]) and last_10_vols.iloc[i] > (last_10_vol_ma20.iloc[i] * 2)
            for i in range(len(last_10_vols))
        )

        pct_change = closes.pct_change() * 100
        has_price_burst = any(pd.notna(x) and x > 5.0 for x in pct_change.iloc[-10:])
        high5 = closes.rolling(window=5).max()
        bias20 = abs(closes.iloc[-1] - ma20_today) / ma20_today * 100 if pd.notna(ma20_today) and ma20_today > 0 else 0
        vol_ma5 = volumes.rolling(window=5).mean()
        max_vol_10 = volumes.iloc[-10:].max()
        K, D = calculate_kd(df)
        k_value = K.iloc[-1]
        last_date = str(df.iloc[-1].get("date")) if "date" in df.columns else history[-1].get("date")

        all_stocks_result.append({
            "code": code,
            "name": info.get("name", ""),
            "market": info.get("market", ""),
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

    actual_data_date = max(updated_dates) if updated_dates else infer_actual_data_date(db)

    output_data = {
        "updated_at": taipei_now.strftime("%Y-%m-%d %H:%M:%S CST"),
        "data_date": actual_data_date,
        "today_updated_count": updated_count,
        "fallback_updated_count": fallback_count,
        "source_stats": source_stats,
        "total_valid_stocks": len(all_stocks_result),
        "stocks": all_stocks_result
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("=== 更新完成 ===")
    print(f"更新股票數: {updated_count}")
    print(f"FinMind 備援補到: {fallback_count}")
    print(f"來源統計: {source_stats}")
    print(f"實際資料日期: {actual_data_date}")
    print(f"成功儲存 {len(all_stocks_result)} 檔符合天數的股票指標！")


if __name__ == "__main__":
    main()
