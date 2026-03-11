//+------------------------------------------------------------------+
//|                                                  ORCA_Multi.mq5  |
//|              ORCA 멀티심볼 마스터 EA — 슬라임 동적 배분             |
//|   심볼: 금,은,유달,나스닥,오일 (파라미터로 변경 가능)                 |
//+------------------------------------------------------------------+
#property copyright "ORCA Multi — Slime Allocation"
#property version   "1.02"
#property description "ORCA 멀티심볼 슬라임 배분 마스터 EA"
#include <Trade\Trade.mqh>

// ═══════════════════════════════════════════════════════════════════
// 입력 파라미터
// ═══════════════════════════════════════════════════════════════════

input group "=== 심볼 설정 ==="
input string InpSymbols      = "XAUUSD,XAGUSD,EURUSD,NAS100,USOIL"; // 금,은,유달,나스닥,오일

input group "=== ORCA DNA ==="
input double InpStepPct      = 0.0045;  // 돌파 기준 0.45%
input int    InpLookback     = 241;     // 룩백 241봉 (~20시간, M5)
input int    InpCooldown     = 7;       // 피라미딩 쿨다운 봉수
input int    InpMaxLayers    = 4;       // 피라미딩 최대 단수
input double InpLayer1SL     = 0.0223; // 1차 SL 2.23%
input double InpTrailPct     = 0.001;  // 트레일링 0.1%
input int    InpTrailAfter   = 1;      // N단부터 트레일링
input int    InpATR_Period   = 42;     // ATR 기간
input double InpATR_Mult     = 19.7;  // ATR 익절 배수
input int    InpSpreadMax    = 80;     // 최대 스프레드 (포인트)
input bool   InpFridayExit   = true;  // 금요일 강제 청산

input group "=== 자본 배분 ==="
input double InpTotalRisk    = 0.03;  // 전체 자본의 3%를 총 리스크로 (레버 100:1 기준)
input int    InpSlimeWindow  = 20;    // 슬라임 샤프 계산 윈도우
input double InpSlimeDecay   = 0.05; // 슬라임 D값 감쇠
input double InpMinAlloc     = 0.05; // 심볼당 최소 배분 5%
input double InpMaxAlloc     = 0.40; // 심볼당 최대 배분 40%

input group "=== 기타 ==="
input int    InpMagicBase    = 20260311; // 매직넘버 베이스
input int    InpSessionStart = 8;        // 진입 시작 시간 (서버시간)
input int    InpSessionEnd   = 20;       // 진입 종료 시간

// ═══════════════════════════════════════════════════════════════════
// 상수 & 전역
// ═══════════════════════════════════════════════════════════════════

#define MAX_SYMS   10
#define MAX_LAYERS  4
#define HIST_SIZE  20

// 심볼별 레이어 배열 (구조체 대신 평면 배열 — MQL5 포인터 이슈 회피)
int      g_sym_count = 0;
string   g_sym[MAX_SYMS];
int      g_magic[MAX_SYMS];

// 롱 레이어
double   g_le[MAX_SYMS][MAX_LAYERS];   // 진입가
double   g_ll[MAX_SYMS][MAX_LAYERS];   // 로트
ulong    g_lt[MAX_SYMS][MAX_LAYERS];   // 티켓
int      g_lc[MAX_SYMS];               // 레이어 수
double   g_pk_long[MAX_SYMS];          // 트레일 피크
datetime g_last_ladd[MAX_SYMS];        // 마지막 롱 추가 시간

// 숏 레이어
double   g_se[MAX_SYMS][MAX_LAYERS];
double   g_sl[MAX_SYMS][MAX_LAYERS];
ulong    g_st[MAX_SYMS][MAX_LAYERS];
int      g_sc[MAX_SYMS];
double   g_pk_short[MAX_SYMS];
datetime g_last_sadd[MAX_SYMS];

// 봉 감지
datetime g_last_bar[MAX_SYMS];

