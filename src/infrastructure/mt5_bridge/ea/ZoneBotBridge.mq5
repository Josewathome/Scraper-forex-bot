//+------------------------------------------------------------------+
//|  ZoneBotBridge.mq5 — ZoneBot ZeroMQ Data Bridge                 |
//|                                                                  |
//|  PURPOSE                                                         |
//|  Pushes market data from the MT5 terminal to the Python bot via  |
//|  ZeroMQ PUB socket.  Both the EA and the Python bot run inside   |
//|  the same Wine container — communication is pure loopback TCP,   |
//|  no Docker networking involved.                                  |
//|                                                                  |
//|  MESSAGES PUBLISHED                                              |
//|  Topic prefix  Payload (JSON)                                    |
//|  -----------  --------------------------------------------------  |
//|  TICK         {sym,bid,ask,spread,time}  — every OnTick()        |
//|  BAR_CLOSE    {sym,tf,open,high,low,close,vol,time} — on close   |
//|  BAR_OPEN     {sym,tf,time}  — when new bar opens                |
//|  TRADE        {event,ticket,sym,type,vol,price,sl,tp,profit}     |
//|  HEARTBEAT    {time} — every 5 seconds when no tick              |
//|                                                                  |
//|  DESIGN NOTE                                                     |
//|  We call libzmq.dll directly rather than via the mql-zmq wrapper |
//|  headers. The wrapper (dingmaotu/mql-zmq) uses char[]/uchar[]    |
//|  types that are incompatible with MT5 build 5800+. Since we only |
//|  need 6 functions (ctx_new, socket, bind, setsockopt_int, send,  |
//|  close/ctx_destroy), a direct import is simpler and guaranteed   |
//|  to compile on any MT5 build.                                    |
//|                                                                  |
//|  CONTAINER DEPLOYMENT                                            |
//|  Both MT5 and the Python bot run under Wine in the same          |
//|  container. No cross-container networking is needed.             |
//|  PUB binds on 127.0.0.1:5556 (loopback only).                   |
//|                                                                  |
//|  INSTALLATION (automated by override_start.sh)                  |
//|  1. libzmq.dll → MQL5/Libraries/                                 |
//|  2. This file → MQL5/Experts/                                    |
//|  3. Compiled by MetaEditor64.exe                                 |
//|  4. Auto-attached to GBPUSD H1 via AutoTrade.ini                 |
//+------------------------------------------------------------------+
#property copyright "ZoneBot"
#property version   "2.00"
#property strict

//── Direct libzmq.dll imports ────────────────────────────────────────
// We import only the 6 functions this EA actually calls.
// All use int/long/uchar[] types that are stable across all MT5 builds.
#import "libzmq.dll"
   long zmq_ctx_new();
   int  zmq_ctx_destroy(long context);
   long zmq_socket(long context, int type);
   int  zmq_close(long socket);
   int  zmq_bind(long socket, const uchar &endpoint[]);
   int  zmq_setsockopt(long socket, int option_name, const uchar &option_value[], int option_len);
   int  zmq_send(long socket, const uchar &buf[], int len, int flags);
#import

//── ZMQ constants ────────────────────────────────────────────────────
#define ZMQ_PUB       1
#define ZMQ_LINGER    17
#define ZMQ_SNDHWM    23

//── Input parameters ─────────────────────────────────────────────────
input string   PUB_ENDPOINT   = "tcp://127.0.0.1:5556";
input string   SYMBOLS_CSV    = "GBPUSD,XAUUSD,USDJPY,AUDUSD,USDCHF";
input int      HEARTBEAT_SECS = 5;

//── Internal state ────────────────────────────────────────────────────
long     g_ctx    = 0;
long     g_pub    = 0;
bool     g_bound  = false;

string   g_symbols[];
int      g_sym_count = 0;

string   g_bar_keys[];
datetime g_bar_last_open[];
int      g_bar_count = 0;

datetime g_last_tick_time    = 0;
datetime g_last_bind_attempt = 0;
int      g_bind_retry_secs   = 10;

//── Helper: send a string over ZMQ ────────────────────────────────────
void _ZmqSend(const string msg)
{
   if(g_pub == 0 || !g_bound) return;
   uchar buf[];
   int   len = StringToCharArray(msg, buf, 0, -1, CP_UTF8) - 1; // exclude null terminator
   if(len <= 0) return;
   zmq_send(g_pub, buf, len, 0);
}

