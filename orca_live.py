#!/usr/bin/env python3
"""
🐋 ORCA 로테이션 피라미딩 라이브 트레이딩 엔진 v3
──────────────────────────────────────────────────
전략:
  평일: 브레이크아웃 피라미딩 (step_pct 돌파 → 레이어 추가)
  주말: 박스 레인지 (고가/저가 레인지 상/하단 매매)

유니버스:
  안정형 TOP 7 (MDD<80%) → 최대 4포지션
  공격형 TOP 3 (MDD 80~95%) → 최대 2포지션

사용법:
  python3 orca_live.py --paper                     # 모의매매
  python3 orca_live.py --paper --evolved --mutant  # 진화 파라미터 + 뮤턴트
  python3 orca_live.py                             # 실매매 (주의!)
"""

import os, sys, json, time, math, hmac, hashlib, base64, random, threading
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

# ── .env 로드 ──────────────────────────────────────────
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

API_KEY          = os.getenv('BITGET_API_KEY', '')
SECRET_KEY       = os.getenv('BITGET_SECRET_KEY', '')
PASSPHRASE       = os.getenv('BITGET_PASSPHRASE', '')
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
BASE_URL         = 'https://api.bitget.com'

# ══════════════════════════════════════════════════════════
# 전체 설정
# ══════════════════════════════════════════════════════════
CONFIG = {
    'product_type':    'USDT-FUTURES',
    'granularity':     '15m',
    'candles_needed':  500,
    'trade_fee':       0.0004,
    'leverage':        3,
    'total_capital':   290,
    'check_interval':  1,
    'paper_mode':      False,
    'hedge_mode':      True,

    # ── 브레이크아웃 피라미딩 ──
    'step_pct':         0.05,
    'atr_period':       14,
    'atr_multiplier':   8.0,
    'trail_pct':        0.03,
    'trail_after':      3,
    'lookback':         96,
    'cooldown_minutes': 60,
    'slippage':         0.001,
    'layer1_sl':        0.05,
    'max_layers':       5,

    # ── 유니버스 ──
    'universe_size':      35,
    'top_stable':         7,
    'top_aggressive':     3,
    'max_stable_pos':     4,
    'max_aggressive_pos': 2,
    'rank_interval':      3600,

    # ── 포지션 사이즈 ──
    'base_size_pct':  0.05,
    'aggro_size_pct': 0.03,
    'min_layer_usdt': 5.0,
    'min_aggro_usdt': 3.0,

    # ── 주말 박스 전략 ──
    'box_enabled':          True,
    'box_days':             [5, 6],
    'box_lookback':         82,
    'box_entry_zone':       0.183,
    'box_tp_ratio':         0.826,
    'box_sl_pct':           0.06,
    'box_max_positions':    1,
    'box_cooldown_minutes': 30,

    # ── 포자 게이트 비활성화 ──
    # 전략 자체가 고MDD/고수익 구조 → 소액 투자 후 날리면 재충전 방식
    'spore_mdd_enter':      9.99,   # 사실상 비활성화
    'spore_mdd_exit':       9.99,
    'cooldown_after_sl':    0,      # 쿨다운 없음
}

# ── 글로벌 상태 ──
positions        = {}
trade_history    = []
universe_candles = {}
top_stable       = set()
top_aggressive   = set()
top_volatile     = set()
last_rank_time   = 0
last_candle_time = 0   # 캔들 갱신 시각 (15분마다)
total_pnl        = 0.0
peak_equity      = CONFIG['total_capital']
max_drawdown     = 0.0
last_hourly      = 0
last_daily       = 0

# ── 뮤턴트 봇 ──
mutant_cfg       = None
mutant_positions = {}
mutant_pnl       = 0.0
mutant_capital   = 10.0

# ── 포자 모드 (Sporulation Gate) ──
spore_mode      = False   # True 이면 신규 진입 전면 차단
sl_cooldown_map = {}      # {symbol: 손절_timestamp} 재진입 쿨다운 추적

# ── WebSocket 가격 ──
ws_prices = {}
ws_lock   = threading.Lock()


# ══════════════════════════════════════════════════════════
# WebSocket
# ══════════════════════════════════════════════════════════

class BitgetWebSocket:
    def __init__(self):
        self.ws         = None
        self.running    = False
        self.subscribed = set()
        self._thread    = None

    def start(self):
        if not HAS_WS:
            return
        self.running = True
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._thread.start()

    def _connect_loop(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    'wss://ws.bitget.com/v2/ws/public',
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f'  ⚠️ WS 에러: {e}')
            if self.running:
                print('  🔌 WS 5초 후 재연결...')
                time.sleep(5)

    def _on_open(self, ws):
        print('  🔌 WebSocket 연결 성공')
        if self.subscribed:
            self._send_subscribe(list(self.subscribed))

    def _on_message(self, ws, msg):
        try:
            d = json.loads(msg)
            if d.get('action') in ('snapshot', 'update'):
                for tick in d.get('data', []):
                    sym = tick.get('instId', '').replace('-USDT-SWAP', 'USDT')
                    if sym and tick.get('last'):
                        with ws_lock:
                            ws_prices[sym] = float(tick['last'])
        except Exception:
            pass

    def _on_error(self, ws, err):
        print(f'  ⚠️ WS 에러: {err}')

    def _on_close(self, ws, code, msg):
        print(f'  🔌 WS 연결 끊김 (code={code})')

    def subscribe(self, symbols):
        new_syms = [s for s in symbols if s not in self.subscribed]
        if not new_syms:
            return
        for s in new_syms:
            self.subscribed.add(s)
        if self.ws and self.ws.sock:
            self._send_subscribe(new_syms)
        print(f'  📡 WS 구독: {len(self.subscribed)}개 코인')

    def _send_subscribe(self, symbols):
        args = [{'instType': 'USDT-FUTURES', 'channel': 'ticker',
                 'instId': s.replace('USDT', '-USDT-SWAP')} for s in symbols]
        try:
            self.ws.send(json.dumps({'op': 'subscribe', 'args': args}))
        except Exception:
            pass

    def get_price(self, symbol):
        with ws_lock:
            return ws_prices.get(symbol)

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


ws_client = BitgetWebSocket()


def get_rt_price(symbol):
    p = ws_client.get_price(symbol)
    return p if p else get_current_price(symbol)


# ══════════════════════════════════════════════════════════
# Bitget REST API
# ══════════════════════════════════════════════════════════

def sign_request(ts, method, path, body=''):
    msg = f'{ts}{method}{path}{body}'
    sig = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()


def bitget_api(method, path, params=None, body=None):
    ts       = str(int(time.time() * 1000))
    qs       = ('?' + '&'.join(f'{k}={v}' for k, v in params.items())) if params else ''
    body_str = json.dumps(body) if body else ''
    sig      = sign_request(ts, method, path + qs, body_str)
    hdrs     = {
        'ACCESS-KEY': API_KEY, 'ACCESS-SIGN': sig,
        'ACCESS-TIMESTAMP': ts, 'ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type': 'application/json', 'locale': 'en-US',
    }
    url = BASE_URL + path + qs
    try:
        r = requests.get(url, headers=hdrs, timeout=10) if method == 'GET' \
            else requests.post(url, headers=hdrs, data=body_str, timeout=10)
        return r.json()
    except Exception as e:
        return {'code': 'ERROR', 'msg': str(e)}


def get_candles(symbol, limit=500):
    gran_map = {'15m': '15m', '1h': '1H', '4h': '4H'}
    gran = gran_map.get(CONFIG['granularity'], '15m')
    r = bitget_api('GET', '/api/v2/mix/market/candles', {
        'productType': CONFIG['product_type'],
        'symbol': symbol, 'granularity': gran, 'limit': str(limit),
    })
    if r.get('code') != '00000':
        return []
    candles = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
               for c in r.get('data', [])]
    candles.sort(key=lambda x: x[0])
    return candles


def get_current_price(symbol):
    r = bitget_api('GET', '/api/v2/mix/market/ticker', {
        'productType': CONFIG['product_type'], 'symbol': symbol})
    try:
        return float(r['data'][0]['lastPr'])
    except Exception:
        return None


def get_top_volume_coins(top_n=35):
    r = bitget_api('GET', '/api/v2/mix/market/tickers', {'productType': CONFIG['product_type']})
    if r.get('code') != '00000':
        return []
    coins = []
    for t in r.get('data', []):
        sym = t.get('symbol', '')
        if not sym.endswith('USDT'):
            continue
        try:
            coins.append({'symbol': sym, 'volume': float(t.get('quoteVolume', 0) or 0),
                          'name': sym.replace('USDT', '')})
        except Exception:
            pass
    coins.sort(key=lambda x: x['volume'], reverse=True)
    return coins[:top_n]


# ══════════════════════════════════════════════════════════
# 텔레그램
# ══════════════════════════════════════════════════════════

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                      json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'},
                      timeout=5)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
# 거래소 포지션
# ══════════════════════════════════════════════════════════

def get_all_orca_positions():
    r = bitget_api('GET', '/api/v2/mix/position/all-position',
                   {'productType': CONFIG['product_type'], 'marginCoin': 'USDT'})
    result = {}
    for p in r.get('data', []):
        if float(p.get('total', 0)) > 0:
            result[p.get('symbol', '')] = p
    return result


def fetch_coin_spec(symbol):
    r = bitget_api('GET', '/api/v2/mix/market/contracts',
                   {'productType': CONFIG['product_type'], 'symbol': symbol})
    for c in r.get('data', []):
        if c.get('symbol') == symbol:
            return c
    return {}


def get_coin_spec(symbol):
    spec = fetch_coin_spec(symbol)
    return {
        'min_size':  float(spec.get('minTradeNum', 1)),
        'size_step': float(spec.get('sizeMultiplier', 1)),
        'price_prec':int(spec.get('pricePlace', 4)),
        'vol_prec':  int(spec.get('volumePlace', 0)),
    }


def calculate_coin_size(symbol, usdt_amount, price, leverage):
    spec     = get_coin_spec(symbol)
    raw_size = usdt_amount * leverage / price
    step     = spec['size_step']
    size     = math.floor(raw_size / step) * step
    return max(round(size, spec['vol_prec']), spec['min_size'])


def round_price(price, symbol):
    return round(price, get_coin_spec(symbol)['price_prec'])


# ══════════════════════════════════════════════════════════
# 지표
# ══════════════════════════════════════════════════════════

def calc_atr_pct(candles, end_idx, period=14):
    if end_idx < period + 1:
        return 0.0
    trs = [max(candles[i][2]-candles[i][3],
               abs(candles[i][2]-candles[i-1][4]),
               abs(candles[i][3]-candles[i-1][4]))
           for i in range(end_idx - period, end_idx)]
    atr   = sum(trs) / period
    close = candles[end_idx][4]
    return atr / close if close > 0 else 0.0


def calc_atr_target(candles, end_idx, period=14, multiplier=8.0):
    if end_idx < period + 1:
        return 0.0
    trs = [max(candles[i][2]-candles[i][3],
               abs(candles[i][2]-candles[i-1][4]),
               abs(candles[i][3]-candles[i-1][4]))
           for i in range(end_idx - period, end_idx)]
    return (sum(trs) / period) * multiplier


# ══════════════════════════════════════════════════════════
# 유니버스 갱신
# ══════════════════════════════════════════════════════════

def get_universe_ranking():
    global universe_candles
    top_n  = CONFIG['universe_size']
    coins  = get_top_volume_coins(top_n)
    if not coins:
        print('  ❌ 유니버스 코인 조회 실패')
        return set(), set(), {}

    print(f'\n  📊 유니버스 갱신: 거래대금 TOP {top_n} → ATR 순위 계산...')

    lk = CONFIG.get('lookback', 96)
    ap = CONFIG.get('atr_period', 14)
    scored = []
    new_candles = {}

    for coin in coins:
        sym = coin['symbol']
        try:
            c = get_candles(sym, CONFIG['candles_needed'])
            time.sleep(0.08)
        except Exception:
            continue
        if len(c) < lk + ap + 10:
            continue

        atr_pct = calc_atr_pct(c, len(c)-1, ap)

        # MDD 계산
        window = c[-(lk*2):]
        peak   = window[0][2]
        mdd    = 0.0
        for bar in window:
            if bar[2] > peak:
                peak = bar[2]
            dd   = (peak - bar[3]) / peak if peak > 0 else 0
            mdd  = max(mdd, dd)

        new_candles[sym] = c
        scored.append({'symbol': sym, 'name': coin['name'], 'atr_pct': atr_pct, 'mdd': mdd})

    if not scored:
        return set(), set(), {}

    scored.sort(key=lambda x: x['atr_pct'], reverse=True)

    stable_list = []
    aggro_list  = []
    MDD_CAP     = 0.95
    MDD_THRESH  = 0.80

    for coin in scored:
        mdd = coin['mdd']
        if mdd >= MDD_CAP:
            print(f"    💀 {coin['name']} 제외 — MDD {mdd*100:.0f}% (휴지조각)")
        elif mdd >= MDD_THRESH:
            aggro_list.append(coin)
        else:
            stable_list.append(coin)

    top_s = CONFIG['top_stable']
    top_a = CONFIG['top_aggressive']
    stable_set = set(c['symbol'] for c in stable_list[:top_s])
    aggro_set  = set(c['symbol'] for c in aggro_list[:top_a])

    print(f'  🛡️ 안정형 TOP {top_s} (MDD<80%):')
    for i, c in enumerate(stable_list[:top_s]):
        print(f'     {i+1:2d}. {c["name"]:>8} | ATR {c["atr_pct"]*100:.2f}%')

    if aggro_list[:top_a]:
        print(f'  ⚡ 공격형 TOP {top_a} (MDD≥80%):')
        for i, c in enumerate(aggro_list[:top_a]):
            print(f'     {i+1:2d}. {c["name"]:>8} | ATR {c["atr_pct"]*100:.2f}% | MDD {c["mdd"]*100:.0f}%')
    else:
        print('  ⚡ 공격형: 해당 코인 없음')

    universe_candles = {sym: new_candles[sym]
                        for sym in (stable_set | aggro_set) if sym in new_candles}
    return stable_set, aggro_set, universe_candles


