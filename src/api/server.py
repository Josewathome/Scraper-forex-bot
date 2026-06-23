"""
server.py — Flask dashboard API server.

Runs in a background daemon thread inside Wine Python.
Serves the dashboard HTML at http://localhost:8080
and exposes a REST API for live config/stats updates.

All /api/* endpoints (except /api/auth/*) require a valid JWT access token
in the Authorization: Bearer <token> header.

Auth endpoints:
    POST /api/auth/login          → {access_token, refresh_token, expires_in}
    POST /api/auth/refresh        → {access_token, expires_in}
    POST /api/auth/logout         → {ok: true}  (revokes refresh token)
    POST /api/auth/rotate-secret  → {ok: true}  (rotates JWT secret, invalidates all sessions)

Protected endpoints:
    GET  /                        → dashboard HTML
    GET  /api/status              → bot status + config snapshot
    GET  /api/stats               → trade analytics (global + per symbol)
    GET  /api/trades              → recent trades list
    GET  /api/config              → current config values
    PATCH /api/config             → live config update
    GET  /api/symbols             → current symbol list
    POST /api/symbols             → add a symbol
    DELETE /api/symbols/<sym>     → remove a symbol
    GET  /api/logs                → last N lines of trading_bot.log
    GET  /api/balance-history    → full balance event log
    POST /api/balance-history    → append a new balance event (deposit/withdrawal)
    POST /api/reset-balance      → reset performance baseline to current equity (Option A)
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import src.config as config

logger = logging.getLogger(__name__)

_BOT_START_TIME = datetime.now(tz=timezone.utc)

# File where dashboard config overrides are written so they survive restarts.
_CONFIG_OVERRIDE_FILE = Path(".checkpoints/config_overrides.json")

# Full history of balance events — each deposit/withdrawal appends a new entry.
# The most recent entry is the "current initial balance" for PnL calculations.
_BALANCE_HISTORY_FILE = Path(".checkpoints/balance_history.json")
_INITIAL_BALANCE_FILE = Path(".checkpoints/initial_balance.json")  # legacy — migrated on first load


def _load_balance_history() -> list:
    """Load balance history, auto-migrating from the old single-snapshot file."""
    if _BALANCE_HISTORY_FILE.exists():
        try:
            data = json.loads(_BALANCE_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass

    # Migrate from the old single-snapshot file if it exists
    seed: dict = {}
    if _INITIAL_BALANCE_FILE.exists():
        try:
            seed = json.loads(_INITIAL_BALANCE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    entry = {
        "balance":  float(seed.get("balance", config.ACCOUNT_BALANCE)),
        "currency": seed.get("currency", config.ACCOUNT_CURRENCY),
        "set_at":   seed.get("set_at", datetime.now(tz=timezone.utc).isoformat()),
        "note":     "Initial funding",
    }
    history = [entry]
    _save_balance_history(history)
    logger.info("Balance history initialised: %.2f %s", entry["balance"], entry["currency"])
    return history


def _save_balance_history(history: list) -> None:
    _BALANCE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _BALANCE_HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=2), encoding="utf-8")
    tmp.replace(_BALANCE_HISTORY_FILE)


def _get_initial_balance() -> tuple:
    """Return (balance, set_at, reset_at) from the most recent balance event.
    reset_at is the ISO timestamp of the most recent reset event, or None."""
    history = _load_balance_history()
    latest  = history[-1]
    # Find the most recent reset event to use as the stats baseline timestamp
    reset_at = None
    for event in reversed(history):
        if event.get("type") == "reset":
            reset_at = event["set_at"]
            break
    return float(latest["balance"]), latest["set_at"], reset_at

_CONFIG_MAP = {
    "risk_pct":           ("RISK_PERCENT",          float),
    "min_rr":             ("MIN_RR",                float),
    "max_open_trades":    ("MAX_OPEN_SYMBOLS",       int),
    "atr_cost_threshold": ("ATR_COST_THRESHOLD",    float),
    "loop_interval":      ("LOOP_INTERVAL_SECONDS", int),
    "tiered_tp_enabled":  ("TIERED_TP_ENABLED",     bool),
    "tiered_tp1_ratio":   ("TIERED_TP1_RATIO",      float),
    "tiered_tp2_ratio":   ("TIERED_TP2_RATIO",      float),
    "breakeven_rr":          ("BREAKEVEN_RR_TRIGGER",       float),
    "backtest_balance":      ("BACKTEST_INITIAL_BALANCE",   float),
}

# Symbols are persisted separately because they are a list, not a scalar.
_SYMBOLS_FILE = Path(".checkpoints/symbols_override.json")


def _persist_symbols() -> None:
    try:
        _SYMBOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SYMBOLS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(config.SYMBOLS), encoding="utf-8")
        tmp.replace(_SYMBOLS_FILE)
        logger.info("Symbols persisted to %s: %s", _SYMBOLS_FILE, config.SYMBOLS)
    except Exception as exc:
        logger.error("Failed to persist symbols: %s", exc)


def load_symbols_override() -> None:
    """Apply persisted symbol list override at startup. Call from load_config_overrides()."""
    if not _SYMBOLS_FILE.exists():
        return
    try:
        syms = json.loads(_SYMBOLS_FILE.read_text(encoding="utf-8"))
        if isinstance(syms, list) and syms:
            config.SYMBOLS[:] = syms
            logger.info("Loaded symbols override from disk: %s", config.SYMBOLS)
    except Exception as exc:
        logger.warning("Could not load symbols override: %s", exc)


def _persist_config_overrides(changed_keys, _map) -> None:
    """Write all current mutable config values to disk."""
    try:
        _CONFIG_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        overrides = {}
        for key, (attr, _) in _map.items():
            overrides[key] = getattr(config, attr, None)
        tmp = _CONFIG_OVERRIDE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
        tmp.replace(_CONFIG_OVERRIDE_FILE)
        logger.info("Config overrides persisted to %s (%s changed)",
                    _CONFIG_OVERRIDE_FILE, changed_keys)
    except Exception as exc:
        logger.error("Failed to persist config overrides: %s", exc)


def load_config_overrides() -> None:
    """
    Apply any previously-saved dashboard config overrides to the live config module.
    Call this once at bot startup (from main.py) BEFORE trading begins.
    """
    if _CONFIG_OVERRIDE_FILE.exists():
        try:
            overrides = json.loads(_CONFIG_OVERRIDE_FILE.read_text(encoding="utf-8"))
            applied = []
            for key, (attr, cast) in _CONFIG_MAP.items():
                if key in overrides and overrides[key] is not None:
                    setattr(config, attr, cast(overrides[key]))
                    applied.append(f"{attr}={overrides[key]}")
            if applied:
                logger.info("Loaded %d config overrides from disk: %s",
                            len(applied), ", ".join(applied))
        except Exception as exc:
            logger.warning("Could not load config overrides (%s) — using defaults.", exc)
    load_symbols_override()


# ─────────────────────────────────────────────────────────────────────
#  JWT utilities  (stdlib only — no PyJWT dependency)
# ─────────────────────────────────────────────────────────────────────

def _b64u_enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    rem = len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (4 - rem if rem else 0))


def _jwt_sign(header_b64: str, payload_b64: str, secret: str) -> str:
    msg = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return _b64u_enc(sig)


def _jwt_encode(payload: dict, secret: str) -> str:
    header  = _b64u_enc(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body    = _b64u_enc(json.dumps(payload).encode())
    sig     = _jwt_sign(header, body, secret)
    return f"{header}.{body}.{sig}"


def _jwt_decode(token: str, secret: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed token")
    header_b64, payload_b64, sig_b64 = parts
    expected = _jwt_sign(header_b64, payload_b64, secret)
    if not hmac.compare_digest(sig_b64, expected):
        raise ValueError("Invalid signature")
    payload = json.loads(_b64u_dec(payload_b64))
    if payload.get("exp", 0) < time.time():
        raise ValueError("Token expired")
    return payload


# ─────────────────────────────────────────────────────────────────────
#  JWTManager — secret persistence + token issuance + blocklist
# ─────────────────────────────────────────────────────────────────────

_SECRET_FILE    = Path(".checkpoints/jwt_secret.txt")
_BLOCKLIST_FILE = Path(".checkpoints/jwt_blocklist.json")


class JWTManager:
    """Manages JWT signing secret and refresh-token revocation list."""

    def __init__(self) -> None:
        self._secret: str      = self._load_or_create()
        self._blocklist: Set[str] = self._load_blocklist()

    # ── Secret lifecycle ──────────────────────────────────────────

    def _load_or_create(self) -> str:
        if _SECRET_FILE.exists():
            s = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if s:
                logger.debug("JWT secret loaded from %s", _SECRET_FILE)
                return s
        return self._save_new_secret()

    def _save_new_secret(self) -> str:
        secret = secrets.token_urlsafe(48)
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(secret, encoding="utf-8")
        return secret

    def _load_blocklist(self) -> Set[str]:
        if not _BLOCKLIST_FILE.exists():
            return set()
        try:
            data = json.loads(_BLOCKLIST_FILE.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()

    def _save_blocklist(self) -> None:
        try:
            _BLOCKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _BLOCKLIST_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self._blocklist)), encoding="utf-8")
            tmp.replace(_BLOCKLIST_FILE)
        except Exception as exc:
            logger.warning("JWT blocklist save failed: %s", exc)

    def rotate(self) -> None:
        """Generate a new secret and save it. All existing tokens immediately invalid."""
        self._secret = self._save_new_secret()
        self._blocklist.clear()
        self._save_blocklist()
        logger.info("JWT secret rotated — all existing sessions invalidated.")

    # ── Token issuance ────────────────────────────────────────────

    def issue_access(self) -> str:
        exp = int(time.time()) + config.JWT_ACCESS_EXPIRES_MINUTES * 60
        return _jwt_encode({"sub": "dashboard", "type": "access",
                            "iat": int(time.time()), "exp": exp}, self._secret)

    def issue_refresh(self) -> str:
        jti = secrets.token_urlsafe(16)
        exp = int(time.time()) + config.JWT_REFRESH_EXPIRES_DAYS * 86400
        return _jwt_encode({"sub": "dashboard", "type": "refresh",
                            "jti": jti, "iat": int(time.time()), "exp": exp}, self._secret)

    # ── Token verification ────────────────────────────────────────

    def verify_access(self, token: str) -> dict:
        payload = _jwt_decode(token, self._secret)
        if payload.get("type") != "access":
            raise ValueError("Not an access token")
        return payload

    def verify_refresh(self, token: str) -> dict:
        payload = _jwt_decode(token, self._secret)
        if payload.get("type") != "refresh":
            raise ValueError("Not a refresh token")
        if payload.get("jti", "") in self._blocklist:
            raise ValueError("Token revoked")
        return payload

    def revoke_refresh(self, token: str) -> None:
        try:
            payload = _jwt_decode(token, self._secret)
            jti = payload.get("jti", "")
            if jti:
                self._blocklist.add(jti)
                self._save_blocklist()
        except Exception:
            pass  # already invalid — nothing to do

    def rotate_refresh(self, old_token: str) -> str:
        """Revoke the old refresh token and issue a new one (token rotation)."""
        self.revoke_refresh(old_token)
        return self.issue_refresh()


# ─────────────────────────────────────────────────────────────────────
#  Login rate limiter — in-memory, per-IP
# ─────────────────────────────────────────────────────────────────────

class _LoginRateLimiter:
    """
    Simple in-memory rate limiter for login attempts.
    Allows MAX_ATTEMPTS per IP within WINDOW_SECONDS.
    Locks the IP for LOCKOUT_SECONDS after too many failures.
    """
    MAX_ATTEMPTS    = 10
    WINDOW_SECONDS  = 60
    LOCKOUT_SECONDS = 300

    def __init__(self) -> None:
        self._attempts: Dict[str, list] = defaultdict(list)  # ip → [timestamp, ...]
        self._locked:   Dict[str, float] = {}                 # ip → unlock_time
        self._lock = threading.Lock()

    def is_allowed(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            if ip in self._locked:
                if now < self._locked[ip]:
                    return False
                del self._locked[ip]
                self._attempts[ip] = []
            cutoff = now - self.WINDOW_SECONDS
            self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]
            return len(self._attempts[ip]) < self.MAX_ATTEMPTS

    def record_attempt(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._attempts[ip].append(now)
            if len(self._attempts[ip]) >= self.MAX_ATTEMPTS:
                self._locked[ip] = now + self.LOCKOUT_SECONDS
                logger.warning("Login rate limit: IP %s locked out for %ds", ip, self.LOCKOUT_SECONDS)


# ─────────────────────────────────────────────────────────────────────
#  DashboardServer
# ─────────────────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self, trade_journal, checkpoint_service, weekly_runner=None, balance_fn=None) -> None:
        self.journal      = trade_journal
        self.checkpoint   = checkpoint_service
        self._weekly      = weekly_runner
        self._balance_fn  = balance_fn   # callable() -> float | None, fetches live MT5 balance
        self._thread: Optional[threading.Thread] = None
        self._jwt         = JWTManager()
        self._password    = self._resolve_password()
        self._rate_limiter = _LoginRateLimiter()

    def _resolve_password(self) -> str:
        pw = getattr(config, "DASHBOARD_PASSWORD", "").strip()
        if pw:
            return pw
        generated = secrets.token_urlsafe(16)
        logger.warning(
            "┌──────────────────────────────────────────────────────┐\n"
            "│  DASHBOARD_PASSWORD not set — auto-generated:         │\n"
            "│  %-52s │\n"
            "│  Add DASHBOARD_PASSWORD=<above> to your .env file.   │\n"
            "└──────────────────────────────────────────────────────┘",
            generated,
        )
        return generated

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="Dashboard"
        )
        self._thread.start()
        port = getattr(config, "DASHBOARD_PORT", 8080)
        logger.info("Dashboard starting at http://0.0.0.0:%d", port)

    # ── Flask app ────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            from flask import Flask, jsonify, request, send_file
        except ImportError:
            logger.error(
                "Flask not installed — dashboard unavailable. "
                "Run: wine python -m pip install flask"
            )
            return

        app = Flask(__name__)
        app.config["JSON_SORT_KEYS"] = False

        jwt_mgr      = self._jwt
        password     = self._password
        rate_limiter = self._rate_limiter

        # ── Auth helpers ─────────────────────────────────────────────

        def require_auth(f):
            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    return jsonify({"error": "Missing Authorization header"}), 401
                try:
                    jwt_mgr.verify_access(auth[7:])
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 401
                return f(*args, **kwargs)
            return wrapper

        def _check_password(candidate: str) -> bool:
            return hmac.compare_digest(
                candidate.encode("utf-8"),
                password.encode("utf-8"),
            )

        # ── Auth endpoints (public) ──────────────────────────────────

        @app.route("/api/auth/login", methods=["POST"])
        def auth_login():
            ip = request.remote_addr or "unknown"
            if not rate_limiter.is_allowed(ip):
                return jsonify({"error": "Too many login attempts — try again later"}), 429
            data = request.get_json(silent=True) or {}
            if not _check_password(data.get("password", "")):
                rate_limiter.record_attempt(ip)
                logger.warning("Dashboard: failed login from %s", ip)
                return jsonify({"error": "Invalid password"}), 401
            logger.info("Dashboard: login from %s", ip)
            return jsonify({
                "access_token":  jwt_mgr.issue_access(),
                "refresh_token": jwt_mgr.issue_refresh(),
                "expires_in":    config.JWT_ACCESS_EXPIRES_MINUTES * 60,
            })

        @app.route("/api/auth/refresh", methods=["POST"])
        def auth_refresh():
            data = request.get_json(silent=True) or {}
            rt   = data.get("refresh_token", "")
            try:
                jwt_mgr.verify_refresh(rt)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 401
            # Rotate: revoke old token and issue a fresh one so stolen refresh
            # tokens can only be used once.
            new_rt = jwt_mgr.rotate_refresh(rt)
            return jsonify({
                "access_token":  jwt_mgr.issue_access(),
                "refresh_token": new_rt,
                "expires_in":    config.JWT_ACCESS_EXPIRES_MINUTES * 60,
            })

        @app.route("/api/auth/logout", methods=["POST"])
        def auth_logout():
            data = request.get_json(silent=True) or {}
            rt   = data.get("refresh_token", "")
            if rt:
                jwt_mgr.revoke_refresh(rt)
            logger.info("Dashboard: logout from %s", request.remote_addr)
            return jsonify({"ok": True})

        @app.route("/api/auth/rotate-secret", methods=["POST"])
        def auth_rotate():
            data = request.get_json(silent=True) or {}
            if not _check_password(data.get("password", "")):
                return jsonify({"error": "Invalid password"}), 401
            jwt_mgr.rotate()
            logger.info("Dashboard: JWT secret rotated by %s", request.remote_addr)
            return jsonify({
                "ok": True,
                "message": "Secret rotated. All existing sessions have been invalidated.",
            })

        # ── Static ──────────────────────────────────────────────────
        # Root serves the dashboard HTML shell (which contains the login form).
        # No auth here — the JS frontend authenticates via /api/auth/login and
        # then uses the returned JWT for all subsequent /api/* calls.

        @app.route("/")
        def index():
            p = Path("src/dashboard/index.html")
            if p.exists():
                return send_file(str(p.resolve()))
            return "<h2>Dashboard HTML not found at src/dashboard/index.html</h2>", 404

        # ── Status ──────────────────────────────────────────────────

        @app.route("/api/status")
        @require_auth
        def status():
            cp  = self.checkpoint.load() or {}
            uptime_secs = int(
                (datetime.now(tz=timezone.utc) - _BOT_START_TIME).total_seconds()
            )
            return jsonify({
                "running":          True,
                "uptime_seconds":   uptime_secs,
                "server_time":      datetime.now(tz=timezone.utc).isoformat(),
                "last_checkpoint":  cp.get("saved_at"),
                "symbols":          config.SYMBOLS,
                "risk_pct":         config.RISK_PERCENT,
                "min_rr":           config.MIN_RR,
            })

        # ── Analytics ───────────────────────────────────────────────

        @app.route("/api/stats")
        @require_auth
        def stats():
            days = request.args.get("days", type=int)  # None = all time

            # reset_at: ISO timestamp of last reset — stats only count trades after this
            initial_bal, bal_set_at, reset_at = _get_initial_balance()
            since = reset_at  # Option A: time-gated stats post-reset

            g = self.journal.get_stats(days=days, since=since)

            # Current balance: use live MT5 value if available, else fall back to journal estimate
            if self._balance_fn:
                try:
                    current_bal = self._balance_fn() or initial_bal
                except Exception:
                    # Only sum broker-truth (v2+) money records — never legacy pip rows.
                    current_bal = initial_bal + self.journal.total_realized_pnl(since=since)
            else:
                current_bal = initial_bal + self.journal.total_realized_pnl(since=since)
            change_pct = round((current_bal - initial_bal) / initial_bal * 100, 2) if initial_bal else 0.0

            g["initial_balance"]    = round(initial_bal, 2)
            g["current_balance"]    = round(current_bal, 2)
            g["balance_change_pct"] = change_pct
            g["balance_set_at"]     = bal_set_at
            g["reset_at"]           = reset_at
            g["currency"]           = config.ACCOUNT_CURRENCY
            g["drawdown_pct"]       = self.journal.get_drawdown(initial_bal, days=days, since=since)
            g["period_days"]        = days

            return jsonify({
                "global":    g,
                "by_symbol": self.journal.get_stats_by_symbol(days=days, since=since),
            })

        @app.route("/api/trades")
        @require_auth
        def trades():
            n = int(request.args.get("n", 50))
            return jsonify({"trades": self.journal.get_recent(n)})

        # ── Config ──────────────────────────────────────────────────

        @app.route("/api/config", methods=["GET"])
        @require_auth
        def get_config():
            # Use getattr with defaults so a renamed/removed config key can never
            # 500 the dashboard (config drift must degrade gracefully, not crash).
            return jsonify({
                "symbols":             getattr(config, "SYMBOLS", []),
                "risk_pct":            getattr(config, "RISK_PERCENT", 0.0),
                "min_rr":              getattr(config, "MIN_RR", 1.5),
                # The portfolio model is now per-symbol; surface the unique-symbol cap.
                "max_open_trades":     getattr(config, "MAX_OPEN_SYMBOLS",
                                               getattr(config, "MAX_OPEN_TRADES", 5)),
                "max_trades_per_symbol": getattr(config, "MAX_TRADES_PER_SYMBOL", 4),
                "atr_cost_threshold":  getattr(config, "ATR_COST_THRESHOLD", 0.25),
                "loop_interval":       getattr(config, "LOOP_INTERVAL_SECONDS", 1),
                "tiered_tp_enabled":   getattr(config, "TIERED_TP_ENABLED", True),
                "tiered_tp1_ratio":    getattr(config, "TIERED_TP1_RATIO", 1.5),
                "tiered_tp2_ratio":    getattr(config, "TIERED_TP2_RATIO", 2.0),
                "monitor_enabled":     getattr(config, "MONITOR_ENABLED", True),
                "breakeven_rr":        getattr(config, "BREAKEVEN_RR_TRIGGER", 1.5),
                "backtest_balance":    getattr(config, "BACKTEST_INITIAL_BALANCE",
                                               getattr(config, "ACCOUNT_BALANCE", 0.0)),
                "account_currency":    getattr(config, "ACCOUNT_CURRENCY", "USD"),
            })

        @app.route("/api/config", methods=["PATCH"])
        @require_auth
        def update_config():
            data    = request.get_json(silent=True) or {}
            changed = []
            for key, (attr, cast) in _CONFIG_MAP.items():
                if key in data:
                    try:
                        setattr(config, attr, cast(data[key]))
                        changed.append(key)
                    except (ValueError, TypeError) as exc:
                        logger.warning("Config update: bad value for %s — %s", key, exc)
            if changed:
                logger.info("Config updated via dashboard: %s", changed)
                _persist_config_overrides(changed, _CONFIG_MAP)
            return jsonify({"updated": changed, "ok": True})

        # ── Symbols ─────────────────────────────────────────────────

        @app.route("/api/symbols", methods=["GET"])
        @require_auth
        def get_symbols():
            return jsonify({"symbols": config.SYMBOLS})

        @app.route("/api/symbols", methods=["POST"])
        @require_auth
        def add_symbol():
            data = request.get_json(silent=True) or {}
            sym  = data.get("symbol", "").upper().strip()
            if not sym:
                return jsonify({"ok": False, "error": "Missing symbol"}), 400
            if sym in config.SYMBOLS:
                return jsonify({"ok": False, "error": f"{sym} already active"}), 400
            config.SYMBOLS.append(sym)
            _persist_symbols()
            logger.info("Symbol added via dashboard: %s", sym)
            return jsonify({"ok": True, "symbols": config.SYMBOLS})

        @app.route("/api/symbols/<symbol>", methods=["DELETE"])
        @require_auth
        def remove_symbol(symbol):
            sym = symbol.upper()
            if sym not in config.SYMBOLS:
                return jsonify({"ok": False, "error": f"{sym} not found"}), 404
            config.SYMBOLS.remove(sym)
            _persist_symbols()
            logger.info("Symbol removed via dashboard: %s", sym)
            return jsonify({"ok": True, "symbols": config.SYMBOLS})

        # ── Balance history ──────────────────────────────────────────

        @app.route("/api/balance-history", methods=["GET"])
        @require_auth
        def get_balance_history():
            return jsonify({"history": _load_balance_history()})

        @app.route("/api/balance-history", methods=["POST"])
        @require_auth
        def add_balance_event():
            data = request.get_json(silent=True) or {}
            try:
                balance = float(data["balance"])
                if balance <= 0:
                    raise ValueError("must be positive")
            except (KeyError, ValueError, TypeError) as exc:
                return jsonify({"ok": False, "error": f"Invalid balance: {exc}"}), 400

            note    = str(data.get("note", "")).strip() or "Balance updated"
            history = _load_balance_history()
            event   = {
                "balance":  round(balance, 2),
                "currency": config.ACCOUNT_CURRENCY,
                "set_at":   datetime.now(tz=timezone.utc).isoformat(),
                "note":     note,
            }
            history.append(event)
            _save_balance_history(history)
            logger.info("Balance history updated: %.2f %s — %s",
                        balance, config.ACCOUNT_CURRENCY, note)
            return jsonify({"ok": True, "history": history})

        # ── Reset balance ────────────────────────────────────────────

        @app.route("/api/reset-balance", methods=["POST"])
        @require_auth
        def reset_balance():
            """
            Reset performance tracking to the current account equity.

            Appends a 'reset' event to balance_history.json with the live
            MT5 equity (or journal-estimated equity if MT5 is unavailable).
            After reset, /api/stats only counts trades closed after this
            timestamp — historical trades remain visible in /api/trades
            but do not affect performance metrics (Option A).

            Body (JSON):
              { "note": "optional reason" }   — note is optional
            """
            data = request.get_json(silent=True) or {}
            note = str(data.get("note", "")).strip() or "Performance reset"

            # Resolve current equity: prefer live MT5, fall back to journal estimate
            initial_bal, _, _ = _get_initial_balance()
            if self._balance_fn:
                try:
                    current_equity = self._balance_fn() or initial_bal
                except Exception:
                    # BROKER-TRUTH only — total_realized_pnl() excludes legacy
                    # pip-era rows. Never sum get_all() raw (it counts fictional P&L).
                    current_equity = initial_bal + self.journal.total_realized_pnl()
            else:
                current_equity = initial_bal + self.journal.total_realized_pnl()

            reset_time = datetime.now(tz=timezone.utc).isoformat()
            history    = _load_balance_history()
            history.append({
                "balance":  round(current_equity, 2),
                "currency": config.ACCOUNT_CURRENCY,
                "set_at":   reset_time,
                "type":     "reset",   # marker so _get_initial_balance() can find it
                "note":     note,
            })
            _save_balance_history(history)
            logger.info(
                "Balance reset by dashboard: new baseline %.2f %s at %s",
                current_equity, config.ACCOUNT_CURRENCY, reset_time,
            )
            return jsonify({
                "ok":           True,
                "new_balance":  round(current_equity, 2),
                "currency":     config.ACCOUNT_CURRENCY,
                "reset_at":     reset_time,
                "note":         note,
            })

        # ── Manual weekly backtest trigger ──────────────────────────

        @app.route("/api/run-weekly-backtest", methods=["POST"])
        @require_auth
        def run_weekly_backtest():
            if not self._weekly:
                return jsonify({"ok": False, "error": "Weekly runner not wired up"}), 503
            import threading
            threading.Thread(
                target=self._weekly.run,
                daemon=True,
                name="ManualWeeklyBT",
            ).start()
            logger.info("Dashboard: manual weekly backtest triggered.")
            return jsonify({
                "ok": True,
                "message": "Backtest running in background. Watch the Logs tab — email arrives when done.",
            })

        # ── Tick Velocity Analytics ─────────────────────────────────

        @app.route("/api/velocity-report")
        @require_auth
        def velocity_report():
            """
            Returns the full tick velocity analytics data for the dashboard.
            Combines:
              - tick_velocity_report.txt  (human-readable summary)
              - tick_velocity_daily.jsonl  (last 14 days of daily aggregates)
              - tick_velocity_hourly.jsonl (last 48 hourly records)
              - tick_velocity_weekly.jsonl (last 4 weekly records + recommendations)
            """
            analytics_dir = Path(
                __import__("os").environ.get("BOT_ANALYTICS_DIR", "/bot/analytics")
            )

            def _read_jsonl(path: Path, tail: int) -> list:
                if not path.exists():
                    return []
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    records = []
                    for ln in lines[-tail:]:
                        ln = ln.strip()
                        if ln:
                            try:
                                records.append(json.loads(ln))
                            except Exception:
                                pass
                    return records
                except Exception:
                    return []

            report_text  = ""
            report_age   = None
            report_path  = analytics_dir / "tick_velocity_report.txt"
            if report_path.exists():
                try:
                    report_text = report_path.read_text(encoding="utf-8", errors="replace")
                    report_age  = int(
                        (datetime.now(tz=timezone.utc) -
                         datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc)
                        ).total_seconds()
                    )
                except Exception:
                    pass

            daily   = _read_jsonl(analytics_dir / "tick_velocity_daily.jsonl",  14)
            hourly  = _read_jsonl(analytics_dir / "tick_velocity_hourly.jsonl", 48)
            weekly  = _read_jsonl(analytics_dir / "tick_velocity_weekly.jsonl",  4)

            return jsonify({
                "report_text":        report_text,
                "report_age_seconds": report_age,
                "daily":              daily,
                "hourly":             hourly,
                "weekly":             weekly,
                "analytics_dir":      str(analytics_dir),
            })

        @app.route("/api/velocity-report/refresh", methods=["POST"])
        @require_auth
        def velocity_report_refresh():
            """Trigger an immediate daily analytics run."""
            try:
                from src.application.tick_analytics import get_analytics
                get_analytics().run_daily(datetime.now(tz=timezone.utc))
                return jsonify({"ok": True, "message": "Daily report regenerated."})
            except Exception as exc:
                logger.warning("Manual velocity report refresh failed: %s", exc)
                return jsonify({"ok": False, "error": str(exc)}), 500

        # ── Log tail ────────────────────────────────────────────────

        @app.route("/api/logs")
        @require_auth
        def logs():
            n    = min(int(request.args.get("n", 100)), 1000)  # cap at 1000 lines
            logf = Path("logs/trading_bot.log")
            if not logf.exists():
                return jsonify({"lines": []})
            try:
                # Read whole file; for a 3-day rotation the file stays manageable.
                # Slicing the last n lines is exact unlike the old chunk-size heuristic.
                raw   = logf.read_text(encoding="utf-8", errors="replace")
                lines = raw.splitlines()
                return jsonify({"lines": lines[-n:]})
            except Exception as exc:
                return jsonify({"lines": [], "error": str(exc)})

        # ── Backtest reports ─────────────────────────────────────────

        @app.route("/api/backtest-reports")
        @require_auth
        def backtest_reports():
            report_dir = Path("src/backtest_results")
            if not report_dir.exists():
                return jsonify({"reports": []})
            reports = sorted(
                [
                    {
                        "name":     p.name,
                        "size":     p.stat().st_size,
                        "modified": datetime.fromtimestamp(
                            p.stat().st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                    for p in report_dir.glob("*.txt")
                ],
                key=lambda r: r["modified"],
                reverse=True,
            )
            return jsonify({"reports": reports})

        @app.route("/api/backtest-reports/<path:filename>", methods=["GET", "DELETE"])
        @require_auth
        def get_backtest_report(filename):
            report_dir = Path("src/backtest_results")
            p = (report_dir / filename).resolve()
            # Guard against path traversal
            if not str(p).startswith(str(report_dir.resolve())):
                return jsonify({"error": "Invalid path"}), 400
            if not p.exists():
                return jsonify({"error": "Not found"}), 404

            if request.method == "DELETE":
                try:
                    p.unlink()
                    return jsonify({"ok": True})
                except Exception as exc:
                    return jsonify({"error": str(exc)}), 500

            return p.read_text(encoding="utf-8", errors="replace"), 200, {
                "Content-Type": "text/plain; charset=utf-8"
            }

        # ── Start ────────────────────────────────────────────────────

        port = getattr(config, "DASHBOARD_PORT", 8080)
        try:
            app.run(
                host="0.0.0.0",
                port=port,
                debug=False,
                use_reloader=False,
                threaded=True,
            )
        except Exception as exc:
            logger.error("Dashboard server crashed: %s", exc)
