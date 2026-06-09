#!/usr/bin/env python3
"""inject_ea_chart.py — attach ZoneBotBridge to MT5's ACTIVE chart profile.

Why this exists
---------------
For the EA to auto-load on every MT5 start (no VNC, no manual drag) its
``<expert>`` block must live inside a ``.chr`` file in the profile MT5
actually loads.  On the gmag11 metatrader5_vnc image (MT5 build 5836,
``/portable``) that profile is::

    .../MetaTrader 5/MQL5/Profiles/Charts/Default/

NOT the classic ``.../MetaTrader 5/Profiles/Charts/Default/``.  The old
startup script injected into the classic path, which MT5 never reads — so
the EA was never attached and the bot received zero ticks for days.

This helper resolves the active charts directory at runtime (the Default
profile dir whose ``.chr`` files were most recently written by MT5),
enforces EXACTLY ONE EA instance (the bot's TCP feed is single-client —
``socket.listen(1)`` + serial accept), and writes the proper UTF-16-LE
``<expert>`` block with an ``<inputs>`` sub-block that MT5 itself emits.

Exit codes (consumed by override_start.sh to decide whether to restart MT5)
    0  charts were modified — MT5 must restart to pick up the change
    2  already correct (single GBPUSD instance present) — no restart needed
"""
import os
import sys
import glob

MT5_DIR  = os.environ.get("MT5_DIR", "/config/.wine/drive_c/Program Files/MetaTrader 5")
EA_NAME  = "ZoneBotBridge"
ENDPOINT = "tcp://127.0.0.1:5556"
SYMBOLS  = os.environ.get("SYMBOLS_CSV", "GBPUSD,XAUUSD,USDJPY,AUDUSD,USDCHF")

# The <expert> block MT5 persists for an attached EA. period_type=2/size=1 = H1.
EXPERT_BLOCK = (
    "\n<expert>\nname={ea}\nflags=3\nwindow=0\n\n"
    "<inputs>\nPUB_ENDPOINT={ep}\nSYMBOLS_CSV={sym}\nHEARTBEAT_SECS=5\n</inputs>\n\n"
    "</expert>\n"
).format(ea=EA_NAME, ep=ENDPOINT, sym=SYMBOLS)


def resolve_charts_dir():
    """Return the Default-profile charts dir MT5 actively uses.

    Picks whichever candidate holds the most-recently-modified .chr files
    (i.e. the one MT5 saves on exit). On a fresh volume where neither is
    populated yet, defaults to the MQL5 profile path and creates it.
    """
    candidates = [
        os.path.join(MT5_DIR, "MQL5", "Profiles", "Charts", "Default"),
        os.path.join(MT5_DIR, "Profiles", "Charts", "Default"),
    ]
    best, best_mtime = None, -1.0
    for d in candidates:
        chrs = glob.glob(os.path.join(d, "*.chr"))
        if not chrs:
            continue
        m = max(os.path.getmtime(p) for p in chrs)
        if m > best_mtime:
            best, best_mtime = d, m
    if best is not None:
        return best
    fresh = candidates[0]
    os.makedirs(fresh, exist_ok=True)
    return fresh


def read_text(path):
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace").lstrip("﻿")
    return raw.decode("utf-8", errors="replace")


def write_chart(path, text):
    # MT5 .chr files are UTF-16-LE with a BOM.
    open(path, "wb").write(b"\xff\xfe" + text.encode("utf-16-le"))


def strip_zonebot(text):
    """Remove every <expert>...</expert> block that hosts ZoneBotBridge."""
    out, i = [], 0
    while True:
        start = text.find("<expert>", i)
        if start == -1:
            out.append(text[i:])
            break
        end = text.find("</expert>", start)
        if end == -1:
            out.append(text[i:])
            break
        end += len("</expert>")
        block = text[start:end]
        if "name=" + EA_NAME in block:
            out.append(text[i:start])   # drop the ZoneBotBridge block
        else:
            out.append(text[i:end])     # keep any other expert untouched
        i = end
    return "".join(out)


def has_ea(text):
    return "name=" + EA_NAME in text


