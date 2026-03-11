//+------------------------------------------------------------------+
//|                                                   ORCA_OIL.mq5   |
//|                     ORCA Oil — ORCA_FX 기반 오일 전용 EA          |
//|                     USOIL 5분봉 | 돌파+피라미딩+트레일링           |
//+------------------------------------------------------------------+
//| ORCA_FX(금) DNA 기반, 오일 특성에 맞게 파라미터 조정               |
//|   - 돌파 0.30% (오일 변동성 금보다 작음)                           |
//|   - 스프레드 80포인트 허용 (오일 스프레드 넓음)                      |
//|   - 단방향 (MaxPositions=1) — 롱숏 동시 방지                       |
//|   - RiskPct 12% 유지 (공격적 대회용)                               |
//+------------------------------------------------------------------+
#property copyright "ORCA Bot — Oil Edition"
#property version   "1.00"
#property description "ORCA Oil — USOIL 전용 돌파 피라미딩 EA"
#include <Trade\Trade.mqh>

// ═══════════════════════════════════════════════════════════════════
// 입력 파라미터 — 진화 챔피언 DNA (Hold-out 검증 통과)
// ═══════════════════════════════════════════════════════════════════

input group "=== 진입 ==="
input double InpStepPct      = 0.0030;    // 돌파 기준 0.30% (오일용 — 금보다 낮은 변동성)
input int    InpLookback     = 241;       // 고점/저점 탐색 241봉 ≈ 20시간 (5분봉)
input int    InpCooldown     = 7;         // 피라미딩 쿨다운 7봉 = 35분
input int    InpMaxLayers    = 4;         // 피라미딩 최대 4단
input int    InpMaxPositions = 1;         // 단방향만 (롱 or 숏, 동시 방지)

input group "=== 청산 ==="
input double InpLayer1SL     = 0.0223;    // 1차 SL 2.23%
input double InpTrailPct     = 0.001;     // 트레일링 0.1%
input int    InpTrailAfter   = 1;         // 1레이어부터 트레일링
input int    InpATR_Period   = 42;        // ATR 기간
input double InpATR_Mult     = 19.7;      // 익절 = ATR × 19.7

input group "=== 사이징 ==="
input double InpRiskPct      = 0.120;     // 진입당 자본의 12% (대회용 공격적 사이징)

input group "=== 필터 ==="
input int    InpSpreadMax    = 80;        // 최대 스프레드 80포인트 (오일 스프레드 넓음)
input bool   InpFridayExit   = true;      // 금요일 강제 청산
input bool   InpSwapFilter   = false;     // 스왑 필터 (비활성)

input group "=== 기타 ==="
input int    InpMagicNumber  = 20260312;  // 매직 넘버 (오일 전용)

// ═══════════════════════════════════════════════════════════════════
// 글로벌 변수
// ═══════════════════════════════════════════════════════════════════

// 피라미딩 레이어
struct Layer
{
   double   entry;       // 진입가
   double   lots;        // 로트
   ulong    ticket;      // 포지션 티켓
   datetime time;        // 진입 시간
};

Layer g_long_layers[];
Layer g_short_layers[];

// 트레일링 피크
double g_peak_long  = 0;
double g_peak_short = 0;

// 쿨다운
datetime g_last_long_add  = 0;
datetime g_last_short_add = 0;

// 새 봉 감지
datetime g_last_bar_time = 0;

// 누적 통계
int    g_total_trades = 0;
int    g_total_wins   = 0;
double g_total_pnl    = 0;

// 동적 레버리지 — MDD 기반 안전밸브
double g_peak_equity    = 0;    // 세션 중 최고 자본
double g_risk_scale     = 1.0;  // 사이징 스케일 (1.0=풀, 0.5=절반)

// 트레이드 객체
CTrade g_trade;