# ══════════════════════════════════════════════════════════
# 주말 체크
# ══════════════════════════════════════════════════════════

def is_weekend():
    return datetime.now(timezone.utc).weekday() in CONFIG.get('box_days', [5, 6])


# ══════════════════════════════════════════════════════════
# 박스 전략: 진입
# ══════════════════════════════════════════════════════════

def check_box_entry_signal(symbol, candles, cfg):
    """
    박스 매매 진입 신호 (주말용)
    - 최근 box_lookback 봉의 고가/저가 = 박스 범위
    - 현재가가 하위 box_entry_zone → LONG (바닥 매수)
    - 현재가가 상위 box_entry_zone → SHORT (천장 매도)
    """
    lookback = cfg.get('box_lookback', 82)
    zone     = cfg.get('box_entry_zone', 0.183)
    tp_ratio = cfg.get('box_tp_ratio', 0.826)

    if not candles or len(candles) < lookback + 10:
        return {'side': None}

    idx      = len(candles) - 1
    close    = candles[idx][4]
    window   = candles[idx - lookback: idx]
    box_high = max(c[2] for c in window)
    box_low  = min(c[3] for c in window)
    box_rng  = box_high - box_low

    if box_rng < box_high * 0.005:
        return {'side': None}

    lower_zone = box_low  + box_rng * zone
    upper_zone = box_high - box_rng * zone

    if close <= lower_zone:
        return {'side': 'long',  'box_high': box_high, 'box_low': box_low,
                'box_tp': box_low + box_rng * tp_ratio}
    elif close >= upper_zone:
        return {'side': 'short', 'box_high': box_high, 'box_low': box_low,
                'box_tp': box_high - box_rng * tp_ratio}
    return {'side': None}


# ══════════════════════════════════════════════════════════
# 박스 전략: 청산
# ══════════════════════════════════════════════════════════

def check_box_exit_conditions(pos, current_price, cfg):
    """
    박스 청산 (3단계)
    A) BOX_SL: 박스 이탈 or 고정 SL%
    B) BOX_TP: 목표가 도달
    """
    side     = pos.get('side')
    slip     = pos.get('slippage', 0.001)
    box_high = pos.get('box_high', 0)
    box_low  = pos.get('box_low', 0)
    box_tp   = pos.get('box_tp', 0)
    sl_pct   = cfg.get('box_sl_pct', 0.06)

    c = current_price * (1 - slip) if side == 'long' else current_price * (1 + slip)

    first_entry = pos.get('first_entry') or (pos.get('layers', [{}])[0].get('entry', current_price))
    if side == 'long':
        sl_price = first_entry * (1 - sl_pct)
        if c <= sl_price or c <= box_low * 0.995:
            return {'action': 'close', 'reason': 'BOX_SL', 'exit_price': c}
        if c >= box_tp:
            return {'action': 'close', 'reason': 'BOX_TP', 'exit_price': c}
    else:
        sl_price = first_entry * (1 + sl_pct)
        if c >= sl_price or c >= box_high * 1.005:
            return {'action': 'close', 'reason': 'BOX_SL', 'exit_price': c}
        if c <= box_tp:
            return {'action': 'close', 'reason': 'BOX_TP', 'exit_price': c}

    return {'action': 'hold', 'reason': ''}


# ══════════════════════════════════════════════════════════
# 브레이크아웃: 진입 신호
# ══════════════════════════════════════════════════════════

def check_entry_signal(symbol, candles, cfg):
    """
    진입 신호 체크
    - Long: 최근 lookback 봉 저점 대비 현재가 step_pct 이상 상승
    - Short: 최근 lookback 봉 고점 대비 현재가 step_pct 이상 하락
    """
    step     = cfg.get('step_pct', 0.05)
    lookback = cfg.get('lookback', 96)

    if len(candles) < lookback + 2:
        return {'side': None}

    # 일봉 음봉이면 진입 안 함 (15분봉 96개 = 24시간)
    daily_open  = candles[max(0, len(candles) - 96)][1]
    daily_close = candles[-1][4]
    if daily_close < daily_open:
        return {'side': None}

    idx    = len(candles) - 1
    close  = candles[idx][4]
    window = candles[idx - lookback: idx]
    lo_p   = min(c[3] for c in window)
    hi_p   = max(c[2] for c in window)

    lg = (close - lo_p) / lo_p if lo_p > 0 else 0
    sg = (hi_p - close) / hi_p if hi_p > 0 else 0

    if lg >= step and sg >= step:
        side = 'long' if lg > sg else 'short'
    elif lg >= step:
        side = 'long'
    elif sg >= step:
        side = 'short'
    else:
        return {'side': None}

    base_price    = lo_p if side == 'long' else hi_p
    current_level = round(lg / step) if side == 'long' else round(sg / step)
    return {'side': side, 'base_price': base_price, 'current_level': current_level}


# ══════════════════════════════════════════════════════════
# 브레이크아웃: 추가 레이어
# ══════════════════════════════════════════════════════════

def check_add_layer(pos, current_price, cfg):
    step     = cfg.get('step_pct', 0.05)
    cd_min   = cfg.get('cooldown_minutes', 60)
    max_lay  = cfg.get('max_layers', 5)
    last_add = pos.get('last_add_time', 0)

    if time.time() - last_add < cd_min * 60:
        return False
    if len(pos.get('layers', [])) >= max_lay:
        return False

    side       = pos.get('side')
    base_price = pos.get('base_price', 0)
    next_level = pos.get('next_level', 2)
    layers     = pos.get('layers', [])
    if not layers:
        return False

    avg = sum(l['entry']*l['size_usdt'] for l in layers) / sum(l['size_usdt'] for l in layers)

    if side == 'long':
        target = base_price * (1 + step * next_level)
        return current_price > target and current_price > avg * 1.005
    else:
        target = base_price * (1 - step * next_level)
        return current_price < target and current_price < avg * 0.995


# ══════════════════════════════════════════════════════════
# 브레이크아웃: 청산 조건
# ══════════════════════════════════════════════════════════

