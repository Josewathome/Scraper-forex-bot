//+------------------------------------------------------------------+
//|  ZoneBotBridge.mq5 v3 — Built-in Socket Transport               |
//|                                                                  |
//|  WHY no DLL imports                                              |
//|  The previous version used #import "libzmq.dll". MT5 shows a    |
//|  DLL-import permission dialog every restart (resets on exit).    |
//|  With no user at the VNC OnInit() never completes. v3 uses       |
//|  MT5's built-in SocketCreate/SocketConnect/SocketSend which      |
//|  need NO DLL imports and NO permission dialog — EA loads on      |
//|  every restart automatically.                                    |
//|                                                                  |
//|  TRANSPORT                                                       |
//|  EA = TCP CLIENT  →  connects to Python TCP SERVER on :5556      |
//|  Messages: newline-delimited "TOPIC {JSON}\n"                    |
//|  Same topics as v2: TICK / BAR_CLOSE / BAR_OPEN / TRADE /        |
//|  HEARTBEAT                                                       |
//+------------------------------------------------------------------+
#property copyright "ZoneBot"
#property version   "3.00"
#property strict

input string   PUB_ENDPOINT   = "tcp://127.0.0.1:5556";
input string   SYMBOLS_CSV    = "GBPUSD,XAUUSD,USDJPY,AUDUSD,USDCHF";
input int      HEARTBEAT_SECS = 5;

string   g_host = "127.0.0.1";
int      g_port = 5556;

int      g_sock      = INVALID_HANDLE;
bool     g_connected = false;

string   g_symbols[];
int      g_sym_count = 0;

string   g_bar_keys[];
datetime g_bar_last_open[];
int      g_bar_count = 0;

datetime g_last_tick_time    = 0;
datetime g_last_conn_attempt = 0;
int      g_conn_retry_secs   = 5;

void _ParseEndpoint(const string ep)
{
   string s = ep;
   if(StringFind(s, "tcp://") == 0) s = StringSubstr(s, 6);
   int colon = StringFind(s, ":");
   if(colon > 0)
   {
      g_host = StringSubstr(s, 0, colon);
      g_port = (int)StringToInteger(StringSubstr(s, colon + 1));
   }
}

void _Send(const string msg)
{
   if(!g_connected || g_sock == INVALID_HANDLE) return;
   uchar buf[];
   int len = StringToCharArray(msg + "\n", buf, 0, -1, CP_UTF8) - 1;
   if(len <= 0) return;
   if(SocketSend(g_sock, buf, len) < 0)
   {
      Print("ZoneBotBridge: send failed — reconnecting");
      SocketClose(g_sock);
      g_sock      = INVALID_HANDLE;
      g_connected = false;
   }
}

void _TryConnect()
{
   datetime now = TimeCurrent();
   if(now - g_last_conn_attempt < g_conn_retry_secs) return;
   g_last_conn_attempt = now;

   if(g_sock != INVALID_HANDLE) { SocketClose(g_sock); g_sock = INVALID_HANDLE; }
   g_connected = false;

   g_sock = SocketCreate();
   if(g_sock == INVALID_HANDLE) { Print("ZoneBotBridge: SocketCreate failed: ", GetLastError()); return; }

   if(SocketConnect(g_sock, g_host, (uint)g_port, 3000))
   {
      g_connected = true;
      Print("ZoneBotBridge: connected to ", g_host, ":", g_port);
   }
   else
   {
      Print("ZoneBotBridge: connect failed (", g_host, ":", g_port, ") err=", GetLastError(), " retry in ", g_conn_retry_secs, "s");
      SocketClose(g_sock);
      g_sock = INVALID_HANDLE;
   }
}

int OnInit()
{
   _ParseEndpoint(PUB_ENDPOINT);
   g_sym_count = StringSplit(SYMBOLS_CSV, ',', g_symbols);
   if(g_sym_count == 0) { Print("ZoneBotBridge: no symbols"); return INIT_FAILED; }

   for(int i = 0; i < g_sym_count; i++) SymbolSelect(g_symbols[i], true);

   string tfs[5]; tfs[0]="M1"; tfs[1]="M5"; tfs[2]="M30"; tfs[3]="H1"; tfs[4]="H4";
   ENUM_TIMEFRAMES tf_enum[5];
   tf_enum[0]=PERIOD_M1; tf_enum[1]=PERIOD_M5; tf_enum[2]=PERIOD_M30;
   tf_enum[3]=PERIOD_H1; tf_enum[4]=PERIOD_H4;

   ArrayResize(g_bar_keys,      g_sym_count * 5);
   ArrayResize(g_bar_last_open, g_sym_count * 5);
   int slot = 0;
   for(int s = 0; s < g_sym_count; s++)
      for(int t = 0; t < 5; t++)
      {
         g_bar_keys[slot]      = g_symbols[s] + "_" + tfs[t];
         g_bar_last_open[slot] = iTime(g_symbols[s], tf_enum[t], 0);
         slot++;
      }
   g_bar_count = slot;

   _TryConnect();
   EventSetTimer(1);
   Print("ZoneBotBridge v3: target=", g_host, ":", g_port);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_sock != INVALID_HANDLE) { SocketClose(g_sock); g_sock = INVALID_HANDLE; }
   g_connected = false;
   Print("ZoneBotBridge: stopped (reason=", reason, ")");
}

