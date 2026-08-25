//+------------------------------------------------------------------+
//| AttachDeployedEAs.mq5 - one-shot script to open all live-deployed  |
//| EA charts via saved templates, run through [StartUp] Script= on    |
//| every terminal boot. Does not modify AttachUS30.mq5 or             |
//| US30_ShortBBFade_EA.mq5 - this is the sole thing overridden in     |
//| override_start.sh's Script= key going forward.                     |
//+------------------------------------------------------------------+
#property strict

void OnStart()
{
   long us30_id = ChartOpen("US30", PERIOD_H1);
   if(us30_id == 0)
   {
      Print("AttachDeployedEAs: US30 ChartOpen failed, error=", GetLastError());
   }
   else
   {
      Sleep(500);
      bool ok1 = ChartApplyTemplate(us30_id, "US30_ShortBBFade.tpl");
      Print("AttachDeployedEAs: US30 chart_id=", us30_id, " ChartApplyTemplate result=", ok1, " error=", GetLastError());
   }

   long audusd_id = ChartOpen("AUDUSD", PERIOD_H1);
   if(audusd_id == 0)
   {
      Print("AttachDeployedEAs: AUDUSD ChartOpen failed, error=", GetLastError());
   }
   else
   {
      Sleep(500);
      bool ok2 = ChartApplyTemplate(audusd_id, "NZDUSD_AUD_Divergence.tpl");
      Print("AttachDeployedEAs: AUDUSD chart_id=", audusd_id, " ChartApplyTemplate result=", ok2, " error=", GetLastError());
   }

   long xauusd_id = ChartOpen("XAUUSD", PERIOD_M15);
   if(xauusd_id == 0)
   {
      Print("AttachDeployedEAs: XAUUSD ChartOpen failed, error=", GetLastError());
   }
   else
   {
      Sleep(500);
      bool ok3 = ChartApplyTemplate(xauusd_id, "AMD_BOS_Liquidity.tpl");
      Print("AttachDeployedEAs: XAUUSD chart_id=", xauusd_id, " ChartApplyTemplate result=", ok3, " error=", GetLastError());
   }

   long eurusd_id = ChartOpen("EURUSD", PERIOD_M15);
   if(eurusd_id == 0)
   {
      Print("AttachDeployedEAs: EURUSD ChartOpen failed, error=", GetLastError());
   }
   else
   {
      Sleep(500);
      bool ok4 = ChartApplyTemplate(eurusd_id, "LowVolBigCandleReversion.tpl");
      Print("AttachDeployedEAs: EURUSD chart_id=", eurusd_id, " ChartApplyTemplate result=", ok4, " error=", GetLastError());
   }

   long xauusd_h1_id = ChartOpen("XAUUSD", PERIOD_H1);
   if(xauusd_h1_id == 0)
   {
      Print("AttachDeployedEAs: XAUUSD(H1) ChartOpen failed, error=", GetLastError());
   }
   else
   {
      Sleep(500);
      bool ok5 = ChartApplyTemplate(xauusd_h1_id, "GoldTrendRider_Gold.tpl");
      Print("AttachDeployedEAs: XAUUSD(H1) chart_id=", xauusd_h1_id, " ChartApplyTemplate result=", ok5, " error=", GetLastError());
   }

   long xagusd_id = ChartOpen("XAGUSD", PERIOD_H1);
   if(xagusd_id == 0)
   {
      Print("AttachDeployedEAs: XAGUSD ChartOpen failed, error=", GetLastError());
   }
   else
   {
      Sleep(500);
      bool ok6 = ChartApplyTemplate(xagusd_id, "GoldTrendRider_Silver.tpl");
      Print("AttachDeployedEAs: XAGUSD chart_id=", xagusd_id, " ChartApplyTemplate result=", ok6, " error=", GetLastError());
   }

   if(!SymbolSelect("XTIUSD", true))
      Print("AttachDeployedEAs: XTIUSD SymbolSelect failed, error=", GetLastError());

   long xtiusd_id = ChartOpen("XTIUSD", PERIOD_H1);
   if(xtiusd_id == 0)
   {
      Print("AttachDeployedEAs: XTIUSD ChartOpen failed, error=", GetLastError());
   }
   else
   {
      Sleep(500);
      bool ok7 = ChartApplyTemplate(xtiusd_id, "GoldTrendRider_Oil.tpl");
      Print("AttachDeployedEAs: XTIUSD chart_id=", xtiusd_id, " ChartApplyTemplate result=", ok7, " error=", GetLastError());
   }
}
