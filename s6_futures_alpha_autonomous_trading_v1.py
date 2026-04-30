#!/usr/bin/env python3
"""
市场扫描器 - 每分钟运行
纯Python零AI成本，发现异常信号自动开仓
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "trades.json")
SCANNER_STATE = os.path.join(SCRIPT_DIR, "scanner_state.json")
SCANNER_LOG = os.path.join(SCRIPT_DIR, "scanner.log")
INITIAL_BALANCE = 100.0
TZ_UTC8 = timezone(timedelta(hours=8))

# === 配置 ===
MAX_OPEN_POSITIONS = 3       # 最多同时持仓
POSITION_PCT = 30            # 每笔仓位占比%
LEVERAGE = 3                 # 杠杆
COOLDOWN_HOURS = 4           # 同一币种冷却时间
MIN_VOLUME_M = 10            # 最小24h成交额(百万U)

# === TG推送 ===
def load_tg_config():
    """Load TG config from environment variables or .env file"""
    env = {}
    # Try .env in script directory, then current directory
    for env_path in [
        os.path.join(SCRIPT_DIR, ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        env[k] = v.strip().strip('"').strip("'")
            break
    # OS environment variables override file
    for key in ['TG_BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TG_CHAT_ID']:
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env

def send_tg(text):
    try:
        env = load_tg_config()
        token = env.get('TG_BOT_TOKEN', env.get('TELEGRAM_BOT_TOKEN', ''))
        if not token:
            return
        chat_id = env.get('TG_CHAT_ID', '')
        if not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=10)
    except:
        pass


# === 数据加载 ===
def load_trades():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"initial_balance": INITIAL_BALANCE, "trades": []}

def save_trades(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_state():
    if os.path.exists(SCANNER_STATE):
        with open(SCANNER_STATE, "r") as f:
            return json.load(f)
    return {"last_opens": {}, "signals_seen": {}}

def save_state(state):
    with open(SCANNER_STATE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def get_balance(data):
    balance = data.get("initial_balance", INITIAL_BALANCE)
    for t in data["trades"]:
        if t["status"] == "closed" and t["pnl_usd"] is not None:
            balance += t["pnl_usd"]
    return balance

def next_id(data):
    if not data["trades"]:
        return "001"
    max_id = max(int(t["id"]) for t in data["trades"])
    return f"{max_id + 1:03d}"

def now_str():
    return datetime.now(TZ_UTC8).strftime("%Y-%m-%dT%H:%M:%S")

def log(msg):
    ts = datetime.now(TZ_UTC8).strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(SCANNER_LOG, "a") as f:
        f.write(line + "\n")


# === 币安API ===
def get_all_tickers():
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    resp = requests.get(url, timeout=10)
    return resp.json()

def get_funding_rates():
    """获取所有币种最新费率"""
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    resp = requests.get(url, timeout=10)
    return {item['symbol']: float(item['lastFundingRate']) * 100 
            for item in resp.json()}

def get_funding_history(symbol, limit=8):
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit={limit}"
    resp = requests.get(url, timeout=10)
    return [float(item['fundingRate']) * 100 for item in resp.json()]

def get_open_interest(symbol):
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
    resp = requests.get(url, timeout=10)
    data = resp.json()
    return float(data['openInterest'])

def get_klines(symbol, interval="4h", limit=6):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    resp = requests.get(url, timeout=10)
    return resp.json()


# === 信号检测 ===

def detect_extreme_negative_funding(symbol, funding_rate, funding_rates_map):
    """
    策略1: 费率极端深负 → 做多(逼空)
    条件: 当前费率<-0.08% 且 连续多期为负
    """
    if funding_rate >= -0.08:
        return None
    
    try:
        history = get_funding_history(symbol, 8)
        neg_count = sum(1 for r in history if r < -0.03)
        if neg_count < 4:
            return None
        
        avg_rate = sum(history) / len(history)
        
        # 费率越极端，信号越强
        strength = "S" if avg_rate < -0.15 else "A" if avg_rate < -0.10 else "B"
        
        return {
            "type": "extreme_neg_funding",
            "direction": "long",
            "strength": strength,
            "reason": f"费率极端深负 avg:{avg_rate:.4f}% 连续{neg_count}/8期为负 逼空概率高",
            "sl_pct": 0.08,   # 止损8%
            "tp_pct": 0.12,   # 止盈12%
        }
    except:
        return None


def detect_extreme_positive_funding(symbol, funding_rate, funding_rates_map):
    """
    策略2: 费率极端正 → 做空(多头拥挤)
    条件: 当前费率>0.10% 且 连续多期高正
    """
    if funding_rate <= 0.10:
        return None
    
    try:
        history = get_funding_history(symbol, 8)
        pos_count = sum(1 for r in history if r > 0.05)
        if pos_count < 4:
            return None
        
        avg_rate = sum(history) / len(history)
        strength = "S" if avg_rate > 0.20 else "A" if avg_rate > 0.12 else "B"
        
        return {
            "type": "extreme_pos_funding",
            "direction": "short",
            "strength": strength,
            "reason": f"费率极端正 avg:{avg_rate:.4f}% 连续{pos_count}/8期高正 多头过度拥挤",
            "sl_pct": 0.10,
            "tp_pct": 0.15,
        }
    except:
        return None


def detect_crash_bounce(ticker):
    """
    策略3: 暴跌后反弹(超跌反弹)
    条件: 24h跌>25% 但最近4h企稳/反弹
    """
    change_pct = float(ticker['priceChangePercent'])
    if change_pct >= -25:
        return None
    
    symbol = ticker['symbol']
    try:
        klines = get_klines(symbol, "1h", 6)
        # 最近2根K线
        recent_closes = [float(k[4]) for k in klines[-3:]]
        # 企稳: 最近K线收盘 >= 前一根
        if len(recent_closes) >= 2 and recent_closes[-1] >= recent_closes[-2]:
            return {
                "type": "crash_bounce",
                "direction": "long",
                "strength": "B",  # 风险较高给B级
                "reason": f"24h暴跌{change_pct:.1f}%后企稳 超跌反弹",
                "sl_pct": 0.10,
                "tp_pct": 0.15,
            }
    except:
        pass
    return None


def detect_pump_short(ticker):
    """
    策略4: 暴涨后做空(ATH回落)
    条件: 24h涨>40% — 根据生命周期模型，暴涨后回调概率>85%
    需要确认已经开始回落(不在最高点做空)
    """
    change_pct = float(ticker['priceChangePercent'])
    if change_pct <= 40:
        return None
    
    symbol = ticker['symbol']
    try:
        klines = get_klines(symbol, "1h", 6)
        highs = [float(k[2]) for k in klines]
        closes = [float(k[4]) for k in klines]
        current = closes[-1]
        peak = max(highs)
        
        # 从最高点回落超过10%才做空
        pullback = (peak - current) / peak * 100
        if pullback < 10:
            return None
        
        strength = "A" if change_pct > 80 else "B"
        
        return {
            "type": "pump_short",
            "direction": "short",
            "strength": strength,
            "reason": f"24h暴涨{change_pct:.1f}%后回落{pullback:.1f}% 历史回调概率>85%",
            "sl_pct": 0.15,   # 暴涨币波动大，止损宽一些
            "tp_pct": 0.20,
        }
    except:
        pass
    return None


# === 综合环境检查 ===
def check_environment(symbol, signal):
    """
    开仓前综合检查：不是单一信号触发就开，要多维度对齐
    返回 (pass/fail, analysis_dict, adjusted_strength)
    """
    analysis = {
        "btc_env": "",
        "sentiment": "",
        "oi_check": "",
        "volume_check": "",
        "verdict": ""
    }
    score = 0  # 综合得分，>=3才开仓
    
    try:
        # 1. BTC环境 — 做多需要BTC不在暴跌，做空需要BTC不在暴涨
        btc_url = "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT"
        btc = requests.get(btc_url, timeout=5).json()
        btc_chg = float(btc['priceChangePercent'])
        
        if signal["direction"] == "long":
            if btc_chg > -2:
                score += 1
                analysis["btc_env"] = f"BTC {btc_chg:+.1f}% 环境正常 +1"
            elif btc_chg < -5:
                score -= 1
                analysis["btc_env"] = f"BTC {btc_chg:+.1f}% 暴跌中做多危险 -1"
            else:
                analysis["btc_env"] = f"BTC {btc_chg:+.1f}% 偏弱 0"
        else:  # short
            if btc_chg < 2:
                score += 1
                analysis["btc_env"] = f"BTC {btc_chg:+.1f}% 环境正常 +1"
            elif btc_chg > 5:
                score -= 1
                analysis["btc_env"] = f"BTC {btc_chg:+.1f}% 暴涨中做空危险 -1"
            else:
                analysis["btc_env"] = f"BTC {btc_chg:+.1f}% 偏强 0"
        
        # 2. 市场情绪(Fear & Greed)
        try:
            fng = requests.get("https://api.alternative.me/fng/", timeout=5).json()
            fng_val = int(fng['data'][0]['value'])
            if signal["direction"] == "long":
                if fng_val <= 25:
                    score += 1
                    analysis["sentiment"] = f"FGI={fng_val}极度恐惧 逆向做多 +1"
                elif fng_val >= 75:
                    score -= 1
                    analysis["sentiment"] = f"FGI={fng_val}极度贪婪 做多风险 -1"
                else:
                    analysis["sentiment"] = f"FGI={fng_val}中性 0"
            else:
                if fng_val >= 75:
                    score += 1
                    analysis["sentiment"] = f"FGI={fng_val}极度贪婪 逆向做空 +1"
                elif fng_val <= 25:
                    score -= 1
                    analysis["sentiment"] = f"FGI={fng_val}极度恐惧 做空风险 -1"
                else:
                    analysis["sentiment"] = f"FGI={fng_val}中性 0"
        except:
            analysis["sentiment"] = "FGI获取失败 0"
        
        # 3. OI变化 — 看该币OI是否支持方向
        try:
            oi = get_open_interest(symbol)
            ticker = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}", timeout=5).json()
            price = float(ticker['lastPrice'])
            oi_usd = oi * price
            
            if oi_usd > 5_000_000:  # OI > 5M说明有关注度
                score += 1
                analysis["oi_check"] = f"OI={oi_usd/1e6:.1f}M 有关注度 +1"
            else:
                analysis["oi_check"] = f"OI={oi_usd/1e6:.1f}M 关注度低 0"
        except:
            analysis["oi_check"] = "OI获取失败 0"
        
        # 4. 成交量 — 量能是否活跃
        try:
            vol = float(ticker.get('quoteVolume', 0))
            if vol > 50_000_000:
                score += 1
                analysis["volume_check"] = f"24h量={vol/1e6:.0f}M 活跃 +1"
            elif vol > 20_000_000:
                analysis["volume_check"] = f"24h量={vol/1e6:.0f}M 一般 0"
            else:
                score -= 1
                analysis["volume_check"] = f"24h量={vol/1e6:.0f}M 冷清 -1"
        except:
            analysis["volume_check"] = "量能获取失败 0"
        
        # 5. 信号本身的强度加分
        if signal["strength"] == "S":
            score += 2
        elif signal["strength"] == "A":
            score += 1
        
        # 综合判定: >=3通过
        analysis["verdict"] = f"综合得分:{score}/7"
        
        if score >= 3:
            return True, analysis, signal["strength"]
        else:
            return False, analysis, signal["strength"]
            
    except Exception as e:
        analysis["verdict"] = f"检查异常:{e} 保守不开"
        return False, analysis, signal["strength"]


# ... 完整代码见 GitHub: github.com/connectfarm1/ai-autonomous-trading ...