//+------------------------------------------------------------------+
//|                                    LowVolBigCandleReversion.mq5   |
//|                                                                    |
//| Strategy (backtested in Python against this account's MT5 history |
//| before being ported here):                                        |
//|                                                                    |
//|   On the M15 timeframe, when a just-CLOSED candle's body is       |
//|   >= RelSizeThreshold times its own 14-bar average range, AND its |
//|   tick volume sits in the bottom third of its recent rolling      |
//|   volume distribution (a big move made on thin participation),   |
//|   trade AGAINST that candle expecting reversion.                  |
//|                                                                    |
//|   Signals whose candle opened during the 00:00-01:00 UTC          |
//|   rollover hour are skipped (spread widens 20-70x in that hour on |
//|   this feed, which was shown to destroy the edge). This is        |
//|   evaluated in true UTC, not raw broker server time, because the  |
//|   broker's server-clock offset from UTC can itself shift (DST     |
//|   changes on the server side) -- a server-time window would then  |
//|   silently drift away from the actual widening event.             |
//|   Live/demo: the UTC offset is measured dynamically every check   |
//|   (TimeCurrent()-TimeGMT()), so it self-adjusts to broker DST      |
//|   shifts automatically. Inside the Strategy Tester TimeGMT()      |
//|   always equals TimeCurrent() (a documented MQL5 tester            |
//|   limitation), so that path instead uses the manual                |
//|   BrokerUTCOffsetHoursForTesting input below.                     |
//|                                                                    |
//|   Every qualifying signal opens ONE TRADE PER ENABLED TARGET/STOP  |
//|   CONFIG (tight / medium / wide), each tagged with this EA's name, |
//|   the symbol, and the config in BOTH the magic number and the     |
//|   order comment, so every fill can be traced back in the logs and |
//|   in trade history to exactly which rule produced it.             |
//+------------------------------------------------------------------+
#property copyright "Backtested per-user research session"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

//================= Inputs =================
input string EAName                 = "LowVolBigCandleReversion";
input int    MagicBase              = 774411;   // trades use MagicBase + symbolIndex*10 + configIndex

input double RelSizeThreshold       = 1.0;      // |body| / avg(range,14) must be >= this
input int    AvgLookback            = 14;       // bars for the range & volume averages
input int    LowVolLookbackBars     = 1000;     // rolling window used to estimate the low-volume cutoff
input double LowVolPercentile       = 0.3333;   // bottom third = "low relative volume"
input int    MinHistoryBars         = 60;       // don't trade until the rolling window has at least this many samples

// NOTE: MT5 bar/tick timestamps (both in MQL5's iTime/CopyRates and in the
// Python MetaTrader5 module used to research this) are BROKER SERVER TIME,
// not true UTC -- verified empirically (this account's server runs UTC+3).
// The rollover filter below converts each bar's server timestamp to true
// UTC before checking the hour, rather than trusting the broker's raw
// server hour, because the broker can shift its own server-UTC offset
// (DST) without warning. Live/demo trading measures that offset itself
// each check via TimeCurrent()-TimeGMT(); the Strategy Tester cannot do
// the same (TimeGMT() is pinned to TimeCurrent() in tester), so backtests
// use BrokerUTCOffsetHoursForTesting as a manual stand-in instead.
input int    RolloverHourStartUTC   = 0;        // inclusive, true UTC
input int    RolloverHourEndUTC     = 1;        // exclusive -> skips [0:00,1:00) UTC
input int    BrokerUTCOffsetHoursForTesting = 3; // broker server time = UTC + this, USED ONLY inside the Strategy Tester (TimeGMT() can't reflect a real broker offset there); ignored live/demo

input bool   TradeEURUSD            = true;
input bool   TradeGBPUSD            = true;
input bool   TradeXAUUSD            = true;

input bool   UseConfig_Tight        = true;     // FX 3 / 3 pips   | XAUUSD $1.5 / $1.5
input bool   UseConfig_Medium       = true;     // FX 3 / 6 pips   | XAUUSD $1.5 / $3
input bool   UseConfig_Wide         = true;     // FX 5 / 10 pips  | XAUUSD $2.5 / $5

