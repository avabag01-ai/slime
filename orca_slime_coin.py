#!/usr/bin/env python3
"""
ORCA Slime Coin — 코인 슬라임 배분 라이브 트레이딩
────────────────────────────────────────────────────
전략: 돌파+트레일링 6봇 × 6종목 = 36봇
배분: 슬라임 알고리즘 (mu=1, decay=0.05, window=20)
종목: BTC/ETH/SOL/XRP/SUI/DOGE

사용법:
  python3 orca_slime_coin.py --paper    # 모의매매
  python3 orca_slime_coin.py            # 실매매 (주의!)

텔레그램 커맨드:
  /status   — 현재 포트폴리오 상태
  /alloc    — 슬라임 비중 현황
  /pause    — 신규 진입 중단
  /resume   — 재개
"""

import os, sys, json, time, math, hmac, hashlib, base64, threading
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests
import numpy as np

# ── .env 로드 ──
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

API_KEY          = os.getenv('BITGET_API_KEY', '')
SECRET_KEY       = os.getenv('BITGET_SECRET_KEY', '')
PASSPHRASE       = os.getenv('BITGET_PASSPHRASE', '')
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
BASE_URL         = 'https://api.bitget.com'

PAPER_MODE = '--paper' in sys.argv

# ══════════════════════════════════════════════════════════
# 슬라임 파라미터 (백테스트 검증, 과최적화 없음)
# ══════════════════════════════════════════════════════════
SLIME_P = {
    'mu':         1.0,    # 샤프 그대로 반영
    'decay':      0.05,   # 느린 반응 (코인 추세가 길어서)
    'window':     20,     # 최근 20거래 기준
    'min_alloc':  0.01,   # 최소 비중 1%
}
SHORT_CAP    = 0.10   # 숏봇 합계 최대 10%
MAX_BOT_ALLOC = 0.25  # 단일 봇 최대 비중 25%

# ══════════════════════════════════════════════════════════
# 봇 전략 정의 (백테스트 검증)
# ══════════════════════════════════════════════════════════
# (이름, lookback봉, step_pct, trail_pct%, max_hold봉, direction, sl_pct)
STRATEGIES = [
    ('추세롱',  240, 0.08, 2.0, 30,  'long',  0.06),
    ('스윙롱',  480, 0.12, 3.0, 60,  'long',  0.08),
    ('단기롱',   96, 0.05, 1.5, 15,  'long',  0.04),
    ('장기롱',  960, 0.18, 4.0, 96,  'long',  0.12),
    ('추세숏',  240, 0.08, 2.0, 20,  'short', 0.05),
    ('스윙숏',  480, 0.12, 3.0, 30,  'short', 0.07),
]

# ══════════════════════════════════════════════════════════
# 종목 설정
# ══════════════════════════════════════════════════════════
COIN_SPECS = {
    'BTCUSDT': {'min_size': 0.001, 'size_step': 0.001, 'price_prec': 1, 'vol_prec': 3},
    'ETHUSDT': {'min_size': 0.01,  'size_step': 0.01,  'price_prec': 2, 'vol_prec': 2},
    'SOLUSDT': {'min_size': 0.1,   'size_step': 0.1,   'price_prec': 3, 'vol_prec': 1},
    'XRPUSDT': {'min_size': 1,     'size_step': 1,     'price_prec': 4, 'vol_prec': 0},
    'SUIUSDT': {'min_size': 0.1,   'size_step': 0.1,   'price_prec': 4, 'vol_prec': 1},
    'DOGEUSDT':{'min_size': 1,     'size_step': 1,     'price_prec': 5, 'vol_prec': 0},
}

CONFIG = {
    'product_type':   'USDT-FUTURES',
    'granularity':    '15m',
    'candles_needed': 1100,          # 최대 lookback(960) + 여유
    'trade_fee':      0.0006,        # 비트겟 0.06%
    'leverage':       3,
    'total_capital':  300,           # 총 투입금 $300
    'check_interval': 60,            # 1분마다 캔들 체크
    'hedge_mode':     True,
}