// 슬라임 배분
double   g_d_val[MAX_SYMS];
double   g_alloc[MAX_SYMS];
double   g_hist[MAX_SYMS][HIST_SIZE];
int      g_hist_cnt[MAX_SYMS];

// 통계
int      g_trades[MAX_SYMS];
int      g_wins[MAX_SYMS];
double   g_pnl[MAX_SYMS];

CTrade   g_trade;

// ═══════════════════════════════════════════════════════════════════
// 초기화
// ═══════════════════════════════════════════════════════════════════

int OnInit()
{
   string parts[];
   int cnt = StringSplit(InpSymbols, ',', parts);
   g_sym_count = 0;

   for(int i = 0; i < cnt && i < MAX_SYMS; i++)
   {
      string s = parts[i];
      StringTrimRight(s); StringTrimLeft(s);
      if(s == "") continue;
      if(!SymbolSelect(s, true))
      {
         PrintFormat("  ⚠️ %s: 심볼 없음 — 스킵", s);
         continue;
      }
      int x = g_sym_count;
      g_sym[x]       = s;
      g_magic[x]     = InpMagicBase + x;
      g_lc[x]        = 0;
      g_sc[x]        = 0;
      g_pk_long[x]   = 0;
      g_pk_short[x]  = 0;
      g_last_ladd[x] = 0;
      g_last_sadd[x] = 0;
      g_last_bar[x]  = 0;
      g_d_val[x]     = 1.0;
      g_alloc[x]     = 1.0 / cnt;
      g_hist_cnt[x]  = 0;
      g_trades[x]    = 0;
      g_wins[x]      = 0;
      g_pnl[x]       = 0;
      for(int h = 0; h < HIST_SIZE; h++) g_hist[x][h] = 0.0;
      g_sym_count++;
      PrintFormat("  ✅ %s 등록 (magic=%d)", s, g_magic[x]);
   }

   if(g_sym_count == 0) { Print("❌ 유효 심볼 없음"); return INIT_FAILED; }

   for(int i = 0; i < g_sym_count; i++) RecoverPositions(i);
   UpdateSlimeAlloc();

   PrintFormat("ORCA Multi v1.02 — %d개 심볼 등록", g_sym_count);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   for(int i = 0; i < g_sym_count; i++)
      PrintFormat("  %s: %d거래 WR%.0f%% PnL=$%.2f alloc=%.0f%%",
         g_sym[i], g_trades[i],
         g_trades[i] > 0 ? (double)g_wins[i]/g_trades[i]*100 : 0,
         g_pnl[i], g_alloc[i]*100);
}

// ═══════════════════════════════════════════════════════════════════
// 메인 틱
// ═══════════════════════════════════════════════════════════════════

void OnTick()
{
   for(int i = 0; i < g_sym_count; i++)
   {
      datetime cur = iTime(g_sym[i], PERIOD_M5, 0);
      if(cur == g_last_bar[i]) continue;
      g_last_bar[i] = cur;

      CheckExitLong(i);
      CheckExitShort(i);
      CheckPyramidAdd(i);
      CheckNewEntry(i);
   }
   if(InpFridayExit) CheckFridayClose();
   UpdateComment();
}

// ═══════════════════════════════════════════════════════════════════
// 청산 — 롱
// ═══════════════════════════════════════════════════════════════════

void CheckExitLong(int i)
{
   if(g_lc[i] == 0) return;
   double close = iClose(g_sym[i], PERIOD_M5, 1);

   // A) SL1
   if(close <= g_le[i][0] * (1.0 - InpLayer1SL))
      { CloseAllLong(i, "SL1", false); return; }

   // B) BE_AVG
   if(g_lc[i] >= 2 && close <= AvgLong(i))
      { CloseAllLong(i, "BE_AVG", false); return; }

   // C) TRAIL
   if(g_lc[i] >= InpTrailAfter)
   {
      if(close > g_pk_long[i]) g_pk_long[i] = close;
      if(close <= g_pk_long[i] * (1.0 - InpTrailPct))
         { CloseAllLong(i, "TRAIL", close > g_le[i][0]); return; }
   }

   // D) TARGET
   double atr = ATRPct(g_sym[i]);
   if(atr > 0 && (close - g_le[i][0]) / g_le[i][0] >= atr * InpATR_Mult)
      { CloseAllLong(i, "TARGET", true); return; }

   if(close > g_pk_long[i] || g_pk_long[i] == 0) g_pk_long[i] = close;
}

// ═══════════════════════════════════════════════════════════════════
// 청산 — 숏
// ═══════════════════════════════════════════════════════════════════

void CheckExitShort(int i)
{
   if(g_sc[i] == 0) return;
   double close = iClose(g_sym[i], PERIOD_M5, 1);

   // A) SL1
   if(close >= g_se[i][0] * (1.0 + InpLayer1SL))
      { CloseAllShort(i, "SL1", false); return; }

   // B) BE_AVG
   if(g_sc[i] >= 2 && close >= AvgShort(i))
      { CloseAllShort(i, "BE_AVG", false); return; }

   // C) TRAIL
   if(g_sc[i] >= InpTrailAfter)
   {
      if(close < g_pk_short[i] || g_pk_short[i] == 0) g_pk_short[i] = close;
      if(close >= g_pk_short[i] * (1.0 + InpTrailPct))
         { CloseAllShort(i, "TRAIL", close < g_se[i][0]); return; }
   }

   // D) TARGET
   double atr = ATRPct(g_sym[i]);
   if(atr > 0 && (g_se[i][0] - close) / g_se[i][0] >= atr * InpATR_Mult)
      { CloseAllShort(i, "TARGET", true); return; }

   if(close < g_pk_short[i] || g_pk_short[i] == 0) g_pk_short[i] = close;
}

// ═══════════════════════════════════════════════════════════════════
// 피라미딩
// ═══════════════════════════════════════════════════════════════════

void CheckPyramidAdd(int i)
{
   string sym   = g_sym[i];
   double close = iClose(sym, PERIOD_M5, 1);
   datetime now = iTime(sym, PERIOD_M5, 0);

   // 롱 피라미딩
   if(g_lc[i] > 0 && g_lc[i] < InpMaxLayers)
   {
      if(BarsElapsed(g_last_ladd[i], now, sym) >= InpCooldown)
      {
         int next   = g_lc[i] + 1;
         double req = InpStepPct * next;
         double act = (close - g_le[i][0]) / g_le[i][0];
         if(act >= req)
         {
            if(next == InpMaxLayers && close <= AvgLong(i) * 1.005) return;
            double lots = CalcLots(i);
            if(lots > 0) { OpenLong(i, lots, "PYR_L" + IntegerToString(next)); g_last_ladd[i] = now; }
         }
      }
   }

   // 숏 피라미딩
   if(g_sc[i] > 0 && g_sc[i] < InpMaxLayers)
   {
      if(BarsElapsed(g_last_sadd[i], now, sym) >= InpCooldown)
      {
         int next   = g_sc[i] + 1;
         double req = InpStepPct * next;
         double act = (g_se[i][0] - close) / g_se[i][0];
         if(act >= req)
         {
            if(next == InpMaxLayers && close >= AvgShort(i) * 0.995) return;
            double lots = CalcLots(i);
            if(lots > 0) { OpenShort(i, lots, "PYR_S" + IntegerToString(next)); g_last_sadd[i] = now; }
         }
      }
   }
}

// ═══════════════════════════════════════════════════════════════════
// 신규 진입
// ═══════════════════════════════════════════════════════════════════

