"""AI coding assistant usage quota polling.

Claude:
    GET https://claude.ai/api/organizations/{uuid}/usage
    Auth: sessionKey cookie via ``curl_cffi`` (TLS fingerprint impersonation).
    Cookie file default: ~/.config/claudeai/claude.ai_cookies.txt

Codex:
    GET https://chatgpt.com/backend-api/wham/usage
    Auth: Bearer token from ~/.codex/auth.json (written by ``codex login``).
"""

import http.cookiejar
import json as _json
import logging
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("feishu-bridge.quota")

_DEFAULT_COOKIE_PATH = Path.home() / ".config" / "claudeai" / "claude.ai_cookies.txt"
_BASE_URL = "https://claude.ai/api"
_DEFAULT_POLL_INTERVAL = 300  # 5 minutes
_REQUEST_TIMEOUT = 15
_IMPERSONATE = "chrome131"

# Shared label mapping for quota window keys → display names.
# Used by worker.py and commands.py to render quota alerts.
WINDOW_LABELS: dict[str, str] = {
    "five_hour": "5h",
    "seven_day": "7d",
    "seven_day_opus": "7d-opus",
    "seven_day_sonnet": "7d-sonnet",
}


@dataclass
class QuotaWindow:
    """Single rate-limit window snapshot."""
    utilization: float  # percentage, e.g. 18.0 means 18%
    resets_at: str  # ISO-8601 timestamp
    resets_at_epoch: float = 0.0  # unix timestamp (derived)


_COOKIE_EXPIRY_WARN_DAYS = 3  # warn when sessionKey expires within N days


@dataclass
class QuotaSnapshot:
    """Point-in-time usage quota from the API."""
    timestamp: float = 0.0
    windows: dict[str, QuotaWindow] = field(default_factory=dict)
    extra_usage_enabled: bool = False
    raw: dict = field(default_factory=dict)
    error: str | None = None
    cookie_expires_at: float = 0.0  # epoch; 0 = unknown

    poll_interval: int = _DEFAULT_POLL_INTERVAL  # set by QuotaPoller

    @property
    def stale(self) -> bool:
        """Snapshot older than 2x poll interval is considered stale."""
        return (time.time() - self.timestamp) > self.poll_interval * 2

    @property
    def available(self) -> bool:
        return bool(self.windows) and not self.error

    @property
    def cookie_expiry_warning(self) -> str | None:
        """Return a warning string if sessionKey expires soon, else None."""
        if not self.cookie_expires_at:
            return None
        remaining = self.cookie_expires_at - time.time()
        if remaining <= 0:
            return "⚠️ sessionKey 已过期，请重新导出 cookie"
        days = remaining / 86400
        if days <= _COOKIE_EXPIRY_WARN_DAYS:
            return f"⚠️ sessionKey 将在 {days:.1f} 天后过期，请尽快更新 cookie"
        return None


def _parse_iso_to_epoch(iso_str: str) -> float:
    """Best-effort ISO-8601 -> epoch conversion."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.timestamp()
    except Exception:
        return 0.0


def _parse_response(data: dict, poll_interval: int = _DEFAULT_POLL_INTERVAL) -> QuotaSnapshot:
    """Parse API JSON into a QuotaSnapshot."""
    windows: dict[str, QuotaWindow] = {}
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
                "seven_day_oauth_apps", "seven_day_cowork", "iguana_necktie"):
        val = data.get(key)
        if val is None:
            continue
        w = QuotaWindow(
            utilization=val.get("utilization", 0.0),
            resets_at=val.get("resets_at", ""),
        )
        w.resets_at_epoch = _parse_iso_to_epoch(w.resets_at)
        windows[key] = w

    extra = data.get("extra_usage", {}) or {}
    return QuotaSnapshot(
        timestamp=time.time(),
        windows=windows,
        extra_usage_enabled=bool(extra.get("is_enabled")),
        raw=data,
        poll_interval=poll_interval,
    )


def _load_session_key(cookie_path: Path) -> tuple[str | None, float]:
    """Extract sessionKey value and expiry from Netscape cookie file.

    Returns:
        (value, expires_epoch) — value is None if not found;
        expires_epoch is 0.0 if unknown.
    """
    try:
        cj = http.cookiejar.MozillaCookieJar(str(cookie_path))
        cj.load(ignore_discard=True, ignore_expires=True)
        for cookie in cj:
            if cookie.name == "sessionKey" and cookie.domain.endswith("claude.ai"):
                return cookie.value, float(cookie.expires or 0)
    except Exception as e:
        log.warning("Failed to load cookies from %s: %s", cookie_path, e)
    return None, 0.0


class QuotaPoller:
    """Background thread that polls claude.ai usage API.

    Uses ``curl_cffi`` for TLS fingerprint impersonation to bypass
    Cloudflare without needing short-lived browser cookies.

    Usage::

        poller = QuotaPoller(cookie_path="/path/to/cookies.txt")
        poller.start()

        snap = poller.snapshot  # thread-safe read
        if snap.available:
            print(snap.windows["seven_day"].utilization)

        poller.stop()
    """

    def __init__(
        self,
        cookie_path: str | Path | None = None,
        poll_interval: int = _DEFAULT_POLL_INTERVAL,
        org_uuid: str | None = None,
    ):
        self._cookie_path = Path(cookie_path) if cookie_path else _DEFAULT_COOKIE_PATH
        self._poll_interval = poll_interval
        self._org_uuid = org_uuid
        self._session_key: str | None = None
        self._session_key_expires: float = 0.0
        self._cookie_mtime: float = 0.0
        self._snapshot = QuotaSnapshot()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_failures = 0

    # --- Public API ---

    @property
    def snapshot(self) -> QuotaSnapshot:
        with self._lock:
            return self._snapshot

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if not self._cookie_path.exists():
            log.warning("Cookie file not found: %s — quota poller disabled",
                        self._cookie_path)
            return
        self._session_key, self._session_key_expires = _load_session_key(self._cookie_path)
        if not self._session_key:
            log.warning("No sessionKey in %s — quota poller disabled",
                        self._cookie_path)
            return
        try:
            self._cookie_mtime = self._cookie_path.stat().st_mtime
        except OSError:
            self._cookie_mtime = 0.0
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="quota-poller", daemon=True,
        )
        self._thread.start()
        log.info("Quota poller started (interval=%ds)", self._poll_interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def force_refresh(self) -> QuotaSnapshot:
        """Synchronous one-shot refresh (for /status command)."""
        self._maybe_reload_cookie()
        if not self._session_key:
            self._session_key, self._session_key_expires = _load_session_key(self._cookie_path)
        snap = self._fetch()
        with self._lock:
            self._snapshot = snap
        return snap

    # --- Internal ---

    def _run(self):
        self._poll_once()
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._poll_interval)
            if self._stop_event.is_set():
                break
            self._poll_once()

    def _maybe_reload_cookie(self):
        """Reload session key if the cookie file was modified on disk."""
        try:
            mtime = self._cookie_path.stat().st_mtime
        except OSError:
            return
        if mtime > self._cookie_mtime:
            old_expires = self._session_key_expires
            self._session_key, self._session_key_expires = _load_session_key(self._cookie_path)
            self._cookie_mtime = mtime
            if self._session_key_expires != old_expires:
                log.info("Cookie file changed — reloaded sessionKey (expires %s)",
                         time.strftime("%Y-%m-%d %H:%M", time.localtime(self._session_key_expires)))

    def _poll_once(self):
        self._maybe_reload_cookie()
        snap = self._fetch()
        with self._lock:
            self._snapshot = snap
        if snap.error:
            self._consecutive_failures += 1
            if self._consecutive_failures <= 3:
                log.warning("Quota poll failed (%d): %s",
                            self._consecutive_failures, snap.error)
            # Reload session key on auth failures (cookie may have been refreshed)
            if self._consecutive_failures == 2:
                self._session_key, self._session_key_expires = _load_session_key(self._cookie_path)
        else:
            if self._consecutive_failures > 0:
                log.info("Quota poll recovered after %d failures",
                         self._consecutive_failures)
            self._consecutive_failures = 0

    def _fetch(self) -> QuotaSnapshot:
        if not self._session_key:
            return QuotaSnapshot(timestamp=time.time(),
                                 error="no sessionKey available",
                                 cookie_expires_at=self._session_key_expires)

        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            return QuotaSnapshot(timestamp=time.time(),
                                 error="curl_cffi not installed")

        # Auto-discover org UUID if not set
        if not self._org_uuid:
            self._org_uuid = self._discover_org(cffi_requests)
            if not self._org_uuid:
                return QuotaSnapshot(timestamp=time.time(),
                                     error="failed to discover org UUID")

        url = f"{_BASE_URL}/organizations/{self._org_uuid}/usage"
        try:
            resp = cffi_requests.get(
                url,
                cookies={"sessionKey": self._session_key},
                headers={"Accept": "application/json"},
                impersonate=_IMPERSONATE,
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                return QuotaSnapshot(
                    timestamp=time.time(),
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )
            snap = _parse_response(resp.json(), self._poll_interval)
            snap.cookie_expires_at = self._session_key_expires
            return snap
        except Exception as e:
            return QuotaSnapshot(timestamp=time.time(), error=str(e),
                                 cookie_expires_at=self._session_key_expires)

    def _discover_org(self, cffi_requests) -> str | None:
        """GET /api/organizations -> first org UUID."""
        url = f"{_BASE_URL}/organizations"
        try:
            resp = cffi_requests.get(
                url,
                cookies={"sessionKey": self._session_key},
                headers={"Accept": "application/json"},
                impersonate=_IMPERSONATE,
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                orgs = resp.json()
                if orgs and isinstance(orgs, list):
                    uuid = orgs[0].get("uuid")
                    if uuid:
                        log.info("Discovered org UUID: %s", uuid)
                        return uuid
        except Exception as e:
            log.warning("Org discovery failed: %s", e)
        return None


# ── Codex (OpenAI) quota ──────────────────────────────────────────────

_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


@dataclass
class CodexQuotaSnapshot:
    """Point-in-time Codex usage quota."""
    timestamp: float = 0.0
    plan_type: str = ""
    allowed: bool = True
    # primary = 5h window, secondary = 7d window
    primary_used_pct: float = 0.0
    primary_resets_at: float = 0.0
    secondary_used_pct: float = 0.0
    secondary_resets_at: float = 0.0
    error: str | None = None

    @property
    def available(self) -> bool:
        return not self.error

    @property
    def stale(self) -> bool:
        return (time.time() - self.timestamp) > _DEFAULT_POLL_INTERVAL * 2


def fetch_codex_quota(auth_path: Path | None = None) -> CodexQuotaSnapshot:
    """One-shot fetch of Codex usage quota.

    Reads ``~/.codex/auth.json`` for the Bearer token, calls the
    ChatGPT backend API, and returns a snapshot.  No Cloudflare bypass
    needed — the API accepts plain Bearer auth.
    """
    auth_file = auth_path or _CODEX_AUTH_PATH
    if not auth_file.exists():
        return CodexQuotaSnapshot(timestamp=time.time(),
                                  error="~/.codex/auth.json not found")

    try:
        with open(auth_file) as f:
            tokens = _json.load(f).get("tokens", {})
        access_token = tokens.get("access_token")
        if not access_token:
            return CodexQuotaSnapshot(timestamp=time.time(),
                                      error="no access_token in auth.json")
    except Exception as e:
        return CodexQuotaSnapshot(timestamp=time.time(), error=f"auth read: {e}")

    req = urllib.request.Request(_CODEX_USAGE_URL)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    account_id = tokens.get("account_id", "")
    if account_id:
        req.add_header("ChatGPT-Account-Id", account_id)

    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            data = _json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return CodexQuotaSnapshot(timestamp=time.time(),
                                  error=f"HTTP {e.code}: {e.read().decode()[:100]}")
    except Exception as e:
        return CodexQuotaSnapshot(timestamp=time.time(), error=str(e))

    rl = data.get("rate_limit", {})
    pw = rl.get("primary_window") or {}
    sw = rl.get("secondary_window") or {}

    return CodexQuotaSnapshot(
        timestamp=time.time(),
        plan_type=data.get("plan_type", ""),
        allowed=rl.get("allowed", True),
        primary_used_pct=pw.get("used_percent", 0),
        primary_resets_at=pw.get("reset_at", 0),
        secondary_used_pct=sw.get("used_percent", 0),
        secondary_resets_at=sw.get("reset_at", 0),
    )


# ── DeepSeek balance ─────────────────────────────────────────────────

_DEEPSEEK_ENV_PATH = Path.home() / ".pi" / "agent" / ".env"
_DEEPSEEK_KEY_VAR = "DEEPSEEK_API_KEY"
_DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"


@dataclass
class DeepseekBalanceSnapshot:
    """Point-in-time DeepSeek account balance."""
    timestamp: float = 0.0
    balance: float = 0.0
    currency: str = "CNY"
    is_available: bool = True
    error: str | None = None

    @property
    def available(self) -> bool:
        return not self.error

    @property
    def stale(self) -> bool:
        return (time.time() - self.timestamp) > _DEFAULT_POLL_INTERVAL * 2


def _read_env_key(env_path: Path, key_var: str) -> str | None:
    """Read a single key=value from a dotenv file."""
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key_var:
                    return v.strip().strip('"').strip("'")
        return None
    except Exception:
        return None


def fetch_deepseek_balance(env_path: Path | None = None,
                           key_var: str | None = None) -> DeepseekBalanceSnapshot:
    """One-shot fetch of DeepSeek account balance.

    Reads ``DEEPSEEK_API_KEY`` from ``~/.pi/agent/.env``, calls
    ``api.deepseek.com/user/balance``, returns snapshot.
    """
    ep = env_path or _DEEPSEEK_ENV_PATH
    kv = key_var or _DEEPSEEK_KEY_VAR
    api_key = _read_env_key(ep, kv)
    if not api_key:
        return DeepseekBalanceSnapshot(
            timestamp=time.time(),
            error=f"{kv} not found in {ep}",
        )

    req = urllib.request.Request(_DEEPSEEK_BALANCE_URL)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            data = _json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        return DeepseekBalanceSnapshot(
            timestamp=time.time(),
            error=f"HTTP {e.code}: {body}",
        )
    except Exception as e:
        return DeepseekBalanceSnapshot(timestamp=time.time(), error=str(e))

    is_available = data.get("is_available", True)
    if not is_available:
        return DeepseekBalanceSnapshot(
            timestamp=time.time(),
            is_available=False,
            error="account not available",
        )

    balance_infos = data.get("balance_infos") or []
    bi = balance_infos[0] if balance_infos else {}
    total_balance = float(bi.get("total_balance", "0") or "0")
    currency = bi.get("currency", "CNY")

    return DeepseekBalanceSnapshot(
        timestamp=time.time(),
        balance=total_balance,
        currency=currency,
        is_available=True,
    )


# ── CLI entry point (`python -m feishu_bridge.quota claude [--json]`) ──

def _claude_quota_cli(cookie_path: Path | None = None, json_out: bool = True) -> str:
    """One-shot Claude quota fetch, returns JSON string.

    If ``json_out`` is True (default), output is always JSON.
    Otherwise, human-readable text.
    """
    cp = Path(cookie_path) if cookie_path else _DEFAULT_COOKIE_PATH

    # Check cookie file exists
    if not cp.exists():
        result = {"error": f"cookie file not found: {cp}", "available": False}
        if json_out:
            return _json.dumps(result, ensure_ascii=False)
        return f"Error: {result['error']}"

    # Load session key
    session_key, expires = _load_session_key(cp)
    if not session_key:
        result = {"error": "no sessionKey in cookie file", "available": False}
        if json_out:
            return _json.dumps(result, ensure_ascii=False)
        return f"Error: {result['error']}"

    poller = QuotaPoller(cookie_path=cp)
    poller._session_key = session_key
    poller._session_key_expires = expires
    try:
        snap = poller.force_refresh()
    except Exception as e:
        result = {"error": str(e), "available": False}
        if json_out:
            return _json.dumps(result, ensure_ascii=False)
        return f"Error: {e}"

    if snap.error:
        result = {
            "error": snap.error,
            "available": False,
        }
        if json_out:
            return _json.dumps(result, ensure_ascii=False)
        return f"Error: {snap.error}"

    # Build output
    utilization: dict[str, float] = {}
    resets_at: dict[str, float] = {}
    for key, w in snap.windows.items():
        utilization[key] = w.utilization
        resets_at[key] = w.resets_at_epoch

    result = {
        "available": True,
        "utilization": utilization,
        "resets_at": resets_at,
        "extra_usage_enabled": snap.extra_usage_enabled,
        "cookie_expires_at": snap.cookie_expires_at or 0,
        "raw": snap.raw,
    }
    if json_out:
        return _json.dumps(result, ensure_ascii=False, default=str)

    # Human-readable
    lines = ["Claude usage:"]
    for key, w in sorted(snap.windows.items()):
        label = WINDOW_LABELS.get(key, key)
        lines.append(f"  {label}: {w.utilization:.1f}% (resets {w.resets_at})")
    if snap.extra_usage_enabled:
        lines.append("  Extra usage: enabled")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m feishu_bridge.quota claude [--json]", file=sys.stderr)
        sys.exit(2)

    subcmd = sys.argv[1]
    if subcmd != "claude":
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        print("Usage: python -m feishu_bridge.quota claude [--json]", file=sys.stderr)
        sys.exit(2)

    use_json = "--json" in sys.argv
    cookie_arg: str | None = None
    for i, arg in enumerate(sys.argv[2:], 2):
        if arg.startswith("--cookie="):
            cookie_arg = arg.split("=", 1)[1]
            break

    cookie_path = Path(cookie_arg) if cookie_arg else None

    try:
        output = _claude_quota_cli(cookie_path=cookie_path, json_out=use_json)
        print(output)
    except Exception as e:
        err = {"error": str(e), "available": False}
        if use_json:
            print(_json.dumps(err, ensure_ascii=False))
        else:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