# 봇 이름 전체 목록 생성
ALL_BOT_NAMES = [f'{sym}_{strat[0]}' for sym in COIN_SPECS for strat in STRATEGIES]
IS_SHORT_BOT  = {n: 'short' in n or '숏' in n for n in ALL_BOT_NAMES}

# ══════════════════════════════════════════════════════════
# 글로벌 상태
# ══════════════════════════════════════════════════════════
# 슬라임 상태
slime_D    = {n: 1.0 for n in ALL_BOT_NAMES}   # 슬라임 D값
slime_hist = {n: [] for n in ALL_BOT_NAMES}    # 봇별 거래 결과 히스토리
slime_alloc= {n: 1.0/len(ALL_BOT_NAMES) for n in ALL_BOT_NAMES}  # 현재 비중

# 봇 포지션 상태
# bot_positions[bot_name] = {side, entry, peak, hold, size_usdt}
bot_positions = {}

# 누적 성과
equity    = CONFIG['total_capital']
peak_eq   = CONFIG['total_capital']
total_pnl = 0.0
trade_log = []   # [{ts, bot, pnl_pct, pnl_usdt, side}]

paused = False
last_candle_times = {}   # sym → last candle timestamp

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
    r = bitget_api('GET', '/api/v2/mix/market/candles', {
        'productType': CONFIG['product_type'],
        'symbol': symbol, 'granularity': '15m', 'limit': str(limit),
    })
    if r.get('code') != '00000':
        return []
    candles = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])]
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
# 주문 실행
# ══════════════════════════════════════════════════════════

def calc_size(symbol, usdt_amount, price):
    """USDT → 코인 수량 계산"""
    spec     = COIN_SPECS[symbol]
    lev      = CONFIG['leverage']
    raw      = usdt_amount * lev / price
    step     = spec['size_step']
    size     = math.floor(raw / step) * step
    size     = max(size, spec['min_size'])
    return round(size, spec['vol_prec'])


def place_order(symbol, side, size, reduce_only=False):
    """시장가 주문"""
    if PAPER_MODE:
        return {'code': '00000', 'data': {'orderId': 'PAPER'}}

    hold_side = 'long' if side == 'buy' else 'short'
    body = {
        'symbol':       symbol,
        'productType':  CONFIG['product_type'],
        'marginMode':   'crossed',
        'marginCoin':   'USDT',
        'size':         str(size),
        'side':         side,           # buy/sell
        'tradeSide':    'close' if reduce_only else 'open',
        'orderType':    'market',
        'force':        'ioc',
        'holdSide':     hold_side,
    }
    r = bitget_api('POST', '/api/v2/mix/order/place-order', body=body)
    return r


def set_leverage(symbol):
    if PAPER_MODE:
        return
    bitget_api('POST', '/api/v2/mix/account/set-leverage', body={
        'symbol': symbol, 'productType': CONFIG['product_type'],
        'marginCoin': 'USDT', 'leverage': str(CONFIG['leverage']),
    })


# ══════════════════════════════════════════════════════════
# 슬라임 배분 업데이트
# ══════════════════════════════════════════════════════════