input double RiskPercent             = 1.0;     // % of account balance risked per ticket (each config's own stop distance)
input double MaxLotCap               = 1.0;     // hard ceiling on any single ticket, regardless of the broker's actual max lot
input int    MaxConcurrentPerSymbol = 12;       // 0 = unlimited; safety cap on simultaneously open trades per symbol
input int    TimerSeconds           = 5;        // how often (seconds) we poll for a new closed M15 bar

CTrade trade;

#define EA_TAG "[" + EAName + "]"

//================= Per-symbol configuration =================
struct ConfigSpec
  {
   string tag;      // "T" tight, "M" medium, "W" wide -- used in the order comment
   double target;   // in "units" (pips for FX, raw dollars for XAUUSD)
   double stop;
  };

struct SymbolSpec
  {
   string      name;
   double      unit;         // price offset per 1.0 "unit": FX = 0.0001 (a pip), XAUUSD = 1.0 (a dollar)
   ConfigSpec  cfg[3];
   datetime    lastBarTime;  // time of bar[0] last seen -- used to detect a new closed bar
   double      relVolBuf[];  // rolling history of rel_vol readings, oldest first, capped at LowVolLookbackBars
  };

SymbolSpec g_sym[3];
int        g_symCount = 0;

//+------------------------------------------------------------------+
//| Register a symbol to trade with its per-config target/stop       |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| GlobalVariable key for a symbol's last-acted-on bar. GlobalVariable |
//| persists across EA restarts (and terminal restarts) unlike a plain |
//| global, which resets to 0 on every reinit -- without this, a       |
//| restart re-evaluates the still-most-recent closed bar as if it     |
//| were new and re-fires a signal that already traded. Confirmed live |
//| 2026-08-21: 4 container restarts duplicated the same EURUSD/GBPUSD |
//| signal 3x each (9 tickets instead of 3, per symbol).                |
//+------------------------------------------------------------------+
string LastBarVarName(string sym) { return "LowVolBigCandleReversion_lastBar_" + sym; }

void AddSymbol(string name, double unit,
                double t1, double s1,
                double t2, double s2,
                double t3, double s3)
  {
   int idx = g_symCount;
   g_sym[idx].name = name;
   g_sym[idx].unit = unit;
   g_sym[idx].cfg[0].tag = "T"; g_sym[idx].cfg[0].target = t1; g_sym[idx].cfg[0].stop = s1;
   g_sym[idx].cfg[1].tag = "M"; g_sym[idx].cfg[1].target = t2; g_sym[idx].cfg[1].stop = s2;
   g_sym[idx].cfg[2].tag = "W"; g_sym[idx].cfg[2].target = t3; g_sym[idx].cfg[2].stop = s3;
   string gvar = LastBarVarName(name);
   g_sym[idx].lastBarTime = GlobalVariableCheck(gvar) ? (datetime)GlobalVariableGet(gvar) : 0;
   ArrayResize(g_sym[idx].relVolBuf, 0);
   g_symCount++;
  }

//+------------------------------------------------------------------+
//| Broker server time -> true UTC offset, in seconds. Live/demo:    |
//| measured fresh every call so it self-adjusts to broker DST       |
//| shifts. In the tester TimeGMT() is pinned to TimeCurrent() (a     |
//| documented MQL5 limitation), so fall back to the manual input.   |
//+------------------------------------------------------------------+
long GetServerUTCOffsetSeconds()
  {
   if(MQLInfoInteger(MQL_TESTER))
      return (long)BrokerUTCOffsetHoursForTesting * 3600;
   return (long)TimeCurrent() - (long)TimeGMT();
  }

//+------------------------------------------------------------------+
//| Is this bar's timestamp inside the rollover window, evaluated in |
//| true UTC (see note by the inputs above for why raw broker server |
//| time is not trusted directly).                                   |
//+------------------------------------------------------------------+
bool IsInRolloverWindowUTC(datetime serverTime)
  {
   datetime utcTime = (datetime)((long)serverTime - GetServerUTCOffsetSeconds());
   MqlDateTime dt;
   TimeToStruct(utcTime, dt);
   int h = dt.hour;
   if(RolloverHourStartUTC <= RolloverHourEndUTC)
      return (h >= RolloverHourStartUTC && h < RolloverHourEndUTC);
   else
      return (h >= RolloverHourStartUTC || h < RolloverHourEndUTC); // wrap-around window, just in case
  }

//+------------------------------------------------------------------+
//| Compute body/range/volume stats for the bar at shift `sh`,       |
//| using the AvgLookback bars ending at (and including) that bar -- |
//| matches a trailing rolling(14) average that includes the current |
//| bar, as used in the backtest.                                    |
//+------------------------------------------------------------------+
bool ComputeBarStats(string sym, int sh, double &relSize, double &relVol,
                      bool &isUp, double &closePrice, datetime &barTime)
  {
   MqlRates r[];
   int copied = CopyRates(sym, PERIOD_M15, sh, AvgLookback, r);
   if(copied < AvgLookback)
      return false; // not enough history yet

   double sumRange = 0.0, sumVol = 0.0;
   for(int k = 0; k < AvgLookback; k++)
     {
      sumRange += (r[k].high - r[k].low);
      sumVol   += (double)r[k].tick_volume;
     }
   double avgRange = sumRange / AvgLookback;
   double avgVol   = sumVol   / AvgLookback;
   if(avgRange <= 0.0 || avgVol <= 0.0)
      return false;

   int last = AvgLookback - 1; // the bar at shift `sh` itself (array is oldest -> newest)
   double body = r[last].close - r[last].open;
   if(body == 0.0)
      return false; // undefined direction, matches the backtest's doji filter

   relSize    = MathAbs(body) / avgRange;
   relVol     = (double)r[last].tick_volume / avgVol;
   isUp       = (body > 0.0);
   closePrice = r[last].close;
   barTime    = r[last].time;
   return true;
  }

//+------------------------------------------------------------------+
//| Nearest-rank percentile of a buffer (ascending)                  |
//+------------------------------------------------------------------+
double RollingPercentile(double &buf[], double pct)
  {
   int n = ArraySize(buf);
   if(n == 0)
      return 0.0;
   double tmp[];
   ArrayResize(tmp, n);
   ArrayCopy(tmp, buf);
   ArraySort(tmp);
   int idx = (int)MathFloor(pct * (n - 1));
   if(idx < 0) idx = 0;
   if(idx > n - 1) idx = n - 1;
   return tmp[idx];
  }

//+------------------------------------------------------------------+
//| Push a new rel_vol reading, trimming to LowVolLookbackBars        |
//+------------------------------------------------------------------+
void PushRelVol(int symIdx, double val)
  {
   int n = ArraySize(g_sym[symIdx].relVolBuf);
   ArrayResize(g_sym[symIdx].relVolBuf, n + 1);
   g_sym[symIdx].relVolBuf[n] = val;
   n++;
   if(n > LowVolLookbackBars)
     {
      for(int k = 0; k < n - 1; k++)
         g_sym[symIdx].relVolBuf[k] = g_sym[symIdx].relVolBuf[k + 1];
      ArrayResize(g_sym[symIdx].relVolBuf, LowVolLookbackBars);
     }
  }

//+------------------------------------------------------------------+
//| Preload the rolling rel_vol history from existing chart history  |
//| so the EA isn't "cold" the moment it's attached. Stops at shift  |
//| 2, NOT 1 -- shift 1 (the most recently closed bar) is left for   |
//| the first live ProcessSymbol() call to evaluate and push itself, |
//| so that bar's own signal check stays strictly causal (its rel_vol|
//| reading must not already be sitting in the window it's compared  |
//| against, and it must not end up double-counted in the buffer).   |
//+------------------------------------------------------------------+
void PrimeHistory(int idx)
  {
   string sym = g_sym[idx].name;
   int primed = 0;
   for(int sh = LowVolLookbackBars; sh >= 2; sh--)
     {
      double relSize, relVol, closePrice;
      bool isUp;
      datetime barTime;
      if(ComputeBarStats(sym, sh, relSize, relVol, isUp, closePrice, barTime))
        {
         PushRelVol(idx, relVol);
         primed++;
        }
     }
   Print(EA_TAG, " primed ", primed, " historical bars of rolling volume context for ", sym);
  }

