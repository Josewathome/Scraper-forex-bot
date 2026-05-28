#!/usr/bin/env python3
"""
Smoke-test for the News API — replicates exactly how the bot calls it.
Uses only stdlib (urllib) so no pip install is needed.

Run from inside the mt5 container (same network as forex-api):
  docker exec mt5 python3 /bot/test_news_api.py

Optional env overrides:
  SYMBOL=GBP        currency to check (default: USD)
  DATE=2026-05-14   date to check     (default: today)
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import date

NEWS_API_URL = os.environ.get("NEWS_API_URL", "http://192.168.1.100:8087").rstrip("/")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
SYMBOL       = os.environ.get("SYMBOL", "USD")
CHECK_DATE   = os.environ.get("DATE", str(date.today()))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
if NEWS_API_KEY:
    HEADERS["X-API-Key"] = NEWS_API_KEY

def fetch(name: str, endpoint: str, params: dict) -> None:
    qs  = urllib.parse.urlencode(params)
    url = f"{NEWS_API_URL}{endpoint}?{qs}"

    print(f"\n{'─'*60}")
    print(f"  {name.upper()}  |  symbol={SYMBOL}  |  date={CHECK_DATE}")
    print(f"  GET {url}")
    print(f"{'─'*60}")

    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body  = resp.read().decode()
            code  = resp.status
            print(f"  Status : {code}")
            data   = json.loads(body)
            events = data if isinstance(data, list) else data.get("events", data)
            count  = len(events) if isinstance(events, list) else "?"
            print(f"  Events : {count} returned")
            if isinstance(events, list) and events:
                print(f"  Sample :\n{json.dumps(events[0], indent=4, default=str)[:800]}")
            else:
                print("  Sample : (no events for this date/currency)")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  HTTP {e.code} : {body[:400]}")
    except urllib.error.URLError as e:
        print(f"  ERROR  : {e.reason}")
        print(f"           NEWS_API_URL={NEWS_API_URL}")
        print("           Is forex-api reachable from inside this container?")
        print("           Check: docker exec mt5 curl -s http://forex-api:8000/health")
    except Exception as exc:
        print(f"  ERROR  : {exc}")

# ── same params the bot uses (see src/infrastructure/news_client.py) ──────────
ff_params  = {"date": CHECK_DATE, "currency": SYMBOL}
mfb_params = {"start_date": CHECK_DATE, "end_date": CHECK_DATE, "currency": SYMBOL}
if NEWS_API_KEY:
    ff_params["api_key"]  = NEWS_API_KEY
    mfb_params["api_key"] = NEWS_API_KEY

print(f"\nNews API Smoke Test")
print(f"  NEWS_API_URL : {NEWS_API_URL}")
print(f"  NEWS_API_KEY : {'set (' + NEWS_API_KEY[:4] + '…)' if NEWS_API_KEY else 'NOT SET'}")

fetch("ForexFactory", "/forexfactory/events", ff_params)
fetch("MyFxBook",     "/myfxbook/events",     mfb_params)

print(f"\n{'─'*60}\nDone.\n")