void OnTick()
{
   if(!g_connected) { _TryConnect(); return; }
   g_last_tick_time = TimeCurrent();

   string tfs[5]; tfs[0]="M1"; tfs[1]="M5"; tfs[2]="M30"; tfs[3]="H1"; tfs[4]="H4";
   ENUM_TIMEFRAMES tf_enum[5];
   tf_enum[0]=PERIOD_M1; tf_enum[1]=PERIOD_M5; tf_enum[2]=PERIOD_M30;
   tf_enum[3]=PERIOD_H1; tf_enum[4]=PERIOD_H4;

   for(int i = 0; i < g_sym_count; i++)
   {
      string sym = g_symbols[i];
      MqlTick tick;
      if(!SymbolInfoTick(sym, tick)) continue;

      int    digits     = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double pip_size   = (digits==3||digits==5) ? 0.0001 : 0.001;
      if(digits==2) pip_size = 0.1;
      double spread_pips = (tick.ask - tick.bid) / pip_size;

      _Send(StringFormat(
         "TICK {\"sym\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,\"spread\":%.2f,\"time\":%d}",
         sym, tick.bid, tick.ask, spread_pips, (int)tick.time));

      for(int t = 0; t < 5; t++)
      {
         datetime bar0 = iTime(sym, tf_enum[t], 0);
         int slot_idx  = i * 5 + t;
         if(slot_idx < 0 || slot_idx >= g_bar_count) continue;
         if(bar0 != g_bar_last_open[slot_idx])
         {
            _Send(StringFormat(
               "BAR_CLOSE {\"sym\":\"%s\",\"tf\":\"%s\","
               "\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,"
               "\"vol\":%d,\"time\":%d}",
               sym, tfs[t],
               iOpen(sym,tf_enum[t],1), iHigh(sym,tf_enum[t],1),
               iLow(sym, tf_enum[t],1), iClose(sym,tf_enum[t],1),
               (int)iVolume(sym,tf_enum[t],1), (int)iTime(sym,tf_enum[t],1)));
            _Send(StringFormat(
               "BAR_OPEN {\"sym\":\"%s\",\"tf\":\"%s\",\"time\":%d}",
               sym, tfs[t], (int)bar0));
            g_bar_last_open[slot_idx] = bar0;
         }
      }
   }
}

void OnTimer()
{
   if(!g_connected) { _TryConnect(); return; }
   datetime now = TimeCurrent();
   if(now - g_last_tick_time >= HEARTBEAT_SECS)
   {
      _Send(StringFormat("HEARTBEAT {\"time\":%d}", (int)now));
      g_last_tick_time = now;
   }
}

void OnTradeTransaction(
   const MqlTradeTransaction &trans,
   const MqlTradeRequest     &request,
   const MqlTradeResult      &result)
{
   if(!g_connected) return;
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD &&
      trans.type != TRADE_TRANSACTION_POSITION) return;
   ulong ticket = trans.position;
   if(ticket == 0 || !PositionSelectByTicket(ticket)) return;

   string sym   = PositionGetString(POSITION_SYMBOL);
   int    ptype = (int)PositionGetInteger(POSITION_TYPE);
   _Send(StringFormat(
      "TRADE {\"event\":\"%s\",\"ticket\":%d,\"sym\":\"%s\","
      "\"type\":\"%s\",\"vol\":%.2f,\"price\":%.5f,"
      "\"sl\":%.5f,\"tp\":%.5f,\"profit\":%.2f,\"time\":%d}",
      (trans.type==TRADE_TRANSACTION_DEAL_ADD ? "DEAL" : "MODIFY"),
      (int)ticket, sym, (ptype==0 ? "BUY" : "SELL"),
      PositionGetDouble(POSITION_VOLUME),
      PositionGetDouble(POSITION_PRICE_OPEN),
      PositionGetDouble(POSITION_SL),
      PositionGetDouble(POSITION_TP),
      PositionGetDouble(POSITION_PROFIT),
      (int)TimeCurrent()));
}