void CheckNewEntry(int i)
{
   // 어느 방향이든 포지션 있으면 신규 진입 차단 (롱+숏 동시 방지)
   if(g_lc[i] > 0 || g_sc[i] > 0) return;

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.hour < InpSessionStart || dt.hour >= InpSessionEnd) return;
   if(dt.day_of_week == 5 && dt.hour >= 19) return;

   string sym = g_sym[i];
   if(SymbolInfoInteger(sym, SYMBOL_SPREAD) > InpSpreadMax) return;
   double ml = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   if(ml > 0 && ml < 150) return;

   double close = iClose(sym, PERIOD_M5, 1);
   double lo    = FindLow(sym);
   double hi    = FindHigh(sym);
   double gain  = (lo > 0) ? (close - lo) / lo  : 0;
   double drop  = (hi > 0) ? (hi - close) / hi  : 0;

   bool long_sig  = (gain >= InpStepPct) && (g_lc[i] == 0);
   bool short_sig = (drop >= InpStepPct) && (g_sc[i] == 0);
   if(long_sig && short_sig) { if(gain >= drop) short_sig = false; else long_sig = false; }
   if(!long_sig && !short_sig) return;

   double lots = CalcLots(i);
   if(lots <= 0) return;

   if(long_sig)
   {
      OpenLong(i, lots, "ENTRY_L1");
      g_last_ladd[i] = iTime(sym, PERIOD_M5, 0);
      g_pk_long[i]   = close;
   }
   else
   {
      OpenShort(i, lots, "ENTRY_S1");
      g_last_sadd[i] = iTime(sym, PERIOD_M5, 0);
      g_pk_short[i]  = close;
   }
}

// ═══════════════════════════════════════════════════════════════════
// 금요일 강제 청산
// ═══════════════════════════════════════════════════════════════════

void CheckFridayClose()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.day_of_week != 5 || dt.hour < 21) return;
   for(int i = 0; i < g_sym_count; i++)
   {
      if(g_lc[i] > 0) CloseAllLong(i,  "FRIDAY", false);
      if(g_sc[i] > 0) CloseAllShort(i, "FRIDAY", false);
   }
}

// ═══════════════════════════════════════════════════════════════════
// 주문 실행
// ═══════════════════════════════════════════════════════════════════

void OpenLong(int i, double lots, string comment)
{
   string sym = g_sym[i];
   g_trade.SetExpertMagicNumber(g_magic[i]);
   g_trade.SetTypeFilling(ORDER_FILLING_IOC);
   if(g_trade.PositionOpen(sym, ORDER_TYPE_BUY, lots,
      SymbolInfoDouble(sym, SYMBOL_ASK), 0, 0, comment))
   {
      int n = g_lc[i];
      if(n < MAX_LAYERS)
      {
         g_le[i][n] = g_trade.ResultPrice();
         g_ll[i][n] = lots;
         g_lt[i][n] = g_trade.ResultOrder();
         g_lc[i]++;
         PrintFormat("  [%s] LONG L%d @ %.5f | %.2f lots | alloc=%.0f%%",
            sym, g_lc[i], g_le[i][n], lots, g_alloc[i]*100);
      }
   }
   else PrintFormat("  [%s] LONG 실패: %s", sym, g_trade.ResultComment());
}

void OpenShort(int i, double lots, string comment)
{
   string sym = g_sym[i];
   g_trade.SetExpertMagicNumber(g_magic[i]);
   g_trade.SetTypeFilling(ORDER_FILLING_IOC);
   if(g_trade.PositionOpen(sym, ORDER_TYPE_SELL, lots,
      SymbolInfoDouble(sym, SYMBOL_BID), 0, 0, comment))
   {
      int n = g_sc[i];
      if(n < MAX_LAYERS)
      {
         g_se[i][n] = g_trade.ResultPrice();
         g_sl[i][n] = lots;
         g_st[i][n] = g_trade.ResultOrder();
         g_sc[i]++;
         PrintFormat("  [%s] SHORT L%d @ %.5f | %.2f lots | alloc=%.0f%%",
            sym, g_sc[i], g_se[i][n], lots, g_alloc[i]*100);
      }
   }
   else PrintFormat("  [%s] SHORT 실패: %s", sym, g_trade.ResultComment());
}

// ═══════════════════════════════════════════════════════════════════
// 청산 실행
// ═══════════════════════════════════════════════════════════════════

