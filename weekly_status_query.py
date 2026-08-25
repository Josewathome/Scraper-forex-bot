import MetaTrader5 as mt5
import datetime

mt5.initialize()
acct = mt5.account_info()
print(f"=== Weekly MT5 Status Check - {datetime.datetime.utcnow()} UTC ===")
if acct is None:
    print("ERROR: could not connect to MT5 terminal (account_info() returned None)")
    mt5.shutdown()
    raise SystemExit(1)
print(f"Account: balance={acct.balance:.2f} equity={acct.equity:.2f} floating_pnl={acct.profit:+.2f}\n")

MAGIC_NAMES = {
    55211101: "US30_ShortBBFade_EA",
    55211801: "NZDUSD_AUD_Divergence_EA",
}

positions = mt5.positions_get()
print(f"=== OPEN POSITIONS ({len(positions) if positions else 0}) ===")
if positions:
    for p in positions:
        if p.magic in MAGIC_NAMES:
            name = MAGIC_NAMES[p.magic]
        elif p.magic == 0:
            name = "Portfolio_EA (legacy, magic=0)"
        else:
            name = f"unknown magic={p.magic}"
        opened = datetime.datetime.utcfromtimestamp(p.time)
        side = "BUY" if p.type == 0 else "SELL"
        print(f"  [{name}] {p.symbol} {side} {p.volume} @ {p.price_open} now={p.price_current} "
              f"pnl={p.profit:+.2f} opened={opened} comment='{p.comment}'")
else:
    print("  (none)")

print()
from_date = datetime.datetime.now() - datetime.timedelta(days=8)
to_date = datetime.datetime.now() + datetime.timedelta(days=1)
deals = mt5.history_deals_get(from_date, to_date)
relevant = [d for d in deals if d.magic in MAGIC_NAMES] if deals else []
print(f"=== CLOSED DEALS in last 8 days for monitored EAs ({len(relevant)}) ===")
if relevant:
    total_pnl = 0.0
    for d in relevant:
        name = MAGIC_NAMES.get(d.magic, "?")
        t = datetime.datetime.utcfromtimestamp(d.time)
        side = "BUY" if d.type == 0 else "SELL"
        print(f"  [{name}] {d.symbol} {side} {d.volume} @ {d.price} profit={d.profit:+.2f} time={t} comment='{d.comment}'")
        total_pnl += d.profit
    print(f"  --- total realized P&L this window: {total_pnl:+.2f} ---")
else:
    print("  (none)")

mt5.shutdown()
