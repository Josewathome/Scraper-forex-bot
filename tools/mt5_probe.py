#!/usr/bin/env python3
"""mt5_probe.py — test whether the MT5 Python IPC is reachable yet.

Run under Wine Python (same interpreter the bot uses). Exits 0 the moment
mt5.initialize() succeeds by ANY method, else 1. Prints the exact reason it
failed so we can tell timing (IPC not ready) apart from a real fault
(import error, terminal not found, wrong path).

The bot (mt5_gateway.connect) calls mt5.initialize() with no path. Under Wine
that sometimes cannot locate a manually-launched terminal, so here we ALSO try
the explicit terminal64.exe path. Whichever works is printed as WORKING=...,
which tells us how the bot should be configured (MT5_PATH).
"""
import sys

TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

try:
    import MetaTrader5 as mt5
except Exception as exc:  # import itself failing is a hard fault, not timing
    print("PROBE: import MetaTrader5 FAILED:", repr(exc))
    sys.exit(1)

# Attempt 1 — no path (exactly what the bot does today).
ok = mt5.initialize(timeout=20000)
if ok:
    print("PROBE: initialize() OK with NO path. WORKING=empty")
    mt5.shutdown()
    sys.exit(0)
code_nopath = mt5.last_error()
mt5.shutdown()

# Attempt 2 — explicit terminal path (more robust under Wine).
ok = mt5.initialize(TERMINAL_PATH, timeout=20000)
if ok:
    print("PROBE: initialize() OK with explicit path. WORKING=path "
          "(set MT5_PATH to %s)" % TERMINAL_PATH)
    mt5.shutdown()
    sys.exit(0)
code_path = mt5.last_error()
mt5.shutdown()

print("PROBE: not ready. no-path err=%r  path err=%r" % (code_nopath, code_path))
sys.exit(1)