void CloseAllLong(int i, string reason, bool is_win)
{
   double total = 0;
   int n = g_lc[i];
   g_trade.SetExpertMagicNumber(g_magic[i]);
   for(int j = PositionsTotal()-1; j >= 0; j--)
   {
      ulong ticket = PositionGetTicket(j);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL)  != g_sym[i]) continue;
      if(PositionGetInteger(POSITION_MAGIC)  != g_magic[i]) continue;
      if(PositionGetInteger(POSITION_TYPE)   != POSITION_TYPE_BUY) continue;
      total += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      g_trade.PositionClose(ticket);
   }
   g_trades[i]++; g_pnl[i] += total;
   if(is_win || total > 0) g_wins[i]++;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   AddHistory(i, eq > 0 ? total/eq : 0);
   UpdateSlimeAlloc();
   PrintFormat("  [%s] LONG [%s] %d층 PnL=$%.2f alloc→%.0f%%",
      g_sym[i], reason, n, total, g_alloc[i]*100);
   g_lc[i] = 0; g_pk_long[i] = 0; g_last_ladd[i] = 0;
}

void CloseAllShort(int i, string reason, bool is_win)
{
   double total = 0;
   int n = g_sc[i];
   g_trade.SetExpertMagicNumber(g_magic[i]);
   for(int j = PositionsTotal()-1; j >= 0; j--)
   {
      ulong ticket = PositionGetTicket(j);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL)  != g_sym[i]) continue;
      if(PositionGetInteger(POSITION_MAGIC)  != g_magic[i]) continue;
      if(PositionGetInteger(POSITION_TYPE)   != POSITION_TYPE_SELL) continue;
      total += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      g_trade.PositionClose(ticket);
   }
   g_trades[i]++; g_pnl[i] += total;
   if(is_win || total > 0) g_wins[i]++;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   AddHistory(i, eq > 0 ? total/eq : 0);
   UpdateSlimeAlloc();
   PrintFormat("  [%s] SHORT [%s] %d층 PnL=$%.2f alloc→%.0f%%",
      g_sym[i], reason, n, total, g_alloc[i]*100);
   g_sc[i] = 0; g_pk_short[i] = 0; g_last_sadd[i] = 0;
}

// ═══════════════════════════════════════════════════════════════════
// 슬라임 배분
// ═══════════════════════════════════════════════════════════════════

void UpdateSlimeAlloc()
{
   double total_d = 0;
   for(int i = 0; i < g_sym_count; i++)
   {
      int w = MathMin(g_hist_cnt[i], InpSlimeWindow);
      double q = 0;
      if(w >= 3)
      {
         double mean = 0;
         for(int j = 0; j < w; j++) mean += g_hist[i][j];
         mean /= w;
         double var = 0;
         for(int j = 0; j < w; j++) var += (g_hist[i][j]-mean)*(g_hist[i][j]-mean);
         double std = MathSqrt(var/w);
         q = (std > 0) ? MathMax(mean/std, 0.0) : 0.0;
      }
      g_d_val[i] = g_d_val[i] * (1.0 - InpSlimeDecay) + q;
      total_d += MathMax(g_d_val[i], InpMinAlloc);
   }
   double sum = 0;
   for(int i = 0; i < g_sym_count; i++)
   {
      g_alloc[i] = MathMin(MathMax(g_d_val[i], InpMinAlloc) / total_d, InpMaxAlloc);
      sum += g_alloc[i];
   }
   for(int i = 0; i < g_sym_count; i++) g_alloc[i] /= sum;
}

void AddHistory(int i, double pnl_pct)
{
   for(int j = HIST_SIZE-1; j > 0; j--) g_hist[i][j] = g_hist[i][j-1];
   g_hist[i][0] = pnl_pct;
   if(g_hist_cnt[i] < HIST_SIZE) g_hist_cnt[i]++;
}

// ═══════════════════════════════════════════════════════════════════
// 로트 계산
// ═══════════════════════════════════════════════════════════════════