//── Helper: convert string endpoint to uchar[] for zmq_bind ──────────
bool _ZmqBind(long sock, const string endpoint)
{
   uchar ep[];
   StringToCharArray(endpoint, ep, 0, -1, CP_UTF8);
   return (zmq_bind(sock, ep) == 0);
}

//── Helper: set integer socket option ─────────────────────────────────
void _ZmqSetOptInt(long sock, int opt, int val)
{
   uchar buf[4];
   buf[0] = (uchar)(val & 0xFF);
   buf[1] = (uchar)((val >> 8)  & 0xFF);
   buf[2] = (uchar)((val >> 16) & 0xFF);
   buf[3] = (uchar)((val >> 24) & 0xFF);
   zmq_setsockopt(sock, opt, buf, 4);
}

//+------------------------------------------------------------------+
//| Attempt ZMQ bind                                                 |
//+------------------------------------------------------------------+
void _TryBind()
{
   if(g_bound) return;

   datetime now = TimeCurrent();
   if(now - g_last_bind_attempt < g_bind_retry_secs) return;
   g_last_bind_attempt = now;

   if(g_ctx == 0)
   {
      g_ctx = zmq_ctx_new();
      if(g_ctx == 0) { Print("ZoneBotBridge: zmq_ctx_new failed"); return; }
   }

   if(g_pub == 0)
   {
      g_pub = zmq_socket(g_ctx, ZMQ_PUB);
      if(g_pub == 0) { Print("ZoneBotBridge: zmq_socket failed"); return; }
      _ZmqSetOptInt(g_pub, ZMQ_LINGER, 0);    // close immediately, no lingering
      _ZmqSetOptInt(g_pub, ZMQ_SNDHWM, 1000); // drop old messages when queue full
   }

   if(_ZmqBind(g_pub, PUB_ENDPOINT))
   {
      g_bound = true;
      Print("ZoneBotBridge: PUB bound on ", PUB_ENDPOINT);
   }
   else
   {
      Print("ZoneBotBridge: bind failed on ", PUB_ENDPOINT, " — retry in ", g_bind_retry_secs, "s");
   }
}

//+------------------------------------------------------------------+
//| Expert initialisation                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   g_sym_count = StringSplit(SYMBOLS_CSV, ',', g_symbols);
   if(g_sym_count == 0)
   {
      Print("ZoneBotBridge: no symbols configured");
      return INIT_FAILED;
   }

   for(int i = 0; i < g_sym_count; i++)
      SymbolSelect(g_symbols[i], true);

   // Initialise bar-state tracker for M1/M5/M30/H1/H4 on each symbol
   string tfs[5];
   tfs[0] = "M1"; tfs[1] = "M5"; tfs[2] = "M30"; tfs[3] = "H1"; tfs[4] = "H4";
   ENUM_TIMEFRAMES tf_enum[5];
   tf_enum[0] = PERIOD_M1; tf_enum[1] = PERIOD_M5; tf_enum[2] = PERIOD_M30;
   tf_enum[3] = PERIOD_H1; tf_enum[4] = PERIOD_H4;

   int nTF = 5;
   ArrayResize(g_bar_keys,      g_sym_count * nTF);
   ArrayResize(g_bar_last_open, g_sym_count * nTF);

   int slot = 0;
   for(int s = 0; s < g_sym_count; s++)
      for(int t = 0; t < nTF; t++)
      {
         g_bar_keys[slot]      = g_symbols[s] + "_" + tfs[t];
         g_bar_last_open[slot] = iTime(g_symbols[s], tf_enum[t], 0);
         slot++;
      }
   g_bar_count = slot;

   _TryBind();
   EventSetTimer(1);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_pub != 0) { zmq_close(g_pub);       g_pub = 0; }
   if(g_ctx != 0) { zmq_ctx_destroy(g_ctx); g_ctx = 0; }
   g_bound = false;
   Print("ZoneBotBridge: stopped (reason=", reason, ")");
}

