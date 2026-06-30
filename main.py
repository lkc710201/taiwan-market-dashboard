#!/usr/bin/env python3
"""台股空頭訊號監控儀表板"""

import warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import yfinance as yf
import pandas as pd
import requests, re, time, os, asyncio
import cloudscraper
from datetime import datetime, timedelta

_cache: dict = {}
HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


def cached(key, ttl, fn, *args):
    now = time.time()
    if key in _cache and (now - _cache[key]["t"]) < ttl:
        return _cache[key]["v"]
    try:
        v = fn(*args)
        if v is not None:
            _cache[key] = {"v": v, "t": now}
        return v
    except Exception as e:
        print(f"[{key}] error: {e}")
        return _cache.get(key, {}).get("v")


# ─── Data Fetchers ───────────────────────────────────────────────────────────

def _yf_index(ticker, name, period="1y", ma_n=60):
    tk = yf.Ticker(ticker)
    hist = tk.history(period=period)
    if hist.empty:
        return None

    closes = hist["Close"]
    ma = closes.rolling(ma_n).mean()
    latest   = float(closes.iloc[-1])
    latest_ma = float(ma.iloc[-1])
    deviation = (latest - latest_ma) / latest_ma * 100

    ma_clean = ma.dropna()
    slope = 0.0
    if len(ma_clean) >= 5:
        slope = (float(ma_clean.iloc[-1]) - float(ma_clean.iloc[-5])) / float(ma_clean.iloc[-5]) * 100

    prev    = float(closes.iloc[-2]) if len(closes) > 1 else latest
    chg_pct = (latest - prev) / prev * 100

    chart = []
    for i in range(max(0, len(hist) - 130), len(hist)):
        ma_v = float(ma.iloc[i]) if not pd.isna(ma.iloc[i]) else None
        chart.append({
            "d": hist.index[i].strftime("%Y-%m-%d"),
            "c": round(float(closes.iloc[i]), 2),
            "m": round(ma_v, 2) if ma_v else None,
        })

    return {
        "name": name, "ticker": ticker,
        "price": round(latest, 2), "ma": round(latest_ma, 2),
        "deviation": round(deviation, 2), "slope": round(slope, 3),
        "bearish": deviation < 0, "chg_pct": round(chg_pct, 2),
        "chart": chart,
    }


def _finmind_foreign_flow():
    """外資近20交易日買賣超 — FinMind TaiwanStockTotalInstitutionalInvestors
    單位: 元 → 億元 (/100,000,000)"""
    start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    r = requests.get("https://api.finmindtrade.com/api/v4/data", params={
        "dataset": "TaiwanStockTotalInstitutionalInvestors",
        "start_date": start,
    }, timeout=12)
    d = r.json()
    if d.get("status") != 200 or not d.get("data"):
        return None

    rows = sorted(
        [x for x in d["data"] if x["name"] == "Foreign_Investor"],
        key=lambda x: x["date"], reverse=True
    )[:20]
    if not rows:
        return None

    def yi(buy, sell):
        return round((int(buy) - int(sell)) / 100_000_000, 1)

    daily    = [{"date": r["date"], "yi": yi(r["buy"], r["sell"])} for r in rows]
    total_yi = sum(x["yi"] for x in daily)

    return {
        "latest_yi":    daily[0]["yi"],
        "latest_date":  daily[0]["date"],
        "total_20d_yi": round(total_yi, 1),
        "bearish":      total_yi < -1500,
        "daily":        daily,
    }


_NDC_FILE = os.path.join(os.path.dirname(__file__), "ndc_data.json")