// ═══════════════════════════════════════════════════════════════════
// 초기화
// ═══════════════════════════════════════════════════════════════════
int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(15);  // 슬리피지 허용폭
   g_trade.SetTypeFilling(ORDER_FILLING_IOC);

   ArrayResize(g_long_layers, 0);
   ArrayResize(g_short_layers, 0);

   // EA 재시작 시 기존 포지션 복구
   RecoverExistingPositions();

   // 동적 레버리지: 피크 자본 초기화
   g_peak_equity = AccountInfoDouble(ACCOUNT_EQUITY);
   g_risk_scale  = 1.0;

   Print("═══════════════════════════════════════════════════");
   Print("  ORCA Oil v1.0 — USOIL 전용 오일 EA");
   PrintFormat("  심볼: %s | TF: M5", _Symbol);
   PrintFormat("  돌파: %.2f%% | SL: %.2f%% | 사이징: %.1f%%",
               InpStepPct * 100, InpLayer1SL * 100, InpRiskPct * 100);
   PrintFormat("  피라미딩: 최대 %d단 | 트레일: %.1f%% (>=%d단)",
               InpMaxLayers, InpTrailPct * 100, InpTrailAfter);
   PrintFormat("  룩백: %d봉 | 쿨다운: %d봉 | 타겟: ATR x%.1f",
               InpLookback, InpCooldown, InpATR_Mult);
   Print("═══════════════════════════════════════════════════");

   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   PrintFormat("ORCA 종료 | 총거래: %d | 승률: %.0f%% | PnL: $%.2f",
               g_total_trades,
               g_total_trades > 0 ? (double)g_total_wins / g_total_trades * 100 : 0,
               g_total_pnl);
}

// ═══════════════════════════════════════════════════════════════════
// 메인 틱 핸들러 — 5분봉 기준
// ═══════════════════════════════════════════════════════════════════
void OnTick()
{
   // 5분봉 새 봉 완성 확인
   datetime current_bar = iTime(_Symbol, PERIOD_M5, 0);
   if(current_bar == g_last_bar_time)
      return;
   g_last_bar_time = current_bar;

   // 동적 레버리지: MDD 실시간 추적 → 사이징 자동 조절
   UpdateRiskScale();

   // Phase 1: 청산 체크 (우선순위: SL → BE → TRAIL → TARGET)
   CheckExitLong();
   CheckExitShort();

   // Phase 2: 피라미딩 (기존 포지션 방향으로 추가 진입)
   CheckPyramidAdd();

   // Phase 3: 신규 진입 (돌파 감지)
   CheckNewEntry();

   // Phase 4: 금요일 강제 청산
   if(InpFridayExit)
      CheckFridayClose();

   // 차트 HUD
   UpdateChartComment();
}

// ═══════════════════════════════════════════════════════════════════
// Phase 1A: 롱 청산 — 4단계 우선순위
// Rust run_backtest 로직과 정확히 동일
// ═══════════════════════════════════════════════════════════════════
void CheckExitLong()
{
   int n = ArraySize(g_long_layers);
   if(n == 0) return;

   // 직전 완성봉의 종가 사용 (Rust 백테스터와 동일)
   double close = iClose(_Symbol, PERIOD_M5, 1);

   // A) SL1: 1차 레이어 기준 손절
   double sl_price = g_long_layers[0].entry * (1.0 - InpLayer1SL);
   if(close <= sl_price)
   {
      PrintFormat("SL1 LONG | entry=%.2f sl=%.2f close=%.2f", g_long_layers[0].entry, sl_price, close);
      CloseAllLong("SL1", false);
      return;
   }

   // B) BE_AVG: 평단가 본절 (2+ 레이어)
   if(n >= 2)
   {
      double avg = GetAvgEntry(g_long_layers);
      if(close <= avg)
      {
         PrintFormat("BE_AVG LONG | avg=%.2f close=%.2f", avg, close);
         CloseAllLong("BE_AVG", false);
         return;
      }
   }

   // C) TRAIL: 트레일링 스탑 (close 기준 — Rust와 동일)
   if(n >= InpTrailAfter)
   {
      // 피크는 close 기준으로 갱신 (Rust 백테스터와 동일)
      if(close > g_peak_long) g_peak_long = close;

      double trail_price = g_peak_long * (1.0 - InpTrailPct);
      if(close <= trail_price)
      {
         double pnl_pct = (close - g_long_layers[0].entry) / g_long_layers[0].entry * 100;
         PrintFormat("TRAIL LONG | peak=%.2f trail=%.2f close=%.2f (%.1f%%)",
                     g_peak_long, trail_price, close, pnl_pct);
         CloseAllLong("TRAIL", pnl_pct > 0);
         return;
      }
   }

   // D) TARGET: ATR% 기준 익절 (Rust와 동일 — ATR%×Mult)
   double atr_pct = CalcATRPct(InpATR_Period);
   if(atr_pct > 0)
   {
      double target_pct = atr_pct * InpATR_Mult;
      double gain = (close - g_long_layers[0].entry) / g_long_layers[0].entry;
      if(gain >= target_pct)
      {
         PrintFormat("TARGET LONG | gain=%.2f%% target=%.2f%%", gain * 100, target_pct * 100);
         CloseAllLong("TARGET", true);
         return;
      }
   }

   // 피크 갱신 (청산 체크 후)
   if(close > g_peak_long || g_peak_long == 0)
      g_peak_long = close;
}

// ═══════════════════════════════════════════════════════════════════
// Phase 1B: 숏 청산 — 4단계 우선순위
// ═══════════════════════════════════════════════════════════════════
void CheckExitShort()
{
   int n = ArraySize(g_short_layers);
   if(n == 0) return;

   double close = iClose(_Symbol, PERIOD_M5, 1);

   // A) SL1
   double sl_price = g_short_layers[0].entry * (1.0 + InpLayer1SL);
   if(close >= sl_price)
   {
      PrintFormat("SL1 SHORT | entry=%.2f sl=%.2f close=%.2f", g_short_layers[0].entry, sl_price, close);
      CloseAllShort("SL1", false);
      return;
   }

   // B) BE_AVG
   if(n >= 2)
   {
      double avg = GetAvgEntry(g_short_layers);
      if(close >= avg)
      {
         PrintFormat("BE_AVG SHORT | avg=%.2f close=%.2f", avg, close);
         CloseAllShort("BE_AVG", false);
         return;
      }
   }

   // C) TRAIL (close 기준)
   if(n >= InpTrailAfter)
   {
      if(close < g_peak_short || g_peak_short == 0) g_peak_short = close;

      double trail_price = g_peak_short * (1.0 + InpTrailPct);
      if(close >= trail_price)
      {
         double pnl_pct = (g_short_layers[0].entry - close) / g_short_layers[0].entry * 100;
         PrintFormat("TRAIL SHORT | peak=%.2f trail=%.2f close=%.2f (%.1f%%)",
                     g_peak_short, trail_price, close, pnl_pct);
         CloseAllShort("TRAIL", pnl_pct > 0);
         return;
      }
   }

   // D) TARGET
   double atr_pct = CalcATRPct(InpATR_Period);
   if(atr_pct > 0)
   {
      double target_pct = atr_pct * InpATR_Mult;
      double gain = (g_short_layers[0].entry - close) / g_short_layers[0].entry;
      if(gain >= target_pct)
      {
         PrintFormat("TARGET SHORT | gain=%.2f%% target=%.2f%%", gain * 100, target_pct * 100);
         CloseAllShort("TARGET", true);
         return;
      }
   }

   // 피크 갱신
   if(close < g_peak_short || g_peak_short == 0)
      g_peak_short = close;
}