def check_exit_conditions(pos, current_price, cfg):
    """
    4단계 청산 체크
    A) SL1: 1차 레이어 -layer1_sl%
    B) BE_AVG: 2+ 레이어, 현재가 평단 이하
    C) TRAIL: trail_after 이상 레이어, 고점 대비 trail_pct% 하락
    D) TARGET: ATR×multiplier 도달
    """
    layers    = pos.get('layers', [])
    side      = pos.get('side')
    slip      = pos.get('slippage', 0.001)
    sl1_pct   = cfg.get('layer1_sl', 0.05)
    trail_pct = cfg.get('trail_pct', 0.03)
    trail_aft = cfg.get('trail_after', 3)

    if not layers:
        return {'action': 'hold', 'reason': ''}

    c           = current_price * (1 - slip) if side == 'long' else current_price * (1 + slip)
    first_entry = layers[0]['entry']
    avg_entry   = sum(l['entry']*l['size_usdt'] for l in layers) / sum(l['size_usdt'] for l in layers)

    # A) SL1
    if side == 'long'  and c <= first_entry * (1 - sl1_pct):
        return {'action': 'close', 'reason': 'SL1',    'exit_price': c}
    if side == 'short' and c >= first_entry * (1 + sl1_pct):
        return {'action': 'close', 'reason': 'SL1',    'exit_price': c}

    # B) BE_AVG
    if len(layers) >= 2:
        if side == 'long'  and c <= avg_entry:
            return {'action': 'close', 'reason': 'BE_AVG', 'exit_price': c}
        if side == 'short' and c >= avg_entry:
            return {'action': 'close', 'reason': 'BE_AVG', 'exit_price': c}

    # C) TRAIL
    if len(layers) >= trail_aft:
        peak = pos.get('peak_price', c)
        if side == 'long':
            pos['peak_price'] = max(peak, c)
            if c <= pos['peak_price'] * (1 - trail_pct):
                return {'action': 'close', 'reason': 'TRAIL', 'exit_price': c}
        else:
            pos['peak_price'] = min(peak, c)
            if c >= pos['peak_price'] * (1 + trail_pct):
                return {'action': 'close', 'reason': 'TRAIL', 'exit_price': c}

    # D) TARGET
    dynamic_target = pos.get('dynamic_target', 0)
    if dynamic_target > 0:
        if side == 'long'  and c >= first_entry + dynamic_target:
            return {'action': 'close', 'reason': 'TARGET', 'exit_price': c}
        if side == 'short' and c <= first_entry - dynamic_target:
            return {'action': 'close', 'reason': 'TARGET', 'exit_price': c}

    return {'action': 'hold', 'reason': ''}


# ══════════════════════════════════════════════════════════
# PnL 계산
# ══════════════════════════════════════════════════════════

def calc_position_pnl(pos, exit_price):
    side = pos.get('side')
    lev  = CONFIG['leverage']
    fee  = CONFIG['trade_fee']
    pnl  = 0.0
    for layer in pos.get('layers', []):
        entry = layer['entry']
        size  = layer['size_usdt']
        if side == 'long':
            pnl += size * lev * (exit_price - entry) / entry
        else:
            pnl += size * lev * (entry - exit_price) / entry
        pnl -= size * fee * 2
    return pnl


# ══════════════════════════════════════════════════════════
# 거래소 주문
# ══════════════════════════════════════════════════════════

def open_pyramid_layer(symbol, side, size_usdt, is_first_layer=False, sl_price=None):
    if CONFIG['paper_mode']:
        price = get_rt_price(symbol) or 0
        return {'price': price, 'order_id': f'PAPER_{int(time.time())}'}

    price = get_rt_price(symbol)
    if not price:
        return None

    size       = calculate_coin_size(symbol, size_usdt, price, CONFIG['leverage'])
    order_side = 'buy' if side == 'long' else 'sell'
    body = {
        'symbol': symbol, 'productType': CONFIG['product_type'],
        'marginMode': 'crossed', 'marginCoin': 'USDT',
        'size': str(size), 'side': order_side, 'tradeSide': 'open',
        'orderType': 'market', 'leverage': str(CONFIG['leverage']),
    }
    if is_first_layer and sl_price:
        body['presetStopLossPrice'] = str(round_price(sl_price, symbol))

    r = bitget_api('POST', '/api/v2/mix/order/place-order', body=body)
    if r.get('code') == '00000':
        return {'price': price, 'order_id': r['data']['orderId']}
    print(f'  ❌ 주문 실패: {r}')
    return None


def close_all_layers(symbol, side):
    if CONFIG['paper_mode']:
        return get_rt_price(symbol) or 0

    close_side = 'sell' if side == 'long' else 'buy'
    body = {
        'symbol': symbol, 'productType': CONFIG['product_type'],
        'marginCoin': 'USDT', 'side': close_side,
        'tradeSide': 'close', 'orderType': 'market', 'size': '0',
    }
    bitget_api('POST', '/api/v2/mix/order/place-order', body=body)
    return get_rt_price(symbol) or 0


# ══════════════════════════════════════════════════════════
# 보고
# ══════════════════════════════════════════════════════════

def send_hourly_report():
    now     = datetime.now()
    eq      = CONFIG['total_capital'] + total_pnl
    ur_pnl  = sum(calc_position_pnl(pos, get_rt_price(sym) or pos['first_entry'])
                  for sym, pos in positions.items())
    nt      = len(trade_history)
    wins    = sum(1 for t in trade_history if t.get('pnl', 0) > 0)
    wr      = wins/nt*100 if nt > 0 else 0
    mode_str= '📦 박스 모드' if is_weekend() and CONFIG.get('box_enabled') else '🔥 돌파 모드'
    send_telegram(
        f'📊 {now.strftime("%H:%M")} 시간 보고\n{mode_str}\n'
        f'자본: ${eq:.1f} | PnL: ${total_pnl:+.2f}\n'
        f'MDD: {max_drawdown*100:.1f}% | 포지션: {len(positions)}개\n'
        f'미실현: ${ur_pnl:+.2f}\n거래: {nt}건 | 승률: {wr:.0f}%')


def send_daily_report():
    nt   = len(trade_history)
    wins = sum(1 for t in trade_history if t.get('pnl', 0) > 0)
    send_telegram(
        f'📅 일일 보고\n총 PnL: ${total_pnl:+.2f}\n'
        f'MDD: {max_drawdown*100:.1f}%\n'
        f'거래: {nt}건 | 승률: {wins/nt*100:.0f}%' if nt > 0 else '거래 없음')


# ══════════════════════════════════════════════════════════
# 상태 저장 / 복원
# ══════════════════════════════════════════════════════════

def save_status_json():
    try:
        eq   = CONFIG['total_capital'] + total_pnl
        path = os.path.join(os.path.dirname(__file__), 'orca_status.json')
        with open(path, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'mode': 'PAPER' if CONFIG['paper_mode'] else 'LIVE',
                'equity': round(eq, 2), 'total_pnl': round(total_pnl, 2),
                'max_drawdown': round(max_drawdown*100, 2),
                'positions': len(positions), 'trades': len(trade_history),
                'top_stable': list(top_stable), 'top_aggressive': list(top_aggressive),
                'weekend_mode': is_weekend() and CONFIG.get('box_enabled', False),
            }, f, indent=2)
    except Exception:
        pass