def update_slime(bot_name, pnl_pct):
    """
    거래 완료 시 슬라임 D값 갱신 후 전체 비중 재계산.
    pnl_pct: 이번 거래 수익률 (예: 0.03 = +3%)
    """
    global slime_D, slime_alloc

    p = SLIME_P
    slime_hist[bot_name].append(pnl_pct)

    # 모든 봇 D값 갱신
    for n in ALL_BOT_NAMES:
        h = slime_hist[n][-p['window']:]
        if len(h) >= 3:
            arr = np.array(h)
            Q = max(arr.mean() / (arr.std() + 1e-9), 0.0)
            Q = Q ** p['mu']
        else:
            Q = 0.0
        slime_D[n] = slime_D[n] * (1 - p['decay']) + Q

    # 비중 계산
    total = sum(max(v, p['min_alloc']) for v in slime_D.values())
    alloc = {n: max(slime_D[n], p['min_alloc']) / total for n in slime_D}

    # 단일 봇 캡 (25%)
    capped = False
    for n in alloc:
        if alloc[n] > MAX_BOT_ALLOC:
            alloc[n] = MAX_BOT_ALLOC
            capped = True
    if capped:
        t2 = sum(alloc.values())
        alloc = {n: v / t2 for n, v in alloc.items()}

    # 숏봇 캡 (10%)
    short_total = sum(alloc[n] for n in alloc if IS_SHORT_BOT[n])
    if short_total > SHORT_CAP:
        scale  = SHORT_CAP / short_total
        excess = short_total - SHORT_CAP
        long_total = sum(alloc[n] for n in alloc if not IS_SHORT_BOT[n])
        for n in alloc:
            if IS_SHORT_BOT[n]:
                alloc[n] *= scale
            elif long_total > 0:
                alloc[n] += alloc[n] / long_total * excess

    slime_alloc = alloc


def get_bot_usdt(bot_name):
    """현재 슬라임 비중에 따른 봇 배분 USDT"""
    return equity * slime_alloc.get(bot_name, 1.0/len(ALL_BOT_NAMES))


# ══════════════════════════════════════════════════════════
# 전략 로직 (단일 봇)
# ══════════════════════════════════════════════════════════

def check_entry(candles, lookback, step_pct, direction):
    """진입 신호 체크. 반환: True/False"""
    if len(candles) < lookback + 2:
        return False
    close = candles[-1][4]
    if direction == 'long':
        recent_low = min(candles[j][3] for j in range(-lookback-1, -1))
        if recent_low <= 0:
            return False
        return (close - recent_low) / recent_low >= step_pct
    else:
        recent_high = max(candles[j][2] for j in range(-lookback-1, -1))
        if recent_high <= 0:
            return False
        return (recent_high - close) / recent_high >= step_pct


def check_exit(pos, close, trail_pct, sl_pct, max_hold):
    """
    청산 조건 체크.
    반환: (should_exit: bool, reason: str, exit_price: float)
    """
    direction = pos['direction']
    entry     = pos['entry']
    peak      = pos['peak']
    hold      = pos['hold']

    if direction == 'long':
        # 피크 갱신
        new_peak = max(peak, close)
        trail    = new_peak * (1 - trail_pct / 100.0)
        sl       = entry * (1 - sl_pct)
        if close <= sl:
            return True, 'SL', sl, new_peak
        if close <= trail and new_peak > entry * 1.03:
            return True, 'TRAIL', close, new_peak
        if hold >= max_hold:
            return True, 'MAXHOLD', close, new_peak
    else:
        new_peak = min(peak, close)
        trail    = new_peak * (1 + trail_pct / 100.0)
        sl       = entry * (1 + sl_pct)
        if close >= sl:
            return True, 'SL', sl, new_peak
        if close >= trail and new_peak < entry * 0.97:
            return True, 'TRAIL', close, new_peak
        if hold >= max_hold:
            return True, 'MAXHOLD', close, new_peak

    return False, '', close, new_peak


# ══════════════════════════════════════════════════════════
# 메인 루프: 캔들 체크 + 봇 실행
# ══════════════════════════════════════════════════════════