// ═══════════════════════════════════════════════════════════════════
// Phase 2: 피라미딩 — 방향 맞으면 레이어 추가
// ═══════════════════════════════════════════════════════════════════
void CheckPyramidAdd()
{
   double close = iClose(_Symbol, PERIOD_M5, 1);
   datetime now = iTime(_Symbol, PERIOD_M5, 0);

   // 롱 피라미딩
   int ln = ArraySize(g_long_layers);
   if(ln > 0 && ln < InpMaxLayers)
   {
      if(BarsElapsed(g_last_long_add, now) >= InpCooldown)
      {
         int next = ln + 1;
         double base = g_long_layers[0].entry;
         double req = InpStepPct * next;  // step × 레이어번호
         double act = (close - base) / base;

         if(act >= req)
         {
            // 안전장치: 마지막 레이어(4단)는 트레일링이 본전 이상일 때만 허용
            // → 되돌림 시 4단 풀스택 손실 방지
            if(next == InpMaxLayers)
            {
               double avg = GetAvgEntry(g_long_layers);
               if(close <= avg * 1.005)  // 평단 대비 0.5% 이상 수익이 아니면 스킵
               {
                  // 아직 본전 근처 → 4단 진입 보류
                  return;
               }
            }

            double lots = CalcLots();
            if(lots > 0)
            {
               PrintFormat("LONG +L%d | gain=%.2f%% req=%.2f%% | lots=%.2f",
                           next, act * 100, req * 100, lots);
               OpenPosition(ORDER_TYPE_BUY, lots, "PYR_L" + IntegerToString(next));
               g_last_long_add = now;
            }
         }
      }
   }

   // 숏 피라미딩
   int sn = ArraySize(g_short_layers);
   if(sn > 0 && sn < InpMaxLayers)
   {
      if(BarsElapsed(g_last_short_add, now) >= InpCooldown)
      {
         int next = sn + 1;
         double base = g_short_layers[0].entry;
         double req = InpStepPct * next;
         double act = (base - close) / base;

         if(act >= req)
         {
            // 안전장치: 마지막 레이어 본전 이상 필수
            if(next == InpMaxLayers)
            {
               double avg = GetAvgEntry(g_short_layers);
               if(close >= avg * 0.995)
               {
                  return;
               }
            }

            double lots = CalcLots();
            if(lots > 0)
            {
               PrintFormat("SHORT +L%d | gain=%.2f%% req=%.2f%% | lots=%.2f",
                           next, act * 100, req * 100, lots);
               OpenPosition(ORDER_TYPE_SELL, lots, "PYR_S" + IntegerToString(next));
               g_last_short_add = now;
            }
         }
      }
   }
}

// ═══════════════════════════════════════════════════════════════════
// Phase 3: 신규 진입 — 돌파 감지
// ═══════════════════════════════════════════════════════════════════
void CheckNewEntry()
{
   // 포지션 수 체크
   bool has_long  = (ArraySize(g_long_layers) > 0);
   bool has_short = (ArraySize(g_short_layers) > 0);
   int total = (has_long ? 1 : 0) + (has_short ? 1 : 0);
   if(total >= InpMaxPositions) return;

   // 세션 필터: 서버시간 08~20시만 진입 (런던 개장~NY 오후, 저유동성 야간 스킵)
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.hour < 8 || dt.hour >= 20) return;

   // 금요일 19시 이후 진입 금지 (주말 갭 방지)
   if(dt.day_of_week == 5 && dt.hour >= 19) return;

   // 스프레드 체크
   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > InpSpreadMax) return;

   // 마진 레벨 체크 (150% 이하면 진입 금지)
   double margin_level = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   if(margin_level > 0 && margin_level < 150) return;

   // 자본 체크 (너무 작으면 진입 금지 — Rust equity > 10.0)
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(equity < 10) return;

   // 돌파 감지 — 직전 완성봉 종가 사용
   double close = iClose(_Symbol, PERIOD_M5, 1);

   double recent_low  = FindRecentLow(InpLookback);
   double recent_high = FindRecentHigh(InpLookback);

   double gain = (recent_low > 0)  ? (close - recent_low) / recent_low   : 0;
   double drop = (recent_high > 0) ? (recent_high - close) / recent_high : 0;

   bool long_sig  = (gain >= InpStepPct) && !has_long;
   bool short_sig = (drop >= InpStepPct) && !has_short;

   // 양쪽 모두 돌파 → 강한 방향 선택
   if(long_sig && short_sig)
   {
      if(gain >= drop) short_sig = false;
      else             long_sig  = false;
   }

   if(!long_sig && !short_sig) return;

   // 스왑 캐리 필터 — 스왑 유리한 방향만 진입 허용
   // 롱 스왑 > 숏 스왑이면 롱만, 반대면 숏만
   if(InpSwapFilter)
   {
      double swap_long  = SymbolInfoDouble(_Symbol, SYMBOL_SWAP_LONG);
      double swap_short = SymbolInfoDouble(_Symbol, SYMBOL_SWAP_SHORT);
      if(long_sig  && swap_long  < swap_short) long_sig  = false;  // 롱이 스왑 불리 → 롱 진입 금지
      if(short_sig && swap_short < swap_long)  short_sig = false;  // 숏이 스왑 불리 → 숏 진입 금지
      if(!long_sig && !short_sig) return;
   }

   double lots = CalcLots();
   if(lots <= 0) return;

   if(long_sig)
   {
      PrintFormat("LONG 진입 | close=%.2f low=%.2f gain=%.2f%% lots=%.2f",
                  close, recent_low, gain * 100, lots);
      OpenPosition(ORDER_TYPE_BUY, lots, "ENTRY_L1");
      g_last_long_add = iTime(_Symbol, PERIOD_M5, 0);
      g_peak_long = close;  // close 기준 (Rust와 동일)
   }
   else if(short_sig)
   {
      PrintFormat("SHORT 진입 | close=%.2f high=%.2f drop=%.2f%% lots=%.2f",
                  close, recent_high, drop * 100, lots);
      OpenPosition(ORDER_TYPE_SELL, lots, "ENTRY_S1");
      g_last_short_add = iTime(_Symbol, PERIOD_M5, 0);
      g_peak_short = close;
   }
}

