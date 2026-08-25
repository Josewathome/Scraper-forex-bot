//+------------------------------------------------------------------+
//|                                          HaltedPlaceholder.mq5     |
//| Inert placeholder attached in ZoneBotBridge's place ONLY while    |
//| TRADING_HALTED=true. Exists solely to satisfy [StartUp]'s         |
//| apparent requirement that an Expert= entry be present for the     |
//| accompanying Script= entry (AttachDeployedEAs) to fire - confirmed|
//| by direct observation 2026-08-13 that omitting Expert= entirely   |
//| stops Script= from running at all, silently breaking the other    |
//| two live EAs. Does no trading, no reconnect attempts, no          |
//| resource use beyond sitting idle. ZoneBotBridge.mq5 itself is      |
//| never modified by this.                                            |
//+------------------------------------------------------------------+
#property copyright "Placeholder - not a trading EA"
#property version   "1.00"
#property strict

int OnInit()
{
   Print("[HaltedPlaceholder] attached (TRADING_HALTED=true) - inert, no trading, no reconnect attempts.");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
}

void OnTick()
{
   // deliberately does nothing
}