def save_positions_state():
    try:
        data = [{'symbol': sym, 'side': pos.get('side'), 'mode': pos.get('mode','breakout'),
                 'tier': pos.get('tier','stable'), 'layers': pos.get('layers',[]),
                 'base_price': pos.get('base_price',0), 'next_level': pos.get('next_level',2),
                 'peak_price': pos.get('peak_price',0), 'first_entry': pos.get('first_entry',0),
                 'dynamic_target': pos.get('dynamic_target',0),
                 'box_high': pos.get('box_high',0), 'box_low': pos.get('box_low',0),
                 'box_tp': pos.get('box_tp',0), 'last_add_time': pos.get('last_add_time',0),
                 'open_time': pos.get('open_time',0), 'slippage': pos.get('slippage',0.001)}
                for sym, pos in positions.items()]
        path = os.path.join(os.path.dirname(__file__), 'orca_positions.json')
        with open(path, 'w') as f:
            json.dump({'positions': data, 'total_pnl': total_pnl}, f, indent=2)
    except Exception:
        pass


def load_positions_state():
    global positions, total_pnl
    path = os.path.join(os.path.dirname(__file__), 'orca_positions.json')
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f)
        total_pnl = data.get('total_pnl', 0.0)
        for p in data.get('positions', []):
            positions[p['symbol']] = p
        print(f'  📂 포지션 복원: {len(positions)}개 포지션, PnL: ${total_pnl:+.2f}')
    except Exception as e:
        print(f'  ⚠️ 포지션 복원 실패: {e}')


def sync_with_exchange():
    if CONFIG['paper_mode']:
        return
    ex_positions = get_all_orca_positions()
    for sym in list(positions.keys()):
        if sym not in ex_positions:
            print(f'  ⚠️ 고아 포지션 정리: {sym}')
            del positions[sym]


# ══════════════════════════════════════════════════════════
# 진화 파라미터 로드
# ══════════════════════════════════════════════════════════

def load_evolved_params():
    # 1) 브레이크아웃 파라미터
    rot_paths = [
        os.path.join(os.path.dirname(__file__), '..', 'data', 'evolved_rotation_params.json'),
        os.path.join(os.path.dirname(__file__), 'data', 'evolved_rotation_params.json'),
        'data/evolved_rotation_params.json',
    ]
    loaded = False
    for path in rot_paths:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            params  = data.get('best_params', data.get('params', {}))
            applied = []
            for key, val in params.items():
                if key in CONFIG:
                    CONFIG[key] = val
                    applied.append(f'{key}={val}')
            # cooldown (봉 단위) → cooldown_minutes (분 단위) 변환
            if 'cooldown' in params and 'cooldown_minutes' in CONFIG:
                CONFIG['cooldown_minutes'] = int(params['cooldown']) * 15
                applied.append(f'cooldown_minutes={CONFIG["cooldown_minutes"]}')
            # rank_period (봉 단위) → rank_interval (초 단위) 변환
            if 'rank_period' in params and 'rank_interval' in CONFIG:
                CONFIG['rank_interval'] = int(params['rank_period']) * 15 * 60
                applied.append(f'rank_interval={CONFIG["rank_interval"]}')
            print(f'  🧬 브레이크아웃 파라미터 로드 ({data.get("generated_at","?")})')
            print(f'     적용: {", ".join(applied[:8])}')
            loaded = True
            break
        except Exception as e:
            print(f'  ⚠️ 브레이크아웃 파라미터 로드 실패: {e}')

    # 2) 박스 전용 파라미터 (있으면 덮어씀)
    box_paths = [
        os.path.join(os.path.dirname(__file__), '..', 'data', 'evolved_box_params.json'),
        os.path.join(os.path.dirname(__file__), 'data', 'evolved_box_params.json'),
        'data/evolved_box_params.json',
    ]
    for path in box_paths:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            params  = data.get('best_params', {})
            applied = []
            for key, val in params.items():
                if key in CONFIG:
                    CONFIG[key] = val
                    applied.append(f'{key}={val}')
            print(f'  📦 박스 파라미터 로드 ({data.get("generated_at","?")})')
            print(f'     적용: {", ".join(applied)}')
            break
        except Exception as e:
            print(f'  ⚠️ 박스 파라미터 로드 실패: {e}')

    return loaded


def init_mutant():
    global mutant_cfg
    base   = {k: CONFIG[k] for k in
              ['step_pct','atr_multiplier','trail_pct','trail_after',
               'lookback','layer1_sl','atr_period','max_stable_pos']}
    mutant = {}
    for k, v in base.items():
        if isinstance(v, float):
            mutant[k] = round(v * random.uniform(0.7, 1.3), 4)
        elif isinstance(v, int):
            mutant[k] = max(1, int(v * random.uniform(0.7, 1.3)))
        else:
            mutant[k] = v
    mutant_cfg = mutant
    print(f'  🧬 돌연변이 봇 초기화 (자본: ${mutant_capital}, ±30%)')
    print(f'     step_pct={mutant["step_pct"]:.4f}, atr_multiplier={mutant["atr_multiplier"]:.2f}, '
          f'trail_pct={mutant["trail_pct"]:.5f}, trail_after={mutant["trail_after"]}, '
          f'lookback={mutant["lookback"]}, layer1_sl={mutant["layer1_sl"]:.5f}, '
          f'atr_period={mutant["atr_period"]}, max_positions={mutant["max_stable_pos"]}')


# ══════════════════════════════════════════════════════════
# 메인 실행 루프
# ══════════════════════════════════════════════════════════

