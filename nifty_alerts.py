"""
NIFTY-50 Alert System – Render Free-Tier Compatible (Flask + Scheduler)
Author: 2025

Runs as a web service with background scheduler (works on Render Free Tier)
"""

import os, json, time, datetime, threading, requests, schedule, pytz
import pandas as pd
import numpy as np
from flask import Flask
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

# =========================================================
# --- Load configuration ---
# =========================================================
CFG_PATH = "config.json"
if os.path.exists(CFG_PATH):
    with open(CFG_PATH) as f:
        cfg = json.load(f)
else:
    cfg = {}

FYERS_ID       = cfg.get("client_id", os.getenv("FYERS_ID", ""))
FYERS_SECRET   = cfg.get("secret_key", os.getenv("FYERS_SECRET", ""))
REDIRECT_URI   = cfg.get("redirect_uri", os.getenv("REDIRECT_URI", "https://127.0.0.1/"))
ACCESS_TOKEN   = cfg.get("access_token", os.getenv("ACCESS_TOKEN", ""))
REFRESH_TOKEN  = cfg.get("refresh_token", os.getenv("REFRESH_TOKEN", ""))
WHATSAPP_URL   = cfg.get("whatsapp_url", os.getenv("WHATSAPP_URL", ""))
EMAIL_WEBHOOK  = cfg.get("email_webhook", os.getenv("EMAIL_WEBHOOK", ""))
DEBUG_MODE     = json.loads(os.getenv("DEBUG_MODE", "true")).__bool__()

SYMBOL_FYERS   = "NSE:NIFTY50-INDEX"
INTERVAL       = "15"
LOOKBACK       = 90

# =========================================================
# --- Fyers Token Refresh ---
# =========================================================
def refresh_fyers_token():
    global ACCESS_TOKEN, REFRESH_TOKEN
    try:
        url = "https://api-t2.fyers.in/api/v3/validate-refresh-token"
        payload = {
            "grant_type": "refresh_token",
            "appIdHash": FYERS_ID,
            "refresh_token": REFRESH_TOKEN
        }
        res = requests.post(url, json=payload, timeout=10)
        data = res.json()
        if data.get("s") == "ok" and "access_token" in data:
            ACCESS_TOKEN = data["access_token"]
            REFRESH_TOKEN = data.get("refresh_token", REFRESH_TOKEN)
            cfg["access_token"] = ACCESS_TOKEN
            cfg["refresh_token"] = REFRESH_TOKEN
            cfg["last_refresh"] = str(datetime.datetime.now())
            json.dump(cfg, open(CFG_PATH, "w"), indent=2)
            print("✅ Token refreshed successfully.")
            return True
        else:
            print("⚠️ Token refresh failed:", data)
            return False
    except Exception as e:
        print("⚠️ Error refreshing token:", e)
        return False

# =========================================================
# --- Fetch from Fyers / Yahoo ---
# =========================================================
def get_data_fyers():
    try:
        from fyers_apiv3 import fyersModel
        fy = fyersModel.FyersModel(client_id=FYERS_ID, token=ACCESS_TOKEN, log_path=".")
        payload = {
            "symbol": SYMBOL_FYERS,
            "resolution": INTERVAL,
            "date_format": "1",
            "range_from": (datetime.date.today() - datetime.timedelta(days=5)).strftime("%Y-%m-%d"),
            "range_to": datetime.date.today().strftime("%Y-%m-%d"),
            "cont_flag": "1"
        }
        res = fy.history(payload)
        if not res or "candles" not in res:
            if res.get("code") == -16:
                print("⚠️ Token invalid; trying refresh ...")
                if refresh_fyers_token():
                    return get_data_fyers()
            raise ValueError(f"Unexpected response: {res}")
        df = pd.DataFrame(res["candles"], columns=["time","open","high","low","close","volume"])
        df["time"] = pd.to_datetime(df["time"], unit="s")
        print("✅ Using Fyers data")
        return df
    except Exception as e:
        print("⚠️ Fyers fetch failed:", e)
        return None

