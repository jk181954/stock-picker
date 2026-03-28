import requests
import json
import os
import yfinance as yf
import pandas as pd
from datetime import datetime
import time

def fetch_all_stock_list():
    stocks = []
    seen_codes = set()

    # 抓上市
    try:
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url_twse, timeout=15)
        if res.status_code == 200:
            for item in res.json():
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip()
                if len(code) == 4 and code.isdigit() and code not in seen_codes:
                    stocks.append({"code": code, "name": name, "market": "上市"})
                    seen_codes.add(code)
    except Exception as e:
        print(f"上市清單失敗: {e}")

    # 抓上櫃
    try:
        url_tpex = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
        res = requests.get(url_tpex, timeout=15)
        if res.status_code == 200:
            for item in res.json():
                code = str(item.get("SecuritiesCompanyCode", "")).strip()
                name = str(item.get("CompanyName", "")).strip()
                if len(code) == 4 and code.isdigit() and code not in seen_codes:
                    stocks.append({"code": code, "name": name, "market": "上櫃"})
                    seen_codes.add(code)
    except Exception as e:
        print(f"上櫃清單失敗: {e}")

    return stocks

def is_ma200_up_10days(ma200_series):
    last_10 = ma200_series.tail(10).tolist()
    if len(last_10) < 10 or pd.isna(last_10).any():
        return False
    for i in range(1, 10):
        if last_10[i] <= last_10[i-1]:
            return False
    return True

def main():
    print("=== 開始獲取台股清單 ===")
    stocks_info = fetch_all_stock_list()
    
    if not stocks_info:
        print("無法取得股票清單")
        return

    print(f"共取得 {len(stocks_info)} 檔普通股。開始透過 yfinance 下載...")

    # 把股票代碼轉成 YF 格式
    # Yahoo 對台灣股票的後綴滿混亂的，但大多數上市用 .TW，上櫃用 .TWO
    all_tickers = []
    ticker_to_info = {}

    for s in stocks_info:
        suffix = ".TW" if s["market"] == "上市" else ".TWO"
        yf_ticker = f"{s['code']}{suffix}"
        all_tickers.append(yf_ticker)
        ticker_to_info[yf_ticker] = s

    results = []
    checked_count = 0
    failed_count = 0

    # 為了不被 Yahoo 鎖 IP，我們一次下載 50 檔，並且不用多執行緒 (threads=False)
    batch_size = 50
    
    for i in range(0, len(all_tickers), batch_size):
        batch_tickers = all_tickers[i:i + batch_size]
        print(f"\n進度: 處理第 {i+1} 到 {i+len(batch_tickers)} 檔...")
        
        # threads=False 非常重要，強制單線程排隊下載，不惹怒 Yahoo
        try:
            data = yf.download(
                batch_tickers, 
                period="1y", 
                interval="1d", 
                group_by="ticker", 
                auto_adjust=False, 
                prepost=False, 
                threads=False, 
                progress=False
            )
            
            # 給 Yahoo 喘口氣
            time.sleep(2)
            
        except Exception as e:
            print(f"批次下載失敗: {e}")
            failed_count += len(batch_tickers)
            continue

        for ticker in batch_tickers:
            checked_count += 1
            info = ticker_to_info[ticker]
            
            try:
                if len(batch_tickers) == 1:
                    df = data.copy()
                else:
                    df = data[ticker].copy()
                
                df = df.dropna(subset=['Close', 'Volume'])
                if len(df) < 220:
                    continue

                close_series = df['Close']
                volume_series = df['Volume']

                ma5 = close_series.rolling(window=5).mean()
                ma20 = close_series.rolling(window=20).mean()
                ma60 = close_series.rolling(window=60).mean()
                ma200 = close_series.rolling(window=200).mean()
                
                lowest_close_20 = close_series.rolling(window=20).min()

                latest_close = close_series.iloc[-1]
                latest_vol = volume_series.iloc[-1] / 1000 
                
                c_ma5 = ma5.iloc[-1]
                c_ma20 = ma20.iloc[-1]
                c_ma60 = ma60.iloc[-1]
                c_ma200 = ma200.iloc[-1]
                # 抓過去 20 日最低
                c_low20 = lowest_close_20.iloc[-2] 
                
                if pd.isna(c_ma5) or pd.isna(c_ma20) or pd.isna(c_ma60) or pd.isna(c_ma200):
                    continue

                ma200_up = is_ma200_up_10days(ma200)

                # 策略條件
                passed = (
                    latest_close > c_ma5 and 
                    latest_close > c_ma20 and 
                    latest_close > c_ma60 and
                    c_low20 < c_ma20 and
                    latest_vol > 500 and
                    latest_close < c_ma200 * 1.4 and
                    ma200_up
                )

                if passed:
                    results.append({
                        "code": info["code"],
                        "name": info["name"],
                        "market": info["market"],
                        "close": round(float(latest_close), 2),
                        "ma5": round(float(c_ma5), 2),
                        "ma20": round(float(c_ma20), 2),
                        "ma60": round(float(c_ma60), 2),
                        "ma200": round(float(c_ma200), 2),
                        "lowestClose20": round(float(c_low20), 2),
                        "volume": round(float(latest_vol), 2),
                    })
                    print(f"🔥 找到標的: {info['code']} {info['name']}")

            except Exception:
                failed_count += 1
                pass

    # 寫入 json
    output_data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "checked_count": checked_count,
        "matched_count": len(results),
        "failed_count": failed_count,
        "stocks": results
    }

    with open("stocks.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print("\n=== 掃描完成 ===")
    print(f"總計掃描: {checked_count} 檔")
    print(f"無法解析 (無資料/下市): {failed_count} 檔")
    print(f"符合策略: {len(results)} 檔")

if __name__ == "__main__":
    main()