// ═══════════════════════════════════════════════════════════════════
// Phase 4: 금요일 강제 청산 — 주말 갭 방지
// ═══════════════════════════════════════════════════════════════════
void CheckFridayClose()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.day_of_week != 5 || dt.hour < 21) return;

   if(ArraySize(g_long_layers) > 0)
   {
      Print("금요일 청산: LONG");
      CloseAllLong("FRIDAY", false);
   }
   if(ArraySize(g_short_layers) > 0)
   {
      Print("금요일 청산: SHORT");
      CloseAllShort("FRIDAY", false);
   }
}

// ═══════════════════════════════════════════════════════════════════
// 유틸리티 함수
// ═══════════════════════════════════════════════════════════════════

//+------------------------------------------------------------------+
//| ATR% 계산 — TR / 종가 비율의 평균                                   |
//+------------------------------------------------------------------+
double CalcATRPct(int period)
{
   double atr_sum = 0;
   for(int i = 1; i <= period; i++)
   {
      double h = iHigh(_Symbol, PERIOD_M5, i);
      double l = iLow(_Symbol, PERIOD_M5, i);
      double p = iClose(_Symbol, PERIOD_M5, i + 1);  // 전봉 종가
      double tr = MathMax(h - l, MathMax(MathAbs(h - p), MathAbs(l - p)));
      atr_sum += tr;
   }
   double price = iClose(_Symbol, PERIOD_M5, 1);
   if(price <= 0) return 0;
   return (atr_sum / period) / price;  // ATR% (비율)
}

//+------------------------------------------------------------------+
//| 최근 N봉 저점 (직전 완성봉부터)                                      |
//+------------------------------------------------------------------+
double FindRecentLow(int lookback)
{
   double mn = DBL_MAX;
   for(int i = 1; i <= lookback; i++)
   {
      double l = iLow(_Symbol, PERIOD_M5, i);
      if(l < mn) mn = l;
   }
   return (mn < DBL_MAX) ? mn : 0;
}

//+------------------------------------------------------------------+
//| 최근 N봉 고점 (직전 완성봉부터)                                      |
//+------------------------------------------------------------------+
double FindRecentHigh(int lookback)
{
   double mx = 0;
   for(int i = 1; i <= lookback; i++)
   {
      double h = iHigh(_Symbol, PERIOD_M5, i);
      if(h > mx) mx = h;
   }
   return mx;
}