double CalcLots(int i)
{
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(eq < 10) return 0;
   string sym = g_sym[i];
   double mpl = 0;
   if(!OrderCalcMargin(ORDER_TYPE_BUY, sym, 1.0,
      SymbolInfoDouble(sym, SYMBOL_ASK), mpl) || mpl <= 0) return 0;
   double raw  = eq * InpTotalRisk * g_alloc[i] / mpl;
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double mn   = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double mx   = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   raw = MathFloor(raw/step)*step;
   return NormalizeDouble(MathMax(mn, MathMin(mx, raw)), 2);
}

// ═══════════════════════════════════════════════════════════════════
// 유틸리티
// ═══════════════════════════════════════════════════════════════════

double ATRPct(string sym)
{
   double sum = 0;
   for(int i = 1; i <= InpATR_Period; i++)
   {
      double h = iHigh(sym, PERIOD_M5, i);
      double l = iLow(sym, PERIOD_M5, i);
      double p = iClose(sym, PERIOD_M5, i+1);
      sum += MathMax(h-l, MathMax(MathAbs(h-p), MathAbs(l-p)));
   }
   double price = iClose(sym, PERIOD_M5, 1);
   return (price > 0) ? (sum/InpATR_Period)/price : 0;
}

double FindLow(string sym)
{
   double mn = DBL_MAX;
   for(int i = 1; i <= InpLookback; i++) { double v = iLow(sym, PERIOD_M5, i); if(v < mn) mn = v; }
   return (mn < DBL_MAX) ? mn : 0;
}

double FindHigh(string sym)
{
   double mx = 0;
   for(int i = 1; i <= InpLookback; i++) { double v = iHigh(sym, PERIOD_M5, i); if(v > mx) mx = v; }
   return mx;
}

double AvgLong(int i)
{
   double sv = 0, sl = 0;
   for(int j = 0; j < g_lc[i]; j++) { sv += g_le[i][j]*g_ll[i][j]; sl += g_ll[i][j]; }
   return (sl > 0) ? sv/sl : 0;
}

double AvgShort(int i)
{
   double sv = 0, sl = 0;
   for(int j = 0; j < g_sc[i]; j++) { sv += g_se[i][j]*g_sl[i][j]; sl += g_sl[i][j]; }
   return (sl > 0) ? sv/sl : 0;
}

int BarsElapsed(datetime from, datetime to, string sym)
{
   if(from == 0) return 9999;
   int secs = (int)(to - from);
   int bs   = PeriodSeconds(PERIOD_M5);
   return (bs > 0) ? secs/bs : 0;
}

void RecoverPositions(int i)
{
   g_lc[i] = 0; g_sc[i] = 0;
   string sym = g_sym[i];
   for(int j = 0; j < PositionsTotal(); j++)
   {
      ulong ticket = PositionGetTicket(j);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_magic[i]) continue;
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double lots  = PositionGetDouble(POSITION_VOLUME);
      if(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
      {
         int n = g_lc[i];
         if(n < MAX_LAYERS) { g_le[i][n]=entry; g_ll[i][n]=lots; g_lt[i][n]=ticket; g_lc[i]++; }
      }
      else
      {
         int n = g_sc[i];
         if(n < MAX_LAYERS) { g_se[i][n]=entry; g_sl[i][n]=lots; g_st[i][n]=ticket; g_sc[i]++; }
      }
   }
}

void UpdateComment()
{
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   double bl = AccountInfoDouble(ACCOUNT_BALANCE);
   string txt = StringFormat("=== ORCA Multi v1.02 ===\nEq:$%.2f Bal:$%.2f\n---\n", eq, bl);
   for(int i = 0; i < g_sym_count; i++)
   {
      string st = "";
      if(g_lc[i] > 0) st += StringFormat("L%d@%.4f ", g_lc[i], AvgLong(i));
      if(g_sc[i] > 0) st += StringFormat("S%d@%.4f ", g_sc[i], AvgShort(i));
      if(st == "") st = "대기";
      txt += StringFormat("%-8s %3.0f%%  %s\n", g_sym[i], g_alloc[i]*100, st);
   }
   Comment(txt);
}
//+------------------------------------------------------------------+
