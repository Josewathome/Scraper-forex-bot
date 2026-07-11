"""
test_news_client_timeout.py — Regression tests for the news HTTP timeout.

Runs with plain Python (no pytest required):

    python -m tests.test_news_client_timeout

Guards against a real bug found in the 2026-07-07 QA audit: both
ForexNewsClient HTTP calls ran with NO timeout on the MAIN event loop
(news refresh every 10 minutes). `requests` blocks indefinitely without
a timeout, so one hung/slow news host froze ALL tick processing and
SL/TP management until the connection died on its own.

The fix passes timeout=(connect, read) to every session.get. A Timeout
is a RequestException, which the existing handlers already map to [] —
NewsManager then fails closed on stale data, which is the designed
degradation path.
"""
from __future__ import annotations

import os

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

import requests

from src.infrastructure.news_client import ForexNewsClient


class _NullCache:
    def get(self, key):
        return None

    def set(self, key, value, ttl):
        pass


class _CapturingSession:
    """Records the kwargs of every .get() and returns a canned response."""

    def __init__(self, exc: Exception | None = None):
        self.calls: list[dict] = []
        self._exc = exc

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc

        class _Resp:
            status_code = 200
            url = "http://test"
            text = "{}"
            request = None          # _log_response_debug reads resp.request
            headers: dict = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {"events": []}

        return _Resp()

    def close(self):
        pass


def _make_client(session) -> ForexNewsClient:
    client = ForexNewsClient(api_key="test-key", cache=_NullCache())
    client._session = session
    return client


def test_timeout_is_passed_to_every_http_call():
    session = _CapturingSession()
    client = _make_client(session)

    client.get_today_events(["USD"], "2026-07-07")
    client.get_range_events(["USD"], "2026-07-01", "2026-07-07")

    assert len(session.calls) == 2, f"expected 2 HTTP calls, saw {len(session.calls)}"
    for i, kwargs in enumerate(session.calls):
        timeout = kwargs.get("timeout")
        assert timeout is not None, f"call #{i}: session.get has NO timeout — main-loop freeze bug is back"
        connect, read = timeout
        assert 0 < connect <= 10 and 0 < read <= 30, f"call #{i}: unreasonable timeout {timeout}"
    print("PASS  test_timeout_is_passed_to_every_http_call")


def test_timeout_exception_returns_empty_not_raises():
    session = _CapturingSession(exc=requests.Timeout("simulated hung news host"))
    client = _make_client(session)

    events = client.get_today_events(["USD"], "2026-07-07")
    assert events == [], f"Timeout must map to [] (fail-closed upstream), got {events!r}"

    events = client.get_range_events(["USD"], "2026-07-01", "2026-07-07")
    assert events == [], f"Timeout must map to [] (fail-closed upstream), got {events!r}"
    print("PASS  test_timeout_exception_returns_empty_not_raises")


if __name__ == "__main__":
    test_timeout_is_passed_to_every_http_call()
    test_timeout_exception_returns_empty_not_raises()
    print("\nAll news-client timeout checks passed.")