//+------------------------------------------------------------------+
//| 평균 진입가 (가중평균)                                               |
//+------------------------------------------------------------------+
double GetAvgEntry(Layer &layers[])
{
   double sv = 0, sl = 0;
   for(int i = 0; i < ArraySize(layers); i++)
   {
      sv += layers[i].entry * layers[i].lots;
      sl += layers[i].lots;
   }
   return (sl > 0) ? sv / sl : 0;
}

//+------------------------------------------------------------------+
//| 로트 계산 — equity × risk_pct / margin_per_lot                     |
//+------------------------------------------------------------------+
double CalcLots()
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);

   // Flash Crash 방어: 주문 직전 실시간 MDD 재확인
   // UpdateRiskScale()은 봉 시작에만 호출되므로, 봉 중간 급락 대비
   if(g_peak_equity > 0)
   {
      double rt_dd = (g_peak_equity - equity) / g_peak_equity;
      if(rt_dd >= 0.08)  // 8% 이상 급락 → 진입 완전 차단
      {
         PrintFormat("FLASH GUARD | MDD=%.1f%% — 진입 차단", rt_dd * 100);
         return 0;
      }
      if(rt_dd >= 0.05)  // 5%+ → 실시간으로도 절반 강제
         g_risk_scale = 0.5;
   }

   double margin_per_lot = 0;

   if(!OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, 1.0,
                       SymbolInfoDouble(_Symbol, SYMBOL_ASK), margin_per_lot))
      return SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);

   if(margin_per_lot <= 0)
      return SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);

   // 사이징: 자본의 7.4%를 마진으로 사용 × 리스크 스케일
   double margin_use = equity * InpRiskPct * g_risk_scale;
   double raw_lots = margin_use / margin_per_lot;

   // 브로커 제한에 맞춤
   double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   raw_lots = MathFloor(raw_lots / lot_step) * lot_step;
   raw_lots = MathMax(min_lot, MathMin(max_lot, raw_lots));

   return NormalizeDouble(raw_lots, 2);
}

//+------------------------------------------------------------------+
//| 포지션 열기                                                        |
//+------------------------------------------------------------------+
void OpenPosition(ENUM_ORDER_TYPE type, double lots, string comment)
{
   double price = (type == ORDER_TYPE_BUY)
                  ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                  : SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(g_trade.PositionOpen(_Symbol, type, lots, price, 0, 0, comment))
   {
      ulong ticket = g_trade.ResultOrder();
      double fill_price = g_trade.ResultPrice();

      if(type == ORDER_TYPE_BUY)
      {
         int idx = ArraySize(g_long_layers);
         ArrayResize(g_long_layers, idx + 1);
         g_long_layers[idx].entry  = fill_price;
         g_long_layers[idx].lots   = lots;
         g_long_layers[idx].ticket = ticket;
         g_long_layers[idx].time   = TimeCurrent();
         PrintFormat("  LONG L%d @ %.2f | %.2f lots", idx + 1, fill_price, lots);
      }
      else
      {
         int idx = ArraySize(g_short_layers);
         ArrayResize(g_short_layers, idx + 1);
         g_short_layers[idx].entry  = fill_price;
         g_short_layers[idx].lots   = lots;
         g_short_layers[idx].ticket = ticket;
         g_short_layers[idx].time   = TimeCurrent();
         PrintFormat("  SHORT L%d @ %.2f | %.2f lots", idx + 1, fill_price, lots);
      }
   }
   else
   {
      uint err = g_trade.ResultRetcode();
      PrintFormat("  주문실패 err=%u: %s", err, g_trade.ResultComment());

      // IOC 실패 시 FOK로 재시도
      if(err == 10030 || err == 10014)
      {
         g_trade.SetTypeFilling(ORDER_FILLING_FOK);
         if(g_trade.PositionOpen(_Symbol, type, lots, price, 0, 0, comment + "_FOK"))
         {
            double fp = g_trade.ResultPrice();
            if(type == ORDER_TYPE_BUY)
            {
               int idx = ArraySize(g_long_layers);
               ArrayResize(g_long_layers, idx + 1);
               g_long_layers[idx].entry  = fp;
               g_long_layers[idx].lots   = lots;
               g_long_layers[idx].ticket = g_trade.ResultOrder();
               g_long_layers[idx].time   = TimeCurrent();
            }
            else
            {
               int idx = ArraySize(g_short_layers);
               ArrayResize(g_short_layers, idx + 1);
               g_short_layers[idx].entry  = fp;
               g_short_layers[idx].lots   = lots;
               g_short_layers[idx].ticket = g_trade.ResultOrder();
               g_short_layers[idx].time   = TimeCurrent();
            }
         }
         g_trade.SetTypeFilling(ORDER_FILLING_IOC);  // 원복
      }
   }
}