MINIMAL_GBPUSD_H1 = (
    "<chart>\nsymbol=GBPUSD\nperiod_type=2\nperiod_size=1\ndigits=5\n"
    "tick_size=0.000000\nscale=4\nmode=1\nbidline=1\n\n<window>\nheight=100\n\n"
    "<indicator>\nname=Main\npath=\napply=1\nshow_data=1\nscale_inherit=0\n"
    "scale_line=0\nscale_line_percent=50\nscale_line_value=0.000000\n"
    "scale_fix_min=0\nscale_fix_min_val=0.000000\nscale_fix_max=0\n"
    "scale_fix_max_val=0.000000\n</indicator>\n\n</window>\n\n</chart>\n"
)


def pick_target(charts, chr_dir):
    """Choose the chart to host the EA: GBPUSD H1 > any GBPUSD > new minimal."""
    for path, text in charts.items():
        if "symbol=GBPUSD" in text and "period_type=2" in text and "period_size=1" in text:
            return path, False
    for path, text in charts.items():
        if "symbol=GBPUSD" in text:
            return path, False
    # No GBPUSD chart at all — create a minimal one (fresh volume).
    idx = len(charts) + 1
    new_path = os.path.join(chr_dir, "chart{:02d}.chr".format(idx))
    charts[new_path] = MINIMAL_GBPUSD_H1
    return new_path, True


def desired_order_wnd(target_basename):
    """The order.wnd that makes MT5 open exactly our one chart on startup.

    order.wnd is a UTF-16-LE (BOM-prefixed) file listing the chart files to
    open, one 'chartNN.chr\\r\\n' per line (decoded from a real MT5 profile).
    MT5 opens the charts listed HERE — NOT whatever .chr files happen to be on
    disk. The Default profile on this image ships WITHOUT an order.wnd, so MT5
    opens zero charts and the injected EA never loads. Writing this file is the
    fix: list our single chart so MT5 opens it (and loads the EA) on launch.
    """
    return b"\xff\xfe" + (target_basename + "\r\n").encode("utf-16-le")


def main():
    chr_dir = resolve_charts_dir()
    charts = {p: read_text(p) for p in sorted(glob.glob(os.path.join(chr_dir, "*.chr")))}

    target, created = pick_target(charts, chr_dir)
    target_base = os.path.basename(target)

    order_path = os.path.join(chr_dir, "order.wnd")
    want_order = desired_order_wnd(target_base)
    have_order = b""
    if os.path.exists(order_path):
        try:
            have_order = open(order_path, "rb").read()
        except Exception:
            have_order = b""
    order_ok = (have_order == want_order)

    # Already ideal? Exactly one EA instance on the target, nowhere else, AND
    # order.wnd already tells MT5 to open exactly that chart.
    others_have = any(has_ea(t) for p, t in charts.items() if p != target)
    if not created and has_ea(charts[target]) and not others_have and order_ok:
        print("inject_ea_chart: EA + order.wnd already correct in",
              os.path.relpath(target, MT5_DIR))
        sys.exit(2)

    # Normalize: strip ZoneBotBridge from every chart so only one remains.
    for path, text in list(charts.items()):
        stripped = strip_zonebot(text)
        if stripped != text:
            charts[path] = stripped
            if not (created and path == target):
                write_chart(path, stripped)
                print("inject_ea_chart: removed stray EA from", os.path.basename(path))

    # Inject the single instance into the target chart.
    text = charts[target]
    if has_ea(text):
        pass  # already present (we may only be fixing order.wnd)
    elif "</chart>" in text:
        text = text.replace("</chart>", EXPERT_BLOCK + "</chart>", 1)
    else:
        text = text.rstrip() + EXPERT_BLOCK + "\n</chart>\n"
    write_chart(target, text)

    # Write order.wnd so MT5 actually OPENS this chart on startup. Without this,
    # MT5 opens no chart and the EA never loads (the whole persistence problem).
    open(order_path, "wb").write(want_order)

    print("inject_ea_chart: EA attached to", os.path.relpath(target, MT5_DIR),
          "+ order.wnd ->", target_base,
          "(created new chart)" if created else "")
    sys.exit(0)


if __name__ == "__main__":
    main()