def process_symbol(symbol, candles):
    """심볼의 모든 봇 처리"""
    global equity, peak_eq, total_pnl, bot_positions

    close = candles[-1][4]

    for strat in STRATEGIES:
        strat_name, lookback, step_pct, trail_pct, max_hold, direction, sl_pct = strat
        bot_name = f'{symbol}_{strat_name}'

        pos = bot_positions.get(bot_name)

        if pos:
            # ── 포지션 관리 ──
            pos['hold'] += 1
            should_exit, reason, exit_price, new_peak = check_exit(
                pos, close, trail_pct, sl_pct, max_hold)
            pos['peak'] = new_peak

            if should_exit:
                # 수익률 계산
                if direction == 'long':
                    pnl_pct = (exit_price - pos['entry']) / pos['entry'] - CONFIG['trade_fee'] * 2
                else:
                    pnl_pct = (pos['entry'] - exit_price) / pos['entry'] - CONFIG['trade_fee'] * 2

                pnl_usdt = pos['size_usdt'] * pnl_pct * CONFIG['leverage']
                equity   += pnl_usdt
                peak_eq   = max(peak_eq, equity)
                total_pnl += pnl_usdt

                # 실 주문 청산
                if not PAPER_MODE:
                    side = 'sell' if direction == 'long' else 'buy'
                    place_order(symbol, side, pos['size_coin'], reduce_only=True)

                # 슬라임 업데이트
                update_slime(bot_name, pnl_pct)

                trade_log.append({
                    'ts': int(time.time()), 'bot': bot_name,
                    'pnl_pct': pnl_pct, 'pnl_usdt': pnl_usdt,
                    'side': direction, 'reason': reason,
                })

                emoji = '✅' if pnl_pct > 0 else '❌'
                print(f'  {emoji} {bot_name} {reason} | {pnl_pct*100:+.2f}% | ${pnl_usdt:+.2f} | 잔액=${equity:.0f}')
                send_telegram(
                    f'{emoji} <b>{bot_name}</b> {reason}\n'
                    f'  수익: {pnl_pct*100:+.2f}% (${pnl_usdt:+.1f})\n'
                    f'  잔액: ${equity:.0f} | MDD: {(peak_eq-equity)/peak_eq*100:.1f}%'
                )

                del bot_positions[bot_name]

        elif not paused:
            # ── 진입 신호 체크 ──
            if check_entry(candles, lookback, step_pct, direction):
                # 배분 USDT 계산
                usdt = get_bot_usdt(bot_name)
                if usdt < 3.0:
                    continue   # 너무 적으면 스킵

                size_coin = calc_size(symbol, usdt, close)
                if size_coin <= 0:
                    continue

                # 실 주문 진입
                if not PAPER_MODE:
                    set_leverage(symbol)
                    side = 'buy' if direction == 'long' else 'sell'
                    r = place_order(symbol, side, size_coin)
                    if r.get('code') != '00000':
                        print(f'  ⚠️ {bot_name} 주문 실패: {r.get("msg")}')
                        continue

                bot_positions[bot_name] = {
                    'direction': direction,
                    'entry':     close,
                    'peak':      close,
                    'hold':      0,
                    'size_usdt': usdt,
                    'size_coin': size_coin,
                    'open_ts':   int(time.time()),
                }

                alloc_pct = slime_alloc.get(bot_name, 0) * 100
                print(f'  📈 {bot_name} 진입 {direction} @ {close:.4f} | ${usdt:.1f} ({alloc_pct:.1f}%)')


