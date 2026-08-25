//+------------------------------------------------------------------+
//| AttachUS30.mq5 - one-shot script to open US30 H1 and attach       |
//| US30_ShortBBFade_EA via a saved template, run through [StartUp]   |
//| Script= on every terminal boot.                                    |
//+------------------------------------------------------------------+
#property strict

void OnStart()
{
   long chart_id = ChartOpen("US30", PERIOD_H1);
   if(chart_id == 0)
   {
      Print("AttachUS30: ChartOpen failed, error=", GetLastError());
      return;
   }
   Sleep(500);
   bool ok = ChartApplyTemplate(chart_id, "US30_ShortBBFade.tpl");
   Print("AttachUS30: chart_id=", chart_id, " ChartApplyTemplate result=", ok, " error=", GetLastError());
}