//+------------------------------------------------------------------+
//| 롱 전체 청산                                                       |
//+------------------------------------------------------------------+
void CloseAllLong(string reason, bool is_win)
{
   int n = ArraySize(g_long_layers);
   double total_pnl = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;
      if(PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_BUY) continue;

      total_pnl += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      g_trade.PositionClose(ticket);
   }

   // 통계 업데이트
   g_total_trades++;
   g_total_pnl += total_pnl;
   if(is_win || total_pnl > 0)
      g_total_wins++;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   PrintFormat("  LONG [%s] | %d층 | PnL=$%.2f | W/T=%d/%d | Eq=$%.2f",
               reason, n, total_pnl, g_total_wins, g_total_trades, equity);

   ArrayResize(g_long_layers, 0);
   g_peak_long = 0;
   g_last_long_add = 0;
}

//+------------------------------------------------------------------+
//| 숏 전체 청산                                                       |
//+------------------------------------------------------------------+
void CloseAllShort(string reason, bool is_win)
{
   int n = ArraySize(g_short_layers);
   double total_pnl = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;
      if(PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_SELL) continue;

      total_pnl += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      g_trade.PositionClose(ticket);
   }

   g_total_trades++;
   g_total_pnl += total_pnl;
   if(is_win || total_pnl > 0)
      g_total_wins++;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   PrintFormat("  SHORT [%s] | %d층 | PnL=$%.2f | W/T=%d/%d | Eq=$%.2f",
               reason, n, total_pnl, g_total_wins, g_total_trades, equity);

   ArrayResize(g_short_layers, 0);
   g_peak_short = 0;
   g_last_short_add = 0;
}

//+------------------------------------------------------------------+
//| 경과 봉 수 계산                                                     |
//+------------------------------------------------------------------+
int BarsElapsed(datetime from_time, datetime to_time)
{
   if(from_time == 0) return 9999;
   int secs = (int)(to_time - from_time);
   int bar_secs = PeriodSeconds(PERIOD_M5);
   return (bar_secs > 0) ? secs / bar_secs : 0;
}

//+------------------------------------------------------------------+
//| EA 재시작 시 기존 포지션 복구                                        |
//+------------------------------------------------------------------+
void RecoverExistingPositions()
{
   ArrayResize(g_long_layers, 0);
   ArrayResize(g_short_layers, 0);

   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;

      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double lots  = PositionGetDouble(POSITION_VOLUME);
      datetime time = (datetime)PositionGetInteger(POSITION_TIME);

      if(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
      {
         int idx = ArraySize(g_long_layers);
         ArrayResize(g_long_layers, idx + 1);
         g_long_layers[idx].entry  = entry;
         g_long_layers[idx].lots   = lots;
         g_long_layers[idx].ticket = ticket;
         g_long_layers[idx].time   = time;
         g_peak_long = MathMax(g_peak_long, iClose(_Symbol, PERIOD_M5, 1));
         PrintFormat("  롱 복구: L%d @ %.2f | %.2f lots", idx + 1, entry, lots);
      }
      else
      {
         int idx = ArraySize(g_short_layers);
         ArrayResize(g_short_layers, idx + 1);
         g_short_layers[idx].entry  = entry;
         g_short_layers[idx].lots   = lots;
         g_short_layers[idx].ticket = ticket;
         g_short_layers[idx].time   = time;
         double cur_close = iClose(_Symbol, PERIOD_M5, 1);
         g_peak_short = (g_peak_short == 0) ? cur_close : MathMin(g_peak_short, cur_close);
         PrintFormat("  숏 복구: L%d @ %.2f | %.2f lots", idx + 1, entry, lots);
      }
   }
}