def print_status():
    """현재 상태 출력"""
    mdd = (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0
    print(f'\n{"="*60}')
    print(f'  잔액: ${equity:.2f}  PnL: ${total_pnl:+.2f}  MDD: {mdd:.1f}%')
    print(f'  포지션: {len(bot_positions)}개 활성')

    # 상위 비중 봇
    top_alloc = sorted(slime_alloc.items(), key=lambda x: -x[1])[:5]
    print(f'  슬라임 TOP5 비중:')
    for n, a in top_alloc:
        in_pos = '🟢' if n in bot_positions else '⚪'
        print(f'    {in_pos} {n:25s} {a*100:5.1f}%')
    print(f'{"="*60}')


def send_status_telegram():
    """텔레그램 상태 보고"""
    mdd   = (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0
    recent = trade_log[-5:] if trade_log else []

    msg = (f'<b>ORCA Slime Coin 상태</b>\n'
           f'잔액: ${equity:.2f} | PnL: ${total_pnl:+.2f}\n'
           f'MDD: {mdd:.1f}% | 포지션: {len(bot_positions)}개\n\n')

    # 종목별 슬라임 비중
    sym_alloc = {}
    for sym in COIN_SPECS:
        sym_alloc[sym] = sum(slime_alloc.get(f'{sym}_{s[0]}', 0) for s in STRATEGIES)
    msg += '<b>종목별 비중:</b>\n'
    for sym, a in sorted(sym_alloc.items(), key=lambda x: -x[1]):
        msg += f'  {sym.replace("USDT",""):6s}: {a*100:.1f}%\n'

    if recent:
        msg += '\n<b>최근 거래:</b>\n'
        for t in reversed(recent):
            e = '✅' if t['pnl_pct'] > 0 else '❌'
            msg += f'  {e} {t["bot"]} {t["pnl_pct"]*100:+.1f}%\n'

    send_telegram(msg)


# ══════════════════════════════════════════════════════════
# 텔레그램 커맨드 폴링
# ══════════════════════════════════════════════════════════

tg_offset = 0

def poll_telegram():
    global paused, tg_offset
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates',
            params={'offset': tg_offset, 'timeout': 5}, timeout=10).json()
        for upd in r.get('result', []):
            tg_offset = upd['update_id'] + 1
            text = upd.get('message', {}).get('text', '').strip().lower()
            if text == '/status':
                send_status_telegram()
            elif text == '/alloc':
                top = sorted(slime_alloc.items(), key=lambda x: -x[1])[:10]
                msg = '<b>슬라임 비중 TOP10:</b>\n'
                for n, a in top:
                    msg += f'  {n}: {a*100:.1f}%\n'
                send_telegram(msg)
            elif text == '/pause':
                paused = True
                send_telegram('⏸ 신규 진입 중단')
            elif text == '/resume':
                paused = False
                send_telegram('▶️ 재개')
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════

def main():
    global last_candle_times

    mode = '📄 모의매매' if PAPER_MODE else '💰 실매매'
    print(f'\n🐋 ORCA Slime Coin 시작 — {mode}')
    print(f'  종목: {len(COIN_SPECS)}개 | 봇: {len(ALL_BOT_NAMES)}개 | 자본: ${CONFIG["total_capital"]}')
    print(f'  슬라임: mu={SLIME_P["mu"]} decay={SLIME_P["decay"]} window={SLIME_P["window"]}')
    print(f'  숏캡: {SHORT_CAP*100:.0f}% | 봇캡: {MAX_BOT_ALLOC*100:.0f}%\n')

    send_telegram(
        f'🐋 <b>ORCA Slime Coin 시작</b> ({mode})\n'
        f'종목 {len(COIN_SPECS)}개 | 봇 {len(ALL_BOT_NAMES)}개 | 자본 ${CONFIG["total_capital"]}'
    )

    # 레버리지 초기 설정
    if not PAPER_MODE:
        for sym in COIN_SPECS:
            set_leverage(sym)
            time.sleep(0.2)

    last_status_report = 0

    while True:
        try:
            now = time.time()

            # 텔레그램 커맨드 체크
            poll_telegram()

            # 각 종목 처리
            for symbol in COIN_SPECS:
                candles = get_candles(symbol, CONFIG['candles_needed'])
                if not candles or len(candles) < 200:
                    continue

                # 새 캔들 체크 (15분마다 실행)
                latest_ts = candles[-1][0]
                last_ts   = last_candle_times.get(symbol, 0)

                if latest_ts <= last_ts:
                    continue   # 새 캔들 없음 → 스킵

                last_candle_times[symbol] = latest_ts
                dt = datetime.fromtimestamp(latest_ts/1000, tz=timezone.utc).strftime('%H:%M')
                print(f'\n[{dt}] {symbol} 새 캔들 | close={candles[-1][4]:.4f}')

                process_symbol(symbol, candles)
                time.sleep(0.3)   # API 레이트 리밋

            # 1시간마다 상태 보고
            if now - last_status_report > 3600:
                print_status()
                send_status_telegram()
                last_status_report = now

            time.sleep(CONFIG['check_interval'])

        except KeyboardInterrupt:
            print('\n\n종료...')
            print_status()
            send_telegram('🛑 ORCA Slime Coin 종료')
            break
        except Exception as e:
            print(f'  ❌ 에러: {e}')
            time.sleep(30)


if __name__ == '__main__':
    main()
