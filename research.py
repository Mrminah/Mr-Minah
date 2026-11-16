import os, requests
from datetime import datetime

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()

SYMBOL = "BTC/USD"
N = 3
BUFFER_PCT = 0.05    # SL buffer, % of price
SPREAD_PCT = 0.03    # assumed cost per trade, % of price
MAX_HOLD = 100
WINDOW = 149
TARGETS = [2, 3, 5, 10, 20]
MIN_RISK_PCT = {"1h": 0.4, "4h": 0.8, "1day": 2.0}

def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text[:4000]}, timeout=20)
    print("TELEGRAM:", r.status_code)

def parse(s):
    s = s.strip()
    if len(s) == 10:
        s += " 00:00:00"
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")

def get_candles(interval, size=5000):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": SYMBOL, "interval": interval,
                "outputsize": size, "apikey": KEY}, timeout=90)
    d = r.json()
    if "values" not in d:
        print("Error", interval, d.get("message"))
        return None, d.get("message", "unknown")
    rows = [{"dt": parse(v["datetime"]), "o": float(v["open"]),
             "h": float(v["high"]), "l": float(v["low"]),
             "c": float(v["close"])} for v in reversed(d["values"])]
    return rows[:-1], None

def swings(rows):
    highs, lows = [], []
    for i in range(N, len(rows) - N):
        w = rows[i - N:i + N + 1]
        if rows[i]["h"] == max(x["h"] for x in w):
            highs.append(rows[i]["h"])
        if rows[i]["l"] == min(x["l"] for x in w):
            lows.append(rows[i]["l"])
    return highs, lows

def buf(p):
    return p * BUFFER_PCT / 100

def build(side, entry, sl, tf):
    if abs(entry - sl) / entry * 100 < MIN_RISK_PCT[tf]:
        return None
    return (side, entry, sl)

def setup_mss(rows, tf):
    highs, lows = swings(rows)
    if len(highs) < 2 or len(lows) < 2:
        return None
    last, prev = rows[-1], rows[-2]
    bodies = [abs(x["c"] - x["o"]) for x in rows[-21:-1]]
    avg = sum(bodies) / len(bodies)
    body = abs(last["c"] - last["o"])
    rng = last["h"] - last["l"]
    if rng <= 0 or body < 1.5 * avg or body < 0.6 * rng:
        return None
    b = buf(last["c"])
    was_down = highs[-1] < highs[-2] and lows[-1] < lows[-2]
    was_up = highs[-1] > highs[-2] and lows[-1] > lows[-2]
    if was_down and last["c"] > last["o"] \
            and prev["c"] <= highs[-1] < last["c"]:
        return build("BUY", last["c"], lows[-1] - b, tf)
    if was_up and last["c"] < last["o"] \
            and prev["c"] >= lows[-1] > last["c"]:
        return build("SELL", last["c"], highs[-1] + b, tf)
    return None

def setup_sweep(rows, tf):
    highs, lows = swings(rows)
    last, prev = rows[-1], rows[-2]
    b = buf(last["c"])
    if highs:
        H = highs[-1]
        if last["h"] > H and last["c"] < H and prev["h"] <= H:
            return build("SELL", last["c"], last["h"] + b, tf)
    if lows:
        L = lows[-1]
        if last["l"] < L and last["c"] > L and prev["l"] >= L:
            return build("BUY", last["c"], last["l"] - b, tf)
    return None

SETUPS = [("MSS", setup_mss), ("Sweep", setup_sweep)]

def mfe(rows, i, side, entry, sl):
    risk = abs(entry - sl)
    best = 0.0
    end = min(i + 1 + MAX_HOLD, len(rows))
    for j in range(i + 1, end):
        h, l = rows[j]["h"], rows[j]["l"]
        if side == "BUY":
            if l <= sl:
                return best, j
            best = max(best, (h - entry) / risk)
        else:
            if h >= sl:
                return best, j
            best = max(best, (entry - l) / risk)
    return best, end - 1

