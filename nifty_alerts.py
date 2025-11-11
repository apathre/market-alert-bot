"""
NIFTY-50 Alert System – Fyers API v3 (stable)
Author: 2025

Features
---------
• Auto-refresh Fyers v3 access token
• Fallback to Yahoo Finance if Fyers unavailable
• Detects RSI divergences + EMA5/EMA21 crossovers (15 min)
• Sends WhatsApp + Email (Zapier Webhook) alerts
• 10 AM IST EMA status alert (skips market holidays/weekends)
• Optional Debug Mode: shows EMA5/EMA21 each run
"""

import pandas as pd
import numpy as np
import requests, time, schedule, datetime, json, os, pytz
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

# =========================================================
# --- Load configuration ---
# =========================================================
CFG_PATH = "config.json"
if not os.path.exists(CFG_PATH):
    raise FileNotFoundError("config.json not found! Please create one.")

with open(CFG_PATH) as f:
    cfg = json.load(f)

FYERS_ID       = cfg["client_id"]
FYERS_SECRET   = cfg["secret_key"]
REDIRECT_URI   = cfg.get("redirect_uri", "https://127.0.0.1/")
ACCESS_TOKEN   = cfg.get("access_token", "")
REFRESH_TOKEN  = cfg.get("refresh_token", "")
WHATSAPP_URL   = cfg.get("whatsapp_url", "")
EMAIL_WEBHOOK  = cfg.get("email_webhook", "")
DEBUG_MODE     = cfg.get("debug_mode", True)

SYMBOL_FYERS   = "NSE:NIFTY50-INDEX"
SYMBOL_YF      = "^NSEI"
INTERVAL       = "15"
LOOKBACK       = 90

# =========================================================
# --- Market holiday detection ---
# =========================================================
HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
_last_holidays = {"date": None, "list": []}

def is_market_holiday(date=None):
    """Check if NSE is closed (weekend or official holiday)."""
    global _last_holidays
    if date is None:
        date = datetime.date.today()

    # Skip weekends
    if date.weekday() >= 5:
        return True

    # Cache NSE holiday list once per week
    if not _last_holidays["date"] or (date - _last_holidays["date"]).days >= 7:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            res = requests.get(HOLIDAY_URL, headers=headers, timeout=10)
            data = res.json()
            holidays = [datetime.datetime.strptime(h["tradingDate"], "%d-%b-%Y").date()
                        for h in data.get("CM", [])]
            _last_holidays = {"date": date, "list": holidays}
        except Exception as e:
            print("⚠️ Could not update NSE holiday list:", e)

    return date in _last_holidays["list"]

# =========================================================
# --- Token refresh ---
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
            print("✅ Fyers token refreshed successfully.")
            return True
        print("⚠️ Token refresh failed:", data)
        return False
    except Exception as e:
        print("⚠️ Error refreshing token:", e)
        return False

# =========================================================
# --- Data fetch (Fyers + Yahoo fallback) ---
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
                print("⚠️ Token invalid; refreshing...")
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
        result = data["chart"]["result"][0]
        df = pd.DataFrame({
            "time": pd.to_datetime(result["timestamp"], unit="s"),
            "open": result["indicators"]["quote"][0]["open"],
            "high": result["indicators"]["quote"][0]["high"],
            "low": result["indicators"]["quote"][0]["low"],
            "close": result["indicators"]["quote"][0]["close"],
            "volume": result["indicators"]["quote"][0]["volume"]
        }).dropna()
        print(f"✅ Using Yahoo REST API data ({len(df)} bars)")
        return df
    except Exception as e:
        print("⚠️ Yahoo fetch failed:", e)
        return None

# =========================================================
# --- Signal computation ---
# =========================================================
def compute_signals(df: pd.DataFrame):
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
        msg = f"🧭 EMA Status — Close: {last['close']:.2f}, EMA5: {last['ema5']:.2f}, " f"EMA21: {last['ema21']:.2f}, Diff: {diff:.2f} → {trend}"
        print(msg)
        send_alert(msg)

    return signals

# =========================================================
# --- Alert delivery ---
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
# --- Regular 15-min job ---
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

# =========================================================
# --- 10AM Daily EMA Status (holiday-adaptive) ---
# =========================================================
def ema_status_alert():
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.datetime.now(ist).date()
    if is_market_holiday(today):
        print("🏖️ Market closed today — skipping 10AM EMA alert.")
        return

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
# --- Scheduler loop ---
# =========================================================
print("🚀 NIFTY-50 Alert System started (15 min + 10 AM EMA summary)")

refresh_fyers_token()
job()
schedule.every(15).minutes.do(job)
schedule.every().day.at("10:00").do(ema_status_alert)

while True:
    schedule.run_pending()
    time.sleep(30)