//+------------------------------------------------------------------+
//| Risk-based lot size for one ticket: RiskPercent of account        |
//| balance divided by this config's own stop distance. Never skips a |
//| trade for sizing reasons -- clamped into the broker's tradeable   |
//| range instead:                                                    |
//|   - below the broker's SYMBOL_VOLUME_MIN -> use SYMBOL_VOLUME_MIN |
//|   - above min(SYMBOL_VOLUME_MAX, MaxLotCap) -> use that cap       |
//|   - otherwise the calculated size is correct as-is                |
//+------------------------------------------------------------------+
double CalcRiskLot(string sym, double stopDistPrice)
  {
   double tickValue    = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize     = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double contractSize = SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE);
   double minLot       = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot       = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double step         = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   double moneyPerPriceUnitPerLot = contractSize;
   if(tickValue > 0.0 && tickSize > 0.0)
      moneyPerPriceUnitPerLot = MathMax(tickValue / tickSize, contractSize);

   double balance     = AccountInfoDouble(ACCOUNT_BALANCE);
   double moneyToRisk = balance * (RiskPercent / 100.0);

   double lots = minLot;
   if(stopDistPrice > 0.0 && moneyPerPriceUnitPerLot > 0.0)
      lots = moneyToRisk / (stopDistPrice * moneyPerPriceUnitPerLot);

   if(step > 0.0)
      lots = MathFloor(lots / step) * step; // round down -- never risk more than budgeted

   double upperBound = MathMin(maxLot, MaxLotCap);
   lots = MathMax(minLot, MathMin(lots, upperBound));
   return lots;
  }

//+------------------------------------------------------------------+
//| Count this EA's currently open positions on a symbol              |
//+------------------------------------------------------------------+
int CountOpenPositions(string sym)
  {
   int count = 0;
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      long magic = PositionGetInteger(POSITION_MAGIC);
      if(magic < MagicBase || magic >= MagicBase + 30)
         continue; // not one of ours
      count++;
     }
   return count;
  }

//+------------------------------------------------------------------+
//| Open one trade per enabled config for a confirmed signal          |
//+------------------------------------------------------------------+
void OpenSignalTrades(int idx, string bias, datetime barTime)
  {
   string sym   = g_sym[idx].name;
   double unit  = g_sym[idx].unit;
   int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point  = SymbolInfoDouble(sym, SYMBOL_POINT);
   double ask    = SymbolInfoDouble(sym, SYMBOL_ASK);
   double bid    = SymbolInfoDouble(sym, SYMBOL_BID);
   double stopLevelPts = (double)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL) * point;

   bool useCfg[3];
   useCfg[0] = UseConfig_Tight;
   useCfg[1] = UseConfig_Medium;
   useCfg[2] = UseConfig_Wide;

   for(int c = 0; c < 3; c++)
     {
      if(!useCfg[c])
         continue;

      double targetDist = g_sym[idx].cfg[c].target * unit;
      double stopDist   = g_sym[idx].cfg[c].stop   * unit;

      if(MaxConcurrentPerSymbol > 0 && CountOpenPositions(sym) >= MaxConcurrentPerSymbol)
        {
         Print(EA_TAG, " ", sym, " ", g_sym[idx].cfg[c].tag, ": skipped, MaxConcurrentPerSymbol reached");
         continue;
        }

      if(stopDist <= stopLevelPts)
        {
         Print(EA_TAG, " ", sym, " ", g_sym[idx].cfg[c].tag, ": skipped, stop distance <= broker min stop level");
         continue;
        }

      int    magic   = MagicBase + idx * 10 + c;
      string comment = EAName + "|" + sym + "|" + g_sym[idx].cfg[c].tag;
      double lot     = CalcRiskLot(sym, stopDist);

      trade.SetExpertMagicNumber(magic);

      bool ok;
      if(bias == "long")
        {
         double sl = NormalizeDouble(ask - stopDist,   digits);
         double tp = NormalizeDouble(ask + targetDist, digits);
         ok = trade.Buy(lot, sym, 0.0, sl, tp, comment);
        }
      else
        {
         double sl = NormalizeDouble(bid + stopDist,   digits);
         double tp = NormalizeDouble(bid - targetDist, digits);
         ok = trade.Sell(lot, sym, 0.0, sl, tp, comment);
        }

      if(!ok)
         Print(EA_TAG, " ", sym, " ", g_sym[idx].cfg[c].tag, " order FAILED lot=", lot, " retcode=", trade.ResultRetcode(),
               " (", trade.ResultRetcodeDescription(), ") signalBar=", TimeToString(barTime));
      else
         Print(EA_TAG, " ", sym, " ", g_sym[idx].cfg[c].tag, " order OK ticket=", trade.ResultOrder(),
               " lot=", lot, " bias=", bias, " signalBar=", TimeToString(barTime), " comment=", comment);
     }
  }