//+------------------------------------------------------------------+
//| 동적 레버리지 — MDD 기반 사이징 자동 조절                              |
//| MDD 0~3%: 풀 사이징 (1.0)                                          |
//| MDD 3~5%: 선형 축소 (1.0 → 0.5)                                    |
//| MDD 5%+:  절반 사이징 (0.5)                                         |
//| 신고점 갱신 시 자동 복구                                               |
//+------------------------------------------------------------------+
void UpdateRiskScale()
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);

   // 피크 갱신 (신고점이면 리스크 풀 복구)
   if(equity > g_peak_equity)
   {
      g_peak_equity = equity;
      g_risk_scale  = 1.0;
      return;
   }

   // 현재 MDD% 계산
   if(g_peak_equity <= 0) return;
   double dd_pct = (g_peak_equity - equity) / g_peak_equity;

   // MDD 구간별 스케일 설정
   if(dd_pct < 0.03)
   {
      // 3% 미만: 풀 사이징
      g_risk_scale = 1.0;
   }
   else if(dd_pct < 0.05)
   {
      // 3~5%: 1.0 → 0.5 선형 축소
      g_risk_scale = 1.0 - (dd_pct - 0.03) / 0.02 * 0.5;
   }
   else
   {
      // 5%+: 절반 고정
      g_risk_scale = 0.5;
   }
}

//+------------------------------------------------------------------+
//| 차트 코멘트 (현재 상태 HUD)                                         |
//+------------------------------------------------------------------+
void UpdateChartComment()
{
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double profit  = AccountInfoDouble(ACCOUNT_PROFIT);

   int ln = ArraySize(g_long_layers);
   int sn = ArraySize(g_short_layers);

   string txt = "";
   txt += "=== ORCA Oil v1 ===\n";
   txt += StringFormat("Equity: $%.2f | Balance: $%.2f\n", equity, balance);
   double dd_pct = (g_peak_equity > 0) ? (g_peak_equity - equity) / g_peak_equity * 100 : 0;
   txt += StringFormat("Floating: $%.2f\n", profit);
   txt += StringFormat("MDD: %.1f%% | Risk: %.0f%%\n", dd_pct, g_risk_scale * 100);
   txt += StringFormat("W/T: %d/%d (%.0f%%)\n",
          g_total_wins, g_total_trades,
          g_total_trades > 0 ? (double)g_total_wins / g_total_trades * 100 : 0);
   txt += "---\n";

   if(ln > 0)
   {
      double avg = GetAvgEntry(g_long_layers);
      double total_lots = 0;
      for(int i = 0; i < ln; i++) total_lots += g_long_layers[i].lots;
      txt += StringFormat("LONG %d layers | avg=%.2f | %.2f lots\n", ln, avg, total_lots);
      txt += StringFormat("  peak=%.2f trail=%.2f\n", g_peak_long, g_peak_long * (1 - InpTrailPct));
   }
   if(sn > 0)
   {
      double avg = GetAvgEntry(g_short_layers);
      double total_lots = 0;
      for(int i = 0; i < sn; i++) total_lots += g_short_layers[i].lots;
      txt += StringFormat("SHORT %d layers | avg=%.2f | %.2f lots\n", sn, avg, total_lots);
      txt += StringFormat("  peak=%.2f trail=%.2f\n", g_peak_short, g_peak_short * (1 + InpTrailPct));
   }
   if(ln == 0 && sn == 0)
      txt += "Waiting for breakout...\n";

   Comment(txt);
}
//+------------------------------------------------------------------+
