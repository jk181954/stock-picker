import requests
import json
import os
import time
import yfinance as yf
import pandas as pd
from datetime import datetime

# 1. 抓取上市 + 上櫃清單
def fetch_all_stock_list():
    stocks = []
    seen_codes = set()
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

    print(f"共取得 {len(stocks_info)} 檔普通股。開始透過 yfinance 聰明批次下載...")

    # 把股票代碼轉成 YF 格式
    all_tickers = []
    ticker_to_info = {}

    for s in stocks_info:
        suffix = ".TW" if s["market"] == "上市" else ".TWO"
        yf_ticker = f"{s['code']}{suffix}"
        all_tickers.append(yf_ticker)
        ticker_to_info[yf_ticker] = s

    all_stocks_data = []
    checked_count = 0
    failed_count = 0

    # 聰明切塊：每次只問 20 檔，避免被 Yahoo 鎖 IP
    batch_size = 20
    
    for i in range(0, len(all_tickers), batch_size):
        batch_tickers = all_tickers[i:i + batch_size]
        print(f"進度: 處理第 {i+1} 到 {i+len(batch_tickers)} 檔...")
        
        try:
            # 使用 pandas datareader 核心的 yf.download
            # threads=False 避免多線程引發連線阻擋
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
            
            # 每批次要乖乖休息 2 秒，假裝是人類在查資料
            time.sleep(2)
            
        except Exception as e:
            print(f"批次下載失敗: {e}")
            failed_count += len(batch_tickers)
            time.sleep(5) # 被擋就睡久一點
            continue

        for ticker in batch_tickers:
            checked_count += 1
            info = ticker_to_info[ticker]
            
            try:
                if len(batch_tickers) == 1:
                    df = data.copy()
                else:
                    df = data[ticker].copy()
                
                # 排除沒有資料的爛股
                if df.empty or 'Close' not in df.columns:
                    continue

                df = df.dropna(subset=['Close', 'Volume'])
                
                # 如果這檔股票剛上市不到 220 天，算不出 200MA，就跳過
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
                # yfinance 的台股成交量通常是「股數」，所以除以 1000 變「張數」
                latest_vol = volume_series.iloc[-1] / 1000 
                
                c_ma5 = ma5.iloc[-1]
                c_ma20 = ma20.iloc[-1]
                c_ma60 = ma60.iloc[-1]
                c_ma200 = ma200.iloc[-1]
                c_low20 = lowest_close_20.iloc[-2] 
                
                if pd.isna(c_ma5) or pd.isna(c_ma20) or pd.isna(c_ma60) or pd.isna(c_ma200):
                    continue

                ma200_up = is_ma200_up_10days(ma200)

                all_stocks_data.append({
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
                    "ma200_up_10days": ma200_up
                })

            except Exception:
                failed_count += 1
                pass

    # 寫入 json
    output_data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_valid_stocks": len(all_stocks_data),
        "stocks": all_stocks_data
    }

    with open("all_stocks_data.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print("\n=== 掃描完成 ===")
    print(f"總計掃描: {checked_count} 檔")
    print(f"無法解析 (無資料/下市/剛上市): {failed_count} 檔")
    print(f"成功儲存 {len(all_stocks_data)} 檔股票的技術指標至 all_stocks_data.json！")

if __name__ == "__main__":
    main()
