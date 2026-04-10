import requests
import json
import os
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz

DB_FILE = "historical_prices.json"
OUTPUT_FILE = "all_stocks_data.json"
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"


def parse_tpex_date(date_str):
    date_str = str(date_str).strip()
    if len(date_str) == 7 and date_str.isdigit():
        year = int(date_str[:3]) + 1911
        return f"{year}-{date_str[3:5]}-{date_str[5:7]}"
    return None


def get_last_trading_date_from_twse():
    """從 TWSE 月曆 API 取得最近真實交易日（防呆用）"""
    tw_now = datetime.now(tz=pytz.timezone("Asia/Taipei"))
    for month_offset in range(2):
        check_dt = tw_now - timedelta(days=30 * month_offset)
        ym = check_dt.strftime("%Y%m")
        try:
            url = f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={ym}01&response=json"
            res = requests.get(url, timeout=10)
            data = res.json()
            for row in reversed(data.get("data", [])):
                parts = str(row[0]).strip().split("/")
                if len(parts) == 3:
                    year = int(parts[0]) + 1911
                    candidate = f"{year}-{parts[1]}-{parts[2]}"
                    if candidate <= tw_now.strftime("%Y-%m-%d"):
                        return candidate
        except Exception as e:
            print(f"TWSE 月曆查詢失敗: {e}")
    return None


def get_today_quotes():
    """從 TPEX / TWSE 即時 API 取得今日報價與實際交易日"""
    today_data = {}
    actual_date = None

    try:
        res = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes", timeout=15)
        for item in res.json():
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            close = str(item.get("Close", "")).replace(",", "")
            vol = str(item.get("TradingShares", "")).replace(",", "")
            date_str = str(item.get("Date", "")).strip()
            if close and vol and close.replace(".", "", 1).isdigit() and len(code) == 4:
                today_data[code] = {"close": float(close), "volume": float(vol) / 1000}
                if actual_date is None:
                    actual_date = parse_tpex_date(date_str)
    except Exception as e:
        print(f"TPEX 行情失敗: {e}")

    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        for item in res.json():
            code = str(item.get("Code", "")).strip()
            close = str(item.get("ClosingPrice", "")).replace(",", "")
            vol = str(item.get("TradeVolume", "")).replace(",", "")
            date_str = str(item.get("Date", "")).strip()
            if close and vol and close.replace(".", "", 1).isdigit() and len(code) == 4:
                parsed = parse_tpex_date(date_str)
                # 只有當 TWSE 日期 >= 已知日期才採用（避免拿到舊資料）
                if actual_date is None or (parsed and parsed >= actual_date):
                    today_data[code] = {"close": float(close), "volume": float(vol) / 1000}
                    if parsed and (actual_date is None or parsed > actual_date):
                        actual_date = parsed
    except Exception as e:
        print(f"TWSE 行情失敗: {e}")

    if actual_date is None:
        print("⚠️ 無法從即時 API 取得交易日，查詢 TWSE 月曆...")
        actual_date = get_last_trading_date_from_twse()
        if actual_date:
            print(f"✅ 月曆查詢成功: {actual_date}")
        else:
            print("⚠️ 月曆查詢失敗，今日可能為非交易日。")
            return {}, None

    return today_data, actual_date


def is_duplicate(history, new_quote):
    """今日報價是否與最後一筆完全相同（API 尚未更新）"""
    if not history:
        return False
    prev = history[-1]
    return (round(prev["close"], 2) == round(new_quote["close"], 2) and
            round(prev["volume"], 2) == round(new_quote["volume"], 2))


def clean_duplicate_entries(db, actual_data_date):
    """清除當日重複寫入的錯誤資料"""
    cleaned = 0
    for info in db.values():
        h = info.get("history", [])
        if (len(h) >= 2 and h[-1]["date"] == actual_data_date and
                round(h[-2]["close"], 2) == round(h[-1]["close"], 2) and
                round(h[-2]["volume"], 2) == round(h[-1]["volume"], 2)):
            info["history"] = h[:-1]
            cleaned += 1
    return cleaned


# ── 補齊第二層：yfinance ────────────────────────────────────────────────────

def fetch_yfinance(code, market, start_date, end_date):
    try:
        import yfinance as yf
        suffix = ".TWO" if market == "TPEX" else ".TW"
        df = yf.download(code + suffix, start=start_date, end=end_date,
                         auto_adjust=True, progress=False)
        if df.empty:
            return []
        rows = []
        for idx, row in df.iterrows():
            c, v = row["Close"], row["Volume"]
            if pd.isna(c) or pd.isna(v):
                continue
            rows.append({"date": idx.strftime("%Y-%m-%d"),
                         "close": round(float(c), 2),
                         "volume": round(float(v) / 1000, 2)})
        return rows
    except Exception:
        return []