def get_data_yfinance():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
        params = {"interval": "15m", "range": "5d"}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        if "chart" not in data or not data["chart"].get("result"):
            print("⚠️ Yahoo data invalid.")
            return None
        result = data["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        indicators = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "time": pd.to_datetime(timestamps, unit="s"),
            "open": indicators.get("open", []),
            "high": indicators.get("high", []),
            "low": indicators.get("low", []),
            "close": indicators.get("close", []),
            "volume": indicators.get("volume", [])
        }).dropna()
        print(f"✅ Using Yahoo Finance data ({len(df)} bars)")
        return df
    except Exception as e:
        print("⚠️ Yahoo fetch failed:", e)
        return None

# =========================================================
# --- Signal Computation ---
# =========================================================
def compute_signals(df):
    df["rsi"]   = RSIIndicator(df["close"], window=14).rsi()
    df["ema5"]  = EMAIndicator(df["close"], window=5).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], window=21).ema_indicator()
    df["ema_bull"] = (df["ema5"] > df["ema21"]) & (df["ema5"].shift(1) <= df["ema21"].shift(1))
    df["ema_bear"] = (df["ema5"] < df["ema21"]) & (df["ema5"].shift(1) >= df["ema21"].shift(1))
    df["bull_div"] = (df["close"] < df["close"].shift(LOOKBACK)) & (df["rsi"] > df["rsi"].shift(LOOKBACK))
    df["bear_div"] = (df["close"] > df["close"].shift(LOOKBACK)) & (df["rsi"] < df["rsi"].shift(LOOKBACK))
    last = df.iloc[-1]
    signals = []
    if last["ema_bull"]: signals.append("📈 EMA Bullish Cross — EMA5 > EMA21")
    if last["ema_bear"]: signals.append("📉 EMA Bearish Cross — EMA5 < EMA21")
    if last["bull_div"]: signals.append("🟢 Bullish RSI Divergence")
    if last["bear_div"]: signals.append("🔴 Bearish RSI Divergence")
    if DEBUG_MODE:
        diff = last["ema5"] - last["ema21"]
        trend = "Bullish" if diff > 0 else "Bearish"
        print(f"🧭 EMA Status — Close: {last['close']:.2f}, EMA5: {last['ema5']:.2f}, EMA21: {last['ema21']:.2f}, Diff: {diff:.2f} → {trend}")
    return signals

# =========================================================
# --- Alert Delivery ---
# =========================================================
def send_alert(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    text = f"[{ts}] NIFTY50 Alert:\n{msg}"
    print(text)
    try:
        if WHATSAPP_URL:
            requests.get(f"{WHATSAPP_URL}&text={requests.utils.quote(text)}", timeout=10)
        if EMAIL_WEBHOOK:
            requests.post(EMAIL_WEBHOOK, json={"text": text}, timeout=10)
    except Exception as e:
        print("⚠️ Alert send failed:", e)

# =========================================================
# --- Scheduled Jobs ---
# =========================================================
def job():
    df = get_data_fyers() or get_data_yfinance()
    if df is None or len(df) < 50:
        print("⚠️ No valid data fetched.")
        return
    signals = compute_signals(df)
    if not signals:
        print("⏸️ No new signals this cycle.")
    for s in signals:
        send_alert(s)

def ema_status_alert():
    df = get_data_fyers() or get_data_yfinance()
    if df is None or len(df) < 21:
        print("⚠️ No data for EMA status check.")
        return
    df["ema5"]  = EMAIndicator(df["close"], window=5).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], window=21).ema_indicator()
    last = df.iloc[-1]
    cond = ">" if last["ema5"] > last["ema21"] else "<"
    diff = last["ema5"] - last["ema21"]
    bias = "Bullish Bias" if diff > 0 else "Bearish Bias"
    msg = (
        f"NIFTY50 Daily EMA Summary (10 AM IST):\n"
        f"Close: {last['close']:.2f}\n"
        f"EMA5: {last['ema5']:.2f}\n"
        f"EMA21: {last['ema21']:.2f}\n"
        f"➤ EMA5 {cond} EMA21 → {bias} ({diff:+.2f} pts)"
    )
    send_alert(msg)

# =========================================================
# --- Scheduler Thread + Flask App ---
# =========================================================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ NIFTY Alert Bot is running on Render Free Tier."

def scheduler_loop():
    refresh_fyers_token()
    job()
    schedule.every(15).minutes.do(job)
    schedule.every().day.at("10:00").do(ema_status_alert)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    print("🚀 Starting NIFTY-50 Alert System (Render mode)")
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