def spike(rows, i):
    base = rows[i - 23:i - 3]
    avg = sum(x["h"] - x["l"] for x in base) / len(base)
    return any((x["h"] - x["l"]) >= 2 * avg for x in rows[i - 3:i + 1])

def avgr(items, T):
    if not items:
        return 0.0
    return sum((T if x["m"] >= T else -1) - x["cost"]
               for x in items) / len(items)

def hitp(items, T):
    return sum(1 for x in items if x["m"] >= T) / len(items) * 100

def halves(items, T):
    items = sorted(items, key=lambda x: x["dt"])
    k = len(items) // 2
    return avgr(items[:k], T), avgr(items[k:], T)

def short(label, items):
    if not items:
        return f"{label}: 0 trades"
    return (f"{label}: n={len(items)}, 3R {hitp(items, 3):.0f}%, "
            f"10R {hitp(items, 10):.0f}%, avg R at 3R "
            f"{avgr(items, 3):+.2f}, at 10R {avgr(items, 10):+.2f}")

res, spans, errs = {}, [], []
for tf in ["1h", "4h", "1day"]:
    rows, err = get_candles(tf)
    if not rows:
        spans.append(f"{tf}: no data ({err})")
        errs.append(tf)
        continue
    spans.append(f"{tf}: {rows[0]['dt']:%Y-%m-%d} to "
                 f"{rows[-1]['dt']:%Y-%m-%d}")
    busy = {}
    for i in range(WINDOW, len(rows) - 1):
        win = rows[i - WINDOW + 1:i + 1]
        for name, fn in SETUPS:
            if busy.get(name, -1) >= i:
                continue
            s = fn(win, tf)
            if not s:
                continue
            side, e, sl = s
            m, j = mfe(rows, i, side, e, sl)
            busy[name] = j
            risk = abs(e - sl)
            res.setdefault((tf, name), []).append(
                {"m": m, "cost": e * SPREAD_PCT / 100 / risk,
                 "dt": rows[i]["dt"], "spike": spike(rows, i)})

m1 = [f"{SYMBOL} STUDY 1: how often do setups reach big R?",
      "Stop checked first. Break-even win rate: 1:3 = 27%, "
      "1:5 = 17%, 1:10 = 9%, 1:20 = 5%.",
      f"Spread assumed {SPREAD_PCT}% of price.", *spans, ""]
for tf in ["1h", "4h", "1day"]:
    for name, _ in SETUPS:
        it = res.get((tf, name), [])
        if len(it) < 4:
            m1.append(f"{tf} {name}: {len(it)} trades (too few)")
            continue
        m1.append(f"{tf} {name}: {len(it)} trades")
        m1.append("hit %: " + " | ".join(f"{T}R {hitp(it, T):.0f}"
                                         for T in TARGETS))
        m1.append("avg R: " + " | ".join(f"{T}R {avgr(it, T):+.2f}"
                                         for T in TARGETS))
        hs = []
        for T in (3, 5, 10):
            a, b = halves(it, T)
            hs.append(f"{T}R {a:+.2f}/{b:+.2f}")
        m1.append("1st half / 2nd half avg R: " + " | ".join(hs))
send("\n".join(m1))

m2 = [f"{SYMBOL} STUDY 2: volatility and sessions", ""]
for tf in ["4h", "1day"]:
    items = res.get((tf, "MSS"), []) + res.get((tf, "Sweep"), [])
    m2.append(f"== {tf} ==")
    m2.append(short("After spike", [x for x in items if x["spike"]]))
    m2.append(short("Calm", [x for x in items if not x["spike"]]))
items = res.get(("1h", "MSS"), []) + res.get(("1h", "Sweep"), [])
m2.append("== 1h by session (UTC hours) ==")
for label, a, b in [("Asia 0-6", 0, 7), ("London 7-11", 7, 12),
                    ("New York 12-19", 12, 20), ("Late 20-23", 20, 24)]:
    m2.append(short(label, [x for x in items if a <= x["dt"].hour < b]))
send("\n".join(m2))
print("\n".join(m1))
print("\n".join(m2))