def backfill_yfinance(db, actual_data_date):
    stale = [(code, info.get("market", "TWSE"), info["history"][-1]["date"])
             for code, info in db.items()
             if info.get("history") and info["history"][-1]["date"] < actual_data_date]

    if not stale:
        print("✅ yfinance：無需補齊。")
        return db, []

    print(f"⚙️  yfinance 補齊：{len(stale)} 檔...")
    still_missing = []
    filled = 0

    for i, (code, market, last_date) in enumerate(stale):
        start_dt = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        end_dt = (datetime.strptime(actual_data_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        rows = fetch_yfinance(code, market, start_dt, end_dt)

        if rows:
            existing = {r["date"] for r in db[code]["history"]}
            for row in rows:
                if row["date"] not in existing:
                    db[code]["history"].append(row)
            db[code]["history"] = sorted(db[code]["history"], key=lambda x: x["date"])[-250:]
            filled += 1
        else:
            still_missing.append(code)

        if (i + 1) % 100 == 0:
            print(f"  進度: {i+1}/{len(stale)} | 補齊 {filled}")
        time.sleep(0.3)

    remaining = [c for c in still_missing
                 if db[c]["history"][-1]["date"] < actual_data_date]
    print(f"✅ yfinance 完成：補齊 {filled} 檔，仍缺漏 {len(remaining)} 檔交由 FinMind")
    return db, remaining


# ── 補齊第三層：FinMind ─────────────────────────────────────────────────────

def fetch_finmind(code, start_date, end_date, token=""):
    params = {"dataset": "TaiwanStockPrice", "data_id": code,
              "start_date": start_date, "end_date": end_date}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        res = requests.get(FINMIND_API_URL, params=params, headers=headers, timeout=20)
        data = res.json()
        if data.get("status") != 200:
            return []
        rows = []
        for item in data.get("data", []):
            dv, cv, vv = item.get("date"), item.get("close"), item.get("Trading_Volume")
            if dv and cv is not None:
                rows.append({"date": dv, "close": round(float(cv), 2),
                             "volume": round(float(vv) / 1000, 2) if vv else 0.0})
        return rows
    except Exception as e:
        print(f"  [{code}] FinMind 失敗: {e}")
        return []


def backfill_finmind(db, actual_data_date, missing_codes, token=""):
    if not missing_codes:
        print("✅ FinMind：無需補齊。")
        return db

    print(f"⚙️  FinMind 補齊：{len(missing_codes)} 檔...")
    filled = 0
    sleep_sec = 6 if token else 12

    for i, code in enumerate(missing_codes):
        last_date = db[code]["history"][-1]["date"]
        start_dt = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        rows = fetch_finmind(code, start_dt, actual_data_date, token=token)

        if rows:
            existing = {r["date"] for r in db[code]["history"]}
            for row in rows:
                if row["date"] not in existing:
                    db[code]["history"].append(row)
                else:
                    for j, h in enumerate(db[code]["history"]):
                        if h["date"] == row["date"]:
                            db[code]["history"][j] = row
                            break
            db[code]["history"] = sorted(db[code]["history"], key=lambda x: x["date"])[-250:]
            filled += 1

        if (i + 1) % 50 == 0:
            print(f"  進度: {i+1}/{len(missing_codes)} | 補齊 {filled}")
        time.sleep(sleep_sec)

    print(f"✅ FinMind 完成：補齊 {filled} 檔")
    return db


# ── 技術指標 ────────────────────────────────────────────────────────────────

def is_ma200_up_10days(ma200_list):
    if len(ma200_list) < 10:
        return False
    last_10 = ma200_list[-10:]
    return all(last_10[i] > last_10[i - 1] for i in range(1, 10))


def calculate_kd(df, n=9):
    low_min = df["close"].rolling(window=n, min_periods=1).min()
    high_max = df["close"].rolling(window=n, min_periods=1).max()
    rsv = (df["close"] - low_min) / (high_max - low_min + 1e-8) * 100
    K = np.zeros(len(df))
    D = np.zeros(len(df))
    for i in range(len(df)):
        if i == 0:
            K[i] = D[i] = 50
        else:
            K[i] = K[i-1] * 2/3 + rsv.iloc[i] * 1/3
            D[i] = D[i-1] * 2/3 + K[i] * 1/3
    return pd.Series(K, index=df.index), pd.Series(D, index=df.index)


# ── 主程式 ───────────────────────────────────────────────────────────────────

def main():
    print("=== 開始每日極速增量更新 ===")

    if not os.path.exists(DB_FILE):
        print(f"找不到 {DB_FILE}！")
        return

    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    # STEP 1：取得今日報價與交易日
    today_quotes, actual_data_date = get_today_quotes()
    if not today_quotes or actual_data_date is None:
        print("今日無資料或非交易日，結束。")
        return
    print(f"實際交易日期: {actual_data_date}")

    # STEP 2：偵測並清除重複寫入的錯誤資料
    already_updated = sum(1 for info in db.values()
                          if info.get("history") and info["history"][-1]["date"] == actual_data_date)
    if already_updated > len(db) * 0.8:
        dup_count = sum(1 for info in db.values()
                        if info.get("history") and len(info["history"]) >= 2
                        and info["history"][-1]["date"] == actual_data_date
                        and round(info["history"][-2]["close"], 2) == round(info["history"][-1]["close"], 2)
                        and round(info["history"][-2]["volume"], 2) == round(info["history"][-1]["volume"], 2))
        if dup_count > len(db) * 0.1:
            cleaned = clean_duplicate_entries(db, actual_data_date)
            print(f"⚠️  清除 {cleaned} 筆重複資料，重新更新...")
        else:
            print(f"✅ 已有 {already_updated} 檔為 {actual_data_date}，資料已是最新，跳過。")
            return

    # STEP 3：TPEX / TWSE 即時 API 更新
    updated_count = 0
    duplicate_skipped = 0
    for code, info in db.items():
        if code not in today_quotes:
            continue
        new_quote = today_quotes[code]
        history = info["history"]

        if is_duplicate(history, new_quote):
            duplicate_skipped += 1
            continue

        if history and history[-1]["date"] == actual_data_date:
            history[-1] = {"date": actual_data_date, **new_quote}
        else:
            history.append({"date": actual_data_date, **new_quote})
        info["history"] = history[-250:]
        updated_count += 1

    print(f"TPEX/TWSE 更新：{updated_count} 檔")
    if duplicate_skipped:
        print(f"重複跳過（API 未更新）：{duplicate_skipped} 檔")

    # STEP 4：yfinance 補缺
    db, still_missing = backfill_yfinance(db, actual_data_date)

    # STEP 5：FinMind 補缺（只補 yfinance 也失敗的）
    finmind_token = os.environ.get("FINMIND_TOKEN", "")
    if finmind_token:
        db = backfill_finmind(db, actual_data_date, still_missing, token=finmind_token)
    elif still_missing:
        print(f"⚠️  未設定 FINMIND_TOKEN，{len(still_missing)} 檔無法補齊。")

    # STEP 6：計算技術指標
    all_stocks_result = []
    for code, info in db.items():
        history = info["history"]
        if len(history) < 220:
            continue
        df = pd.DataFrame(history)
        closes, volumes = df["close"], df["volume"]
        ma5   = closes.rolling(5).mean()
        ma20  = closes.rolling(20).mean()
        ma60  = closes.rolling(60).mean()
        ma200 = closes.rolling(200).mean()
        low20 = closes.rolling(20).min()
        ma200_up    = is_ma200_up_10days(ma200.dropna().tolist())
        ma20_today  = ma20.iloc[-1]
        ma20_yesterday = ma20.iloc[-2] if len(ma20) > 1 else ma20_today
        vol_ma20        = volumes.rolling(20).mean()
        has_vol_burst   = any(volumes.iloc[-10:].iloc[i] > vol_ma20.iloc[-10:].iloc[i] * 2 for i in range(10))
        has_price_burst = any(closes.pct_change().iloc[-10:] * 100 > 5.0)
        high5    = closes.rolling(5).max()
        bias20   = abs(closes.iloc[-1] - ma20_today) / ma20_today * 100 if ma20_today > 0 else 0
        vol_ma5  = volumes.rolling(5).mean()
        max_vol10 = volumes.iloc[-10:].max()
        K, D = calculate_kd(df)
        all_stocks_result.append({
            "code": code, "name": info["name"], "market": info["market"],
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
            "max_vol_10d": round(float(max_vol10), 2),
            "k_value": round(float(K.iloc[-1]), 2),
        })

    # STEP 7：儲存
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)

    tw_now = datetime.now(tz=pytz.timezone("Asia/Taipei"))
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": tw_now.strftime("%Y-%m-%d %H:%M:%S CST"),
            "data_date": actual_data_date,
            "total_valid_stocks": len(all_stocks_result),
            "stocks": all_stocks_result,
        }, f, ensure_ascii=False, indent=2)

    print(f"=== 更新完成 ===")
    print(f"TPEX/TWSE: {updated_count} 檔 | 實際日期: {actual_data_date}")
    print(f"儲存指標: {len(all_stocks_result)} 檔")


if __name__ == "__main__":
    main()