def run():
    global positions, trade_history, universe_candles
    global top_stable, top_aggressive, top_volatile, last_rank_time, last_candle_time
    global total_pnl, peak_equity, max_drawdown
    global last_hourly, last_daily
    global mutant_positions, mutant_pnl
    global spore_mode, sl_cooldown_map

    use_evolved = '--evolved' in sys.argv
    use_mutant  = '--mutant'  in sys.argv

    if use_evolved:
        load_evolved_params()
    if use_mutant:
        init_mutant()

    mode_str = '📝 모의매매' if CONFIG['paper_mode'] else '🔥 실매매 (LIVE!)'

    print('=' * 65)
    print('🐋 ORCA 로테이션 피라미딩 엔진 v3')
    print('=' * 65)
    print(f'\n  모드: {mode_str}')
    print(f'  총 자금: ${CONFIG["total_capital"]}')
    print(f'  레버리지: {CONFIG["leverage"]}x')
    print(f'  유니버스: 거래대금 TOP {CONFIG["universe_size"]}')
    print(f'  안정형 풀: TOP {CONFIG["top_stable"]} (max {CONFIG["max_stable_pos"]}포지션)')
    print(f'  공격형 풀: TOP {CONFIG["top_aggressive"]} (max {CONFIG["max_aggressive_pos"]}포지션)')
    print(f'  전략: {CONFIG["step_pct"]*100:.0f}% 돌파 진입, 피라미딩, 본절/트레일링/ATR×{CONFIG["atr_multiplier"]:.0f} 청산')
    print(f'  가격: WebSocket 실시간 틱 (REST 폴백)')

    print('  🔌 WebSocket 연결 중...')
    if HAS_WS:
        ws_client.start()

    load_positions_state()
    if not positions:
        print('  📋 새 세션 시작 (이전 포지션 없음)')

    if not CONFIG['paper_mode']:
        sync_with_exchange()

    send_telegram(
        f'🐋 ORCA 피라미딩 v3 시작!\n모드: {mode_str}\n'
        f'자금: ${CONFIG["total_capital"]} x{CONFIG["leverage"]}\n'
        f'유니버스: TOP {CONFIG["universe_size"]} → 안정 {CONFIG["top_stable"]} + 공격 {CONFIG["top_aggressive"]}\n'
        f'포지션: {len(positions)}개 보유 중')

    last_weekend_state = None

    while True:
        try:
            now = datetime.now()

            # ── 유니버스 갱신 ──
            if time.time() - last_rank_time >= CONFIG['rank_interval']:
                print(f'\n{"─"*50}')
                print(f'  ⏰ [{now.strftime("%H:%M")}] 유니버스 순위 갱신 중...')
                try:
                    top_stable, top_aggressive, universe_candles = get_universe_ranking()
                    top_volatile = top_stable | top_aggressive
                    last_rank_time = time.time()
                    if HAS_WS and top_volatile:
                        ws_client.subscribe(list(top_volatile))
                    print(f'  📊 캔들 갱신: {len(universe_candles)}개 코인')
                    send_telegram(
                        f'🔄 유니버스 갱신\n'
                        f'🛡️ 안정({CONFIG["top_stable"]}): {", ".join(s.replace("USDT","") for s in list(top_stable)[:5])}\n'
                        f'⚡ 공격({CONFIG["top_aggressive"]}): {", ".join(s.replace("USDT","") for s in list(top_aggressive)[:3])}')
                except Exception as e:
                    print(f'  ⚠️ 순위 갱신 실패, 이전 순위 유지: {e}')

            # ── 주말 ↔ 평일 전환 ──
            current_weekend = CONFIG.get('box_enabled', False) and is_weekend()
            if current_weekend != last_weekend_state and last_weekend_state is not None:
                if current_weekend:
                    print('\n  📦 주말 박스 매매 모드 ON')
                    send_telegram('📦 주말 박스 매매 모드 ON\n돌파 신규 진입 중단, 박스 매매 전환')
                else:
                    print('\n  🔥 평일 돌파 모드 복귀')
                    send_telegram('🔥 평일 돌파 모드 복귀\n박스 신규 진입 중단, 돌파 전략 전환')
            last_weekend_state = current_weekend

            if not universe_candles:
                print('  ⏳ 유니버스 캔들 대기 중...')
                time.sleep(CONFIG['check_interval'])
                continue

            # ── Phase 1: 캔들 업데이트 (15분마다) ──
            if time.time() - last_candle_time >= 900:
                for sym in list(top_volatile):
                    try:
                        c = get_candles(sym, CONFIG['candles_needed'])
                        if c:
                            universe_candles[sym] = c
                        time.sleep(0.05)
                    except Exception:
                        pass
                last_candle_time = time.time()

            # ── Phase 2: 포지션 관리 ──
            for sym in list(positions.keys()):
                pos     = positions[sym]
                candles = universe_candles.get(sym)
                if not candles:
                    # 유니버스 탈락 심볼: REST로 캔들 직접 fetch
                    try:
                        fetched = get_candles(sym, 50)
                        if fetched:
                            candles = fetched
                        else:
                            continue
                    except Exception:
                        continue

                price = get_rt_price(sym) or candles[-1][4]
                name  = sym.replace('USDT', '')
                mode  = pos.get('mode', 'breakout')

                # 박스 포지션
                if mode == 'box':
                    result = check_box_exit_conditions(pos, price, CONFIG)
                    if result['action'] == 'close':
                        exit_price = close_all_layers(sym, pos['side'])
                        pnl        = calc_position_pnl(pos, exit_price)
                        total_pnl += pnl
                        trade_history.append({'time': now.isoformat(), 'symbol': sym, 'pnl': round(pnl, 2)})
                        reason = result['reason']
                        emoji  = {'BOX_SL':'🔴','BOX_TP':'🎯','BOX_EXPIRE':'⏰'}.get(reason,'❌')
                        n_lay  = len(pos.get('layers',[]))
                        print(f'\n  {emoji} [{name}] 박스 {pos["side"].upper()} 청산 | {n_lay}L | PnL: ${pnl:+.2f}')
                        send_telegram(f'{emoji} {name} 박스 청산\n{n_lay}레이어\nPnL: ${pnl:+.2f}\n누적: ${total_pnl:+.2f}')
                        del positions[sym]
                        continue

                # 브레이크아웃 포지션
                else:
                    result = check_exit_conditions(pos, price, CONFIG)
                    if result['action'] == 'close':
                        exit_price = close_all_layers(sym, pos['side'])
                        pnl        = calc_position_pnl(pos, exit_price)
                        total_pnl += pnl
                        trade_history.append({'time': now.isoformat(), 'symbol': sym, 'pnl': round(pnl, 2)})
                        reason = result['reason']
                        emoji  = {'SL1':'🔴','BE_AVG':'⚪','TRAIL':'🟡','TARGET':'🎯'}.get(reason,'❌')
                        n_lay  = len(pos.get('layers',[]))
                        print(f'\n  {emoji} [{name}] {pos["side"].upper()} 청산 | {n_lay}L | PnL: ${pnl:+.2f}')
                        send_telegram(f'{emoji} {name} 청산\n{n_lay}레이어\nPnL: ${pnl:+.2f}\n누적: ${total_pnl:+.2f}')
                        # 손절이면 쿨다운 기록
                        if reason == 'SL1':
                            sl_cooldown_map[sym] = time.time()
                        del positions[sym]
                        continue

                    # 추가 레이어
                    if check_add_layer(pos, price, CONFIG):
                        is_aggro   = pos.get('tier') == 'aggressive'
                        sz_pct     = CONFIG['aggro_size_pct'] if is_aggro else CONFIG['base_size_pct']
                        sz_min     = CONFIG['min_aggro_usdt']  if is_aggro else CONFIG['min_layer_usdt']
                        avail      = CONFIG['total_capital'] + total_pnl
                        layer_usdt = max(avail * sz_pct, sz_min)
                        result     = open_pyramid_layer(sym, pos['side'], layer_usdt)
                        if result:
                            pos['layers'].append({'entry': result['price'], 'size_usdt': layer_usdt,
                                                   'level': pos['next_level'], 'order_id': result.get('order_id','')})
                            pos['next_level']   += 1
                            pos['last_add_time'] = time.time()
                            avg = sum(l['entry']*l['size_usdt'] for l in pos['layers']) / sum(l['size_usdt'] for l in pos['layers'])
                            print(f'  📈 [{name}] 추가 진입 레이어{len(pos["layers"])} @ ${result["price"]:,.4f} | 평단 ${avg:,.4f}')
                            send_telegram(f'📈 {name} 추가 진입\n레이어 {len(pos["layers"])}\n평단: ${avg:,.2f}\n총 ${sum(l["size_usdt"] for l in pos["layers"]):.1f}')

            # ── MDD 추적 ──
            eq = CONFIG['total_capital'] + total_pnl
            if eq > peak_equity:
                peak_equity = eq
            cur_dd = (peak_equity - eq) / peak_equity if peak_equity > 0 else 0.0
            if cur_dd > max_drawdown:
                max_drawdown = cur_dd

            # ── 포자 게이트 (Sporulation Gate) ──
            spore_enter = CONFIG.get('spore_mdd_enter', 0.15)
            spore_exit  = CONFIG.get('spore_mdd_exit',  0.08)
            if not spore_mode and cur_dd >= spore_enter:
                spore_mode = True
                msg = f'🍄 포자 모드 진입! MDD {cur_dd*100:.1f}% ≥ {spore_enter*100:.0f}%\n신규 진입 전면 차단'
                print(f'\n  {msg}')
                send_telegram(msg)
            elif spore_mode and cur_dd <= spore_exit:
                spore_mode = False
                msg = f'🌱 포자 모드 해제. MDD {cur_dd*100:.1f}% ≤ {spore_exit*100:.0f}%\n신규 진입 재개'
                print(f'\n  {msg}')
                send_telegram(msg)

            # ── Phase 3: 신규 진입 ──
            stable_count = sum(1 for p in positions.values()
                               if p.get('tier','stable') == 'stable' and p.get('mode') != 'box')
            aggro_count  = sum(1 for p in positions.values() if p.get('tier') == 'aggressive')
            box_count    = sum(1 for p in positions.values() if p.get('mode') == 'box')
            avail        = CONFIG['total_capital'] + total_pnl

            # 포자 모드면 신규 진입 전면 차단
            if spore_mode:
                time.sleep(CONFIG['check_interval'])
                continue

            # 주말 박스
            if current_weekend:
                box_max = CONFIG.get('box_max_positions', 1)
                if box_count < box_max:
                    for sym in sorted(top_volatile):
                        if sym in positions or box_count >= box_max:
                            continue
                        candles = universe_candles.get(sym)
                        if not candles:
                            continue
                        signal = check_box_entry_signal(sym, candles, CONFIG)
                        if not signal.get('side'):
                            continue

                        # 쿨다운 체크
                        last_t = max((t.get('time','') for t in trade_history if t.get('symbol') == sym), default='')
                        if last_t:
                            elapsed = (now - datetime.fromisoformat(last_t)).total_seconds() / 60
                            if elapsed < CONFIG.get('box_cooldown_minutes', 30):
                                continue

                        name = sym.replace('USDT','')
                        side = signal['side']
                        sz   = max(avail * CONFIG['base_size_pct'], CONFIG['min_layer_usdt'])
                        result = open_pyramid_layer(sym, side, sz, is_first_layer=True)
                        if not result:
                            continue

                        price = result['price']
                        positions[sym] = {
                            'symbol': sym, 'side': side, 'mode': 'box', 'tier': 'stable',
                            'layers': [{'entry': price, 'size_usdt': sz, 'level': 1, 'order_id': result.get('order_id','')}],
                            'first_entry': price, 'peak_price': price, 'dynamic_target': 0,
                            'box_high': signal['box_high'], 'box_low': signal['box_low'], 'box_tp': signal['box_tp'],
                            'last_add_time': time.time(), 'open_time': time.time(), 'slippage': CONFIG['slippage'],
                        }
                        box_rng_pct = (signal['box_high'] - signal['box_low']) / signal['box_high'] * 100
                        print(f'\n  📦 [{name}] 박스 {side.upper()} | 범위 {box_rng_pct:.1f}%')
                        print(f'     박스: ${signal["box_low"]:,.2f} ~ ${signal["box_high"]:,.2f} | TP: ${signal["box_tp"]:,.2f}')
                        send_telegram(
                            f'📦 {name} 박스 {side.upper()}\n@ ${price:,.4f}\n'
                            f'박스: ${signal["box_low"]:,.2f} ~ ${signal["box_high"]:,.2f}\nTP: ${signal["box_tp"]:,.2f}\n'
                            f'SL: 박스이탈 -{CONFIG["box_sl_pct"]*100:.0f}%')
                        box_count += 1

            # 평일 브레이크아웃
            else:
                # 미장 시간 필터: NYSE 12:30~20:00 UTC (서머타임 기준)
                utc_hm = now.hour * 60 + now.minute
                NY_OPEN  = CONFIG.get('ny_open_utc',  12 * 60 + 30)
                NY_CLOSE = CONFIG.get('ny_close_utc', 20 * 60)
                in_ny = NY_OPEN <= utc_hm < NY_CLOSE
                entry_pools = [
                    (sorted(top_stable),     CONFIG['max_stable_pos'],    stable_count, '🛡️', CONFIG['base_size_pct'], CONFIG['min_layer_usdt'],  'stable'),
                    (sorted(top_aggressive), CONFIG['max_aggressive_pos'], aggro_count,  '⚡', CONFIG['aggro_size_pct'],CONFIG['min_aggro_usdt'],   'aggressive'),
                ] if in_ny else []
                for pool_syms, pool_max, pool_count, emoji, sz_pct, sz_min, tier in entry_pools:
                    if pool_count >= pool_max:
                        continue
                    for sym in pool_syms:
                        if sym in positions or pool_count >= pool_max:
                            continue
                        candles = universe_candles.get(sym)
                        if not candles:
                            continue
                        # 손절 후 재진입 쿨다운 체크
                        cd_min = CONFIG.get('cooldown_after_sl', 120)
                        if sym in sl_cooldown_map and time.time() - sl_cooldown_map[sym] < cd_min * 60:
                            continue
                        signal = check_entry_signal(sym, candles, CONFIG)
                        if not signal.get('side'):
                            continue

                        name       = sym.replace('USDT','')
                        side       = signal['side']
                        layer_usdt = max(avail * sz_pct, sz_min)
                        atr_target = calc_atr_target(candles, len(candles)-1, CONFIG['atr_period'], CONFIG['atr_multiplier'])
                        price_now  = get_rt_price(sym) or candles[-1][4]
                        sl_price   = price_now * (1 - CONFIG['layer1_sl']) if side == 'long' \
                                     else price_now * (1 + CONFIG['layer1_sl'])

                        result = open_pyramid_layer(sym, side, layer_usdt, is_first_layer=True, sl_price=sl_price)
                        if not result:
                            continue

                        price = result['price']
                        positions[sym] = {
                            'symbol': sym, 'side': side, 'mode': 'breakout', 'tier': tier,
                            'layers': [{'entry': price, 'size_usdt': layer_usdt, 'level': 1, 'order_id': result.get('order_id','')}],
                            'base_price': signal['base_price'], 'next_level': 2,
                            'current_level': signal['current_level'],
                            'first_entry': price, 'peak_price': price,
                            'dynamic_target': atr_target,
                            'last_add_time': time.time(), 'open_time': time.time(),
                            'slippage': CONFIG['slippage'],
                        }
                        tier_emoji = '🛡️' if tier == 'stable' else '⚡'
                        print(f'\n  {tier_emoji} [{name}] 신규 진입 @ ${price:,.4f} | 기준가 ${signal["base_price"]:,.4f}')
                        print(f'     목표: ATR×{CONFIG["atr_multiplier"]:.0f} | SL: -{CONFIG["layer1_sl"]*100:.0f}% | ${layer_usdt:.1f}')
                        send_telegram(
                            f'{tier_emoji} {name} 진입\n@ ${price:,.4f}\n기준: ${signal["base_price"]:,.4f}\n'
                            f'목표: ATR×{CONFIG["atr_multiplier"]:.0f} | SL: -{CONFIG["layer1_sl"]*100:.0f}%\n사이즈: ${layer_usdt:.1f}')
                        pool_count += 1

            # ── 뮤턴트 봇 ──
            if use_mutant and mutant_cfg:
                mut_stable = sum(1 for p in mutant_positions.values() if p.get('tier','stable') == 'stable')
                mut_max    = mutant_cfg.get('max_stable_pos', 2)

                for sym in list(mutant_positions.keys()):
                    pos     = mutant_positions[sym]
                    candles = universe_candles.get(sym)
                    if not candles:
                        continue
                    price  = get_rt_price(sym) or candles[-1][4]
                    result = check_exit_conditions(pos, price, mutant_cfg)
                    if result['action'] == 'close':
                        pnl = calc_position_pnl(pos, result['exit_price'])
                        mutant_pnl += pnl
                        del mutant_positions[sym]

                if mut_stable < mut_max:
                    for sym in sorted(top_stable):
                        if sym in mutant_positions or mut_stable >= mut_max:
                            continue
                        candles = universe_candles.get(sym)
                        if not candles:
                            continue
                        signal = check_entry_signal(sym, candles, mutant_cfg)
                        if not signal.get('side'):
                            continue
                        price = get_rt_price(sym) or candles[-1][4]
                        sz    = max(mutant_capital * 0.10, 1.0)
                        mutant_positions[sym] = {
                            'symbol': sym, 'side': signal['side'], 'mode': 'breakout', 'tier': 'stable',
                            'layers': [{'entry': price, 'size_usdt': sz, 'level': 1, 'order_id': ''}],
                            'base_price': signal['base_price'], 'next_level': 2,
                            'first_entry': price, 'peak_price': price,
                            'dynamic_target': calc_atr_target(candles, len(candles)-1,
                                                              mutant_cfg.get('atr_period',14),
                                                              mutant_cfg.get('atr_multiplier',8.0)),
                            'last_add_time': time.time(), 'open_time': time.time(),
                            'slippage': CONFIG['slippage'],
                        }
                        mut_stable += 1

            # ── 저장 ──
            save_positions_state()
            save_status_json()

            # ── 현황 출력 (5분마다) ──
            if now.minute % 5 == 0 and now.second < 3:
                eq       = CONFIG['total_capital'] + total_pnl
                ws_info  = f'WS 🟢 ({len(ws_client.subscribed)}개 실시간)' if HAS_WS and ws_client.subscribed else 'WS ❌'
                mode_tag = '📦 박스' if current_weekend else '🔥 돌파'
                print(f'\n  [{now.strftime("%H:%M")}] 📊 현황 | 자본: ${eq:.1f} | PnL: ${total_pnl:+.2f} | MDD: {max_drawdown*100:.1f}%')
                print(f'  {ws_info}')
                if positions:
                    for sym, pos in positions.items():
                        name  = sym.replace('USDT','')
                        price = get_rt_price(sym) or 0
                        ur    = calc_position_pnl(pos, price) if price else 0
                        n_lay = len(pos.get('layers',[]))
                        mtag  = '📦' if pos.get('mode') == 'box' else ''
                        tot_sz= sum(l['size_usdt'] for l in pos.get('layers',[]))
                        print(f'    {mtag}{name:>6} {pos["side"].upper()} {n_lay}L ${tot_sz:.1f} | 미실현 {ur:+.1f}')
                else:
                    print('    (포지션 없음)')
                if use_mutant:
                    print(f'  🧬 뮤턴트 | 자본: ${mutant_capital+mutant_pnl:.1f} | PnL: ${mutant_pnl:+.2f} | 포지션: {len(mutant_positions)}개')
                    if mutant_positions:
                        for sym, pos in mutant_positions.items():
                            name  = sym.replace('USDT','')
                            price = get_rt_price(sym) or 0
                            print(f'    🧬 {name:>6} {pos["side"].upper()} ${sum(l["size_usdt"] for l in pos.get("layers",[])):.1f}')

            # ── 1시간마다 보고 ──
            if time.time() - last_hourly >= 3600:
                try:
                    send_hourly_report()
                    last_hourly = time.time()
                except Exception as e:
                    print(f'  ⚠️ 리포트 전송 에러: {e}')

            # ── 24시간마다 일일 보고 ──
            if time.time() - last_daily >= 86400:
                try:
                    send_daily_report()
                    last_daily = time.time()
                except Exception as e:
                    print(f'  ⚠️ 데일리 리포트 에러: {e}')

            time.sleep(CONFIG['check_interval'])

        except KeyboardInterrupt:
            print('\n\n🛑 ORCA 피라미딩 엔진 종료')
            print(f'\n📊 최종 결과:')
            print(f'  총 PnL: ${total_pnl:+.2f}')
            print(f'  MDD: {max_drawdown*100:.1f}%')
            print(f'  거래: {len(trade_history)}건')
            print(f'  미청산 포지션: {len(positions)}개')
            for sym, pos in positions.items():
                print(f'    {sym}: {pos["side"]}')
            send_telegram(f'🛑 ORCA 피라미딩 종료\n총 PnL: ${total_pnl:+.2f}')
            ws_client.stop()
            break

        except Exception as e:
            print(f'❌ 에러: {e}')
            import traceback
            traceback.print_exc()
            time.sleep(60)


if __name__ == '__main__':
    if '--live' in sys.argv:
        CONFIG['paper_mode'] = False
        print('⚠️  실매매 모드! 실제 돈이 사용됩니다!')
    if '--paper' in sys.argv:
        CONFIG['paper_mode'] = True
    run()