//+------------------------------------------------------------------+
//| OnTick                                                           |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!g_bound) { _TryBind(); return; }

   g_last_tick_time = TimeCurrent();

   string tfs[5];
   tfs[0] = "M1"; tfs[1] = "M5"; tfs[2] = "M30"; tfs[3] = "H1"; tfs[4] = "H4";
   ENUM_TIMEFRAMES tf_enum[5];
   tf_enum[0] = PERIOD_M1; tf_enum[1] = PERIOD_M5; tf_enum[2] = PERIOD_M30;
   tf_enum[3] = PERIOD_H1; tf_enum[4] = PERIOD_H4;
   int nTF = 5;

   for(int i = 0; i < g_sym_count; i++)
   {
      string sym = g_symbols[i];

      MqlTick tick;
      if(!SymbolInfoTick(sym, tick)) continue;

      // ── Publish TICK ─────────────────────────────────────────
      int    digits      = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double pip_size    = (digits == 3 || digits == 5) ? 0.0001 : 0.001;
      if(digits == 2) pip_size = 0.1;
      double spread_pips = (tick.ask - tick.bid) / pip_size;

      _ZmqSend(StringFormat(
         "TICK {\"sym\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,\"spread\":%.2f,\"time\":%d}",
         sym, tick.bid, tick.ask, spread_pips, (int)tick.time
      ));

      // ── Check bar closes for M1/M5/M30/H1/H4 ───────────────
      for(int t = 0; t < nTF; t++)
      {
         datetime bar0 = iTime(sym, tf_enum[t], 0);

         // slot index is deterministic: row-major layout s*nTF + t
         int slot_idx = i * nTF + t;
         if(slot_idx < 0 || slot_idx >= g_bar_count) continue;

         if(bar0 != g_bar_last_open[slot_idx])
         {
            _ZmqSend(StringFormat(
               "BAR_CLOSE {\"sym\":\"%s\",\"tf\":\"%s\","
               "\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,"
               "\"vol\":%d,\"time\":%d}",
               sym, tfs[t],
               iOpen(sym, tf_enum[t], 1), iHigh(sym, tf_enum[t], 1),
               iLow(sym,  tf_enum[t], 1), iClose(sym, tf_enum[t], 1),
               (int)iVolume(sym, tf_enum[t], 1), (int)iTime(sym, tf_enum[t], 1)
            ));
            _ZmqSend(StringFormat(
               "BAR_OPEN {\"sym\":\"%s\",\"tf\":\"%s\",\"time\":%d}",
               sym, tfs[t], (int)bar0
            ));
            g_bar_last_open[slot_idx] = bar0;
         }
      }
   }
}

//+------------------------------------------------------------------+
//| OnTimer — heartbeat + bind retry                                 |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!g_bound) { _TryBind(); return; }

   datetime now = TimeCurrent();
   if(now - g_last_tick_time >= HEARTBEAT_SECS)
   {
      _ZmqSend(StringFormat("HEARTBEAT {\"time\":%d}", (int)now));
      g_last_tick_time = now;
   }
}

//+------------------------------------------------------------------+
//| OnTradeTransaction                                               |
//+------------------------------------------------------------------+
void OnTradeTransaction(
   const MqlTradeTransaction &trans,
   const MqlTradeRequest     &request,
   const MqlTradeResult      &result)
{
   if(!g_bound) return;
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD &&
      trans.type != TRADE_TRANSACTION_POSITION) return;

   ulong ticket = trans.position;
   if(ticket == 0 || !PositionSelectByTicket(ticket)) return;

   string sym    = PositionGetString(POSITION_SYMBOL);
   int    ptype  = (int)PositionGetInteger(POSITION_TYPE);
   double vol    = PositionGetDouble(POSITION_VOLUME);
   double price  = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl     = PositionGetDouble(POSITION_SL);
   double tp     = PositionGetDouble(POSITION_TP);
   double profit = PositionGetDouble(POSITION_PROFIT);

   _ZmqSend(StringFormat(
      "TRADE {\"event\":\"%s\",\"ticket\":%d,\"sym\":\"%s\","
      "\"type\":\"%s\",\"vol\":%.2f,\"price\":%.5f,"
      "\"sl\":%.5f,\"tp\":%.5f,\"profit\":%.2f,\"time\":%d}",
      (trans.type == TRADE_TRANSACTION_DEAL_ADD ? "DEAL" : "MODIFY"),
      (int)ticket, sym,
      (ptype == 0 ? "BUY" : "SELL"),
      vol, price, sl, tp, profit, (int)TimeCurrent()
   ));
}