//+------------------------------------------------------------------+
//| Check one symbol for a freshly closed M15 bar and act on it       |
//+------------------------------------------------------------------+
void ProcessSymbol(int idx)
  {
   string sym = g_sym[idx].name;

   datetime t0 = iTime(sym, PERIOD_M15, 0);
   if(t0 == 0)
      return; // no data yet
   if(t0 == g_sym[idx].lastBarTime)
      return; // still the same forming bar, nothing new closed
   g_sym[idx].lastBarTime = t0;
   GlobalVariableSet(LastBarVarName(sym), (double)t0);

   double relSize, relVol, closePrice;
   bool isUp;
   datetime barTime;
   if(!ComputeBarStats(sym, 1, relSize, relVol, isUp, closePrice, barTime))
      return;

   // Threshold uses history strictly BEFORE this bar (causal), then this
   // bar's own reading is folded into the rolling window for next time.
   double threshold = RollingPercentile(g_sym[idx].relVolBuf, LowVolPercentile);
   bool   haveEnoughHistory = ArraySize(g_sym[idx].relVolBuf) >= MinHistoryBars;
   PushRelVol(idx, relVol);

   if(!haveEnoughHistory)
      return;
   if(relSize < RelSizeThreshold)
      return;
   if(relVol > threshold)
      return;
   if(IsInRolloverWindowUTC(barTime))
     {
      Print(EA_TAG, " ", sym, " signal at ", TimeToString(barTime), " (broker server time; UTC rollover hour) -- skipped");
      return;
     }

   string bias = isUp ? "short" : "long"; // trade AGAINST the signal candle
   Print(EA_TAG, " ", sym, " SIGNAL bar=", TimeToString(barTime), " isUp=", isUp, " relSize=",
         DoubleToString(relSize, 3), " relVol=", DoubleToString(relVol, 3),
         " threshold=", DoubleToString(threshold, 3), " -> bias=", bias);
   OpenSignalTrades(idx, bias, barTime);
  }

//+------------------------------------------------------------------+
//| Expert initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_symCount = 0;

   if(TradeEURUSD) AddSymbol("EURUSD", 0.0001, 3, 3, 3, 6, 5, 10);
   if(TradeGBPUSD) AddSymbol("GBPUSD", 0.0001, 3, 3, 3, 6, 5, 10);
   if(TradeXAUUSD) AddSymbol("XAUUSD", 1.0,    1.5, 1.5, 1.5, 3, 2.5, 5);

   if(g_symCount == 0)
     {
      Print(EA_TAG, " no symbols enabled -- nothing to do.");
      return(INIT_FAILED);
     }

   for(int i = 0; i < g_symCount; i++)
     {
      if(!SymbolSelect(g_sym[i].name, true))
         Print(EA_TAG, " WARNING: could not select ", g_sym[i].name, " in Market Watch");
      PrimeHistory(i);
     }

   trade.SetExpertMagicNumber(MagicBase);
   trade.SetDeviationInPoints(20);

   EventSetTimer(MathMax(1, TimerSeconds));
   Print(EA_TAG, " initialized on ", g_symCount, " symbol(s). magicBase=", MagicBase);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

//+------------------------------------------------------------------+
//| Timer -- primary driver, works for symbols other than the chart   |
//| symbol too (needed since this EA manages several instruments).    |
//+------------------------------------------------------------------+
void OnTimer()
  {
   for(int i = 0; i < g_symCount; i++)
      ProcessSymbol(i);
  }

//+------------------------------------------------------------------+
//| Tick -- redundant safety net so a new bar is never missed while   |
//| the chart symbol is actively ticking (harmless duplicate check,   |
//| ProcessSymbol is idempotent within the same bar).                 |
//+------------------------------------------------------------------+
void OnTick()
  {
   for(int i = 0; i < g_symCount; i++)
      ProcessSymbol(i);
  }
//+------------------------------------------------------------------+