def _ndc_from_file():
    """讀取本地 ndc_data.json 作為備份資料"""
    try:
        import json as _json
        with open(_NDC_FILE, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None

def _ndc_from_api():
    """從 NDC API 取得最新景氣燈號（需要 Cloudflare bypass）"""
    NDC_HOME = "https://index.ndc.gov.tw/n/zh_tw"
    NDC_API  = "https://index.ndc.gov.tw/n/json/lightscore"
    try:
        scraper = cloudscraper.create_scraper()
        r0 = scraper.get(NDC_HOME, timeout=25)
        csrf = re.search(r'name="csrf-token"\s+content="([^"]+)"', r0.text)
        if not csrf:
            return None
        r = scraper.post(NDC_API, data="",
            headers={"X-CSRF-TOKEN": csrf.group(1),
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": NDC_HOME,
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "Accept": "application/json, text/javascript, */*; q=0.01"},
            timeout=25)
        if r.status_code != 200 or "application/json" not in r.headers.get("Content-Type",""):
            return None
        data = r.json()
        line = data.get("line", [])
        if not line:
            return None
        latest = line[-1]
        score  = int(latest["y"])
        yyyymm = str(latest["x"])
        if   score <= 16: light = "blue"
        elif score <= 22: light = "yellow"
        elif score <= 31: light = "green"
        elif score <= 37: light = "yellow_red"
        else:             light = "red"
        labels = {"red":"紅燈","yellow_red":"黃紅燈",
                  "green":"綠燈","yellow":"黃藍燈","blue":"藍燈"}
        result = {
            "score": score, "light": light, "label": labels[light],
            "bearish": light in ("yellow","blue"),
            "date": f"{yyyymm[:4]}-{yyyymm[4:]}",
            "history": [{"ym": x["x"], "score": int(x["y"])} for x in line[-12:]],
        }
        # 成功時更新本地檔案
        try:
            import json as _json
            with open(_NDC_FILE, "w", encoding="utf-8") as f:
                _json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return result
    except Exception as e:
        print(f"NDC API: {e}")
        return None

def _ndc_cycle():
    """景氣燈號：優先 API，失敗時讀 ndc_data.json（每月手動更新）"""
    result = _ndc_from_api()
    if result:
        return result
    print("NDC API failed, using local ndc_data.json")
    return _ndc_from_file()


def _m1b_m2():
    """M1b/M2 年增率 — CBC 統計資料庫 GetJsonFromArray API (verify=False)
    PX file: EF15M01.px = 貨幣總計數 日平均數 月
    回傳 M1b/M2 年增率 及 死亡/黃金交叉判斷"""
    try:
        CBC = "https://cpx.cbc.gov.tw"
        HDR_CBC = {**HDR, "Content-Type": "application/json",
                   "X-Requested-With": "XMLHttpRequest",
                   "Referer": f"{CBC}/Data/DataMain/?pxfilename=EF15M01.px"}

        # Step 1: get all available time periods
        rng = requests.post(f"{CBC}/Range/GetJsonRangeData",
                            json={"pxfilename": "EF15M01.px"},
                            headers=HDR_CBC, verify=False, timeout=12)
        rng.raise_for_status()
        rng_data = rng.json()
        dims = {v["Key"]: v["Value"] for v in rng_data.get("values", [])}
        all_periods = dims.get("期間", [])
        if not all_periods:
            return None

        # Step 2: fetch M1b & M2 YoY for last 25 months
        r = requests.post(f"{CBC}/Data/GetJsonFromArray",
                          json={
                              "rotateCnt": 0,
                              "pxfilename": "EF15M01.px",
                              "range": {
                                  "values": [
                                      {"key": "期間", "value": all_periods[-25:]},
                                      {"key": "項目", "value": ["貨幣總計數 -Ｍ１Ｂ", "貨幣總計數 -Ｍ２"]},
                                      {"key": "種類", "value": ["年增率"]},
                                  ]
                              },
                          },
                          headers=HDR_CBC, verify=False, timeout=12)
        r.raise_for_status()
        import json as _json
        data = _json.loads(r.json())  # double-encoded JSON string
        rows = data.get("data", [])
        if not rows:
            return None

        latest     = rows[-1]
        period_str = str(latest[0])            # e.g. "2026M05"
        m1b_yoy    = float(latest[1])
        m2_yoy     = float(latest[2])
        date_label = f"{period_str[:4]}/{period_str[5:]}"  # "2026/05"

        # Build chart history: last 25 rows
        chart = [{"ym": row[0], "m1b": float(row[1]), "m2": float(row[2])} for row in rows]

        return {
            "m1b_yoy":    round(m1b_yoy, 2),
            "m2_yoy":     round(m2_yoy, 2),
            "death_cross": m1b_yoy < m2_yoy,
            "bearish":     m1b_yoy < m2_yoy,
            "date":        date_label,
            "chart":       chart,
        }
    except Exception as e:
        print(f"M1b/M2: {e}")
    return None


# ─── Background auto-refresh ─────────────────────────────────────────────────

def _refresh_all():
    """Warm every cache bucket; called on startup and every 5 min."""
    cached("twii",    300,  _yf_index, "^TWII",    "加權指數",   "1y",  60)
    cached("sox",     300,  _yf_index, "^SOX",     "費城半導體", "1y",  60)
    cached("dxy",     300,  _yf_index, "DX-Y.NYB", "美元指數",   "6mo", 20)
    cached("us10y",   300,  _yf_index, "^TNX",     "美債10Y",   "6mo", 20)
    cached("foreign", 1800, _finmind_foreign_flow)
    cached("m1b_m2",  3600, _m1b_m2)
    cached("ndc",     3600, _ndc_cycle)
    print(f"[refresh] done at {datetime.now().strftime('%H:%M:%S')}")

async def _scheduler():
    """Run _refresh_all every 5 minutes in the background."""
    while True:
        await asyncio.sleep(300)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _refresh_all)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm cache immediately on startup (non-blocking)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _refresh_all)
    # Start background scheduler
    asyncio.create_task(_scheduler())
    yield

app = FastAPI(title="台股空頭訊號監控", lifespan=lifespan)

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/api/all")
async def get_all():
    return JSONResponse({
        "twii":    cached("twii",    300,  _yf_index, "^TWII",    "加權指數",    "1y",  60),
        "sox":     cached("sox",     300,  _yf_index, "^SOX",     "費城半導體",  "1y",  60),
        "dxy":     cached("dxy",     300,  _yf_index, "DX-Y.NYB", "美元指數",    "6mo", 20),
        "us10y":   cached("us10y",   300,  _yf_index, "^TNX",     "美債10Y",    "6mo", 20),
        "foreign": cached("foreign", 1800, _finmind_foreign_flow),
        "m1b_m2":  cached("m1b_m2", 3600, _m1b_m2),
        "ndc":     cached("ndc",    3600,  _ndc_cycle),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/", response_class=HTMLResponse)
async def root():
    path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
