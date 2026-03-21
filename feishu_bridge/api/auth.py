"""
Feishu OAuth Device Flow + Token Storage.

Implements RFC 8628 Device Authorization Grant for Feishu/Lark.
Provides auto-auth: detect permission errors → send auth card → poll → store UAT.

Usage:
    auth = FeishuAuth(app_id, app_secret, lark_client)
    token = await auth.ensure_user_token(chat_id, user_open_id, scopes=["task:task:read"])
    # Use token for user-level API calls
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("feishu-auth")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FEISHU_DEVICE_AUTH_URL = "https://accounts.feishu.cn/oauth/v1/device_authorization"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

TOKEN_DIR = Path.home() / ".local" / "share" / "feishu-bridge" / "tokens"
_OLD_TOKEN_DIR = Path.home() / ".claude" / "data" / "feishu-tokens"
_migrated_dirs: set = set()
REFRESH_AHEAD_S = 300  # refresh 5 min before expiry
POLL_INTERVAL = 5      # seconds between token polls
POLL_MAX_WAIT = 300    # 5 min max wait for user to authorize


def _migrate_legacy_token_dir():
    """One-time copy of .enc files from old path to new TOKEN_DIR (per target dir)."""
    if TOKEN_DIR in _migrated_dirs:
        return
    _migrated_dirs.add(TOKEN_DIR)
    if not _OLD_TOKEN_DIR.exists():
        return
    import shutil
    TOKEN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for enc_file in _OLD_TOKEN_DIR.glob("*.enc"):
        dest = TOKEN_DIR / enc_file.name
        if dest.exists():
            continue  # skip if exists — 保证幂等
        try:
            shutil.copy2(enc_file, dest)
            log.info("Migrated token: %s → %s", enc_file, dest)
        except OSError as e:
            log.warning("Token migration failed for %s: %s (will re-auth)", enc_file.name, e)


# ---------------------------------------------------------------------------
# Token Storage (AES-256-GCM encrypted files)
# ---------------------------------------------------------------------------

def _derive_key(app_id: str, user_open_id: str) -> bytes:
    """Derive AES-256 key from app_id + user_open_id + machine_id."""
    machine_id = _get_machine_id()
    material = f"feishu-uat:{app_id}:{user_open_id}:{machine_id}"
    return hashlib.sha256(material.encode()).digest()


def _get_machine_id() -> str:
    """Get a stable machine identifier."""
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            return Path(path).read_text().strip()
        except OSError:
            continue
    try:
        return "fallback-" + os.getlogin()
    except OSError:
        return "fallback-" + os.environ.get("USER", "unknown")


def _token_path(app_id: str, user_open_id: str) -> Path:
    return TOKEN_DIR / f"{app_id}_{user_open_id}.enc"


def _auth_card_path(app_id: str, user_open_id: str) -> Path:
    """Path for persisting auth card msg_id (cross-process tracking)."""
    return TOKEN_DIR / f"{app_id}_{user_open_id}_authcard"


def save_auth_card_id(app_id: str, user_open_id: str, msg_id: str):
    """Persist auth card msg_id so the caller can delete it after delivery.

    Uses atomic write with 0o600 permissions, matching save_token pattern.
    """
    import tempfile
    TOKEN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    TOKEN_DIR.chmod(0o700)
    path = _auth_card_path(app_id, user_open_id)
    fd, tmp_path = tempfile.mkstemp(dir=TOKEN_DIR)
    fd_open = True
    try:
        os.write(fd, msg_id.encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd_open = False
        os.replace(tmp_path, str(path))
    except BaseException:
        if fd_open:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_auth_card_id(app_id: str, user_open_id: str) -> Optional[str]:
    """Read persisted auth card msg_id without removing it. Returns None if absent."""
    try:
        msg_id = _auth_card_path(app_id, user_open_id).read_text().strip()
        return msg_id or None
    except OSError:
        return None


def remove_auth_card_id(app_id: str, user_open_id: str):
    """Remove persisted auth card msg_id file. No-op if absent or inaccessible."""
    try:
        _auth_card_path(app_id, user_open_id).unlink(missing_ok=True)
    except OSError:
        pass


def save_token(app_id: str, user_open_id: str, token_data: dict):
    """Encrypt and save token to disk (atomic write, 0600)."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Ensure existing directory has correct permissions
    TOKEN_DIR.chmod(0o700)

    key = _derive_key(app_id, user_open_id)
    plaintext = json.dumps(token_data).encode()

    # AES-256-GCM
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)

    # Atomic write: create with restrictive permissions, then replace
    import tempfile
    path = _token_path(app_id, user_open_id)
    fd, tmp_path = tempfile.mkstemp(dir=TOKEN_DIR)
    fd_open = True
    try:
        os.write(fd, nonce + ct)
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd_open = False
        os.replace(tmp_path, str(path))
    except BaseException:
        if fd_open:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    log.info("Token saved for %s:%s", app_id[:8], user_open_id[:8])


def load_token(app_id: str, user_open_id: str) -> Optional[dict]:
    """Load and decrypt token from disk. Returns None if missing/corrupt."""
    _migrate_legacy_token_dir()
    path = _token_path(app_id, user_open_id)
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
        nonce, ct = data[:12], data[12:]
        key = _derive_key(app_id, user_open_id)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        plaintext = AESGCM(key).decrypt(nonce, ct, None)
        return json.loads(plaintext)
    except Exception:
        log.warning("Failed to load token for %s:%s, will re-auth",
                    app_id[:8], user_open_id[:8])
        return None


def delete_token(app_id: str, user_open_id: str):
    """Remove stored token."""
    path = _token_path(app_id, user_open_id)
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Device Flow OAuth
# ---------------------------------------------------------------------------

def request_device_authorization(app_id: str, app_secret: str,
                                  scopes: list[str]) -> dict:
    """Step 1: Request device authorization code from Feishu.

    Returns: {device_code, user_code, verification_uri_complete, expires_in, interval}
    """
    scopes = list(dict.fromkeys(scopes))  # deduplicate, preserve order
    scope_str = " ".join(scopes)
    if "offline_access" not in scope_str.split():
        scope_str += " offline_access"

    basic_auth = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()

    try:
        resp = requests.post(
            FEISHU_DEVICE_AUTH_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic_auth}",
            },
            data={"client_id": app_id, "scope": scope_str},
            timeout=10,
        )
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        raise RuntimeError(f"Device authorization network error: {e}") from e

    if not resp.ok or "error" in data:
        msg = data.get("error_description", data.get("error", "Unknown error"))
        raise RuntimeError(f"Device authorization failed: {msg}")

    result = {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri", ""),
        "verification_uri_complete": data.get("verification_uri_complete",
                                               data.get("verification_uri", "")),
        "expires_in": data.get("expires_in", 240),
        "interval": data.get("interval", POLL_INTERVAL),
    }
    log.info("Device code obtained, expires_in=%ds", result["expires_in"])
    log.debug("user_code=%s", result["user_code"])
    return result


def poll_device_token(app_id: str, app_secret: str, device_code: str,
                      expires_in: int, interval: int,
                      stop_flag=None) -> dict:
    """Step 2: Poll token endpoint until user authorizes.

    Returns: {"ok": True, "token": {...}} or {"ok": False, "error": "...", "message": "..."}
    """
    deadline = time.time() + min(expires_in, POLL_MAX_WAIT)
    current_interval = interval

    while time.time() < deadline:
        if stop_flag and stop_flag.is_set():
            return {"ok": False, "error": "cancelled", "message": "Polling cancelled"}

        time.sleep(current_interval)

        try:
            resp = requests.post(
                FEISHU_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": app_id,
                    "client_secret": app_secret,
                },
                timeout=10,
            )
            data = resp.json()
        except Exception as e:
            log.warning("Poll network error: %s", e)
            current_interval = min(current_interval + 1, 60)
            continue

        error = data.get("error")

        if not error and data.get("access_token"):
            log.info("Token obtained successfully")
            return {
                "ok": True,
                "token": {
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token", ""),
                    "expires_in": data.get("expires_in", 7200),
                    "refresh_expires_in": data.get("refresh_token_expires_in", 604800),
                    "scope": data.get("scope", ""),
                    "obtained_at": time.time(),
                },
            }

        if error == "authorization_pending":
            continue
        if error == "slow_down":
            current_interval = min(current_interval + 5, 60)
            log.info("Slow down, interval=%ds", current_interval)
            continue
        if error == "access_denied":
            return {"ok": False, "error": "access_denied", "message": "用户拒绝了授权"}
        if error in ("expired_token", "invalid_grant"):
            return {"ok": False, "error": "expired_token", "message": "授权码已过期"}

        desc = data.get("error_description", error or "Unknown error")
        return {"ok": False, "error": "unknown", "message": desc}

    return {"ok": False, "error": "expired_token", "message": "授权超时"}


def refresh_access_token(app_id: str, app_secret: str,
                         refresh_token: str) -> Optional[dict]:
    """Refresh an expired access token."""
    try:
        resp = requests.post(
            FEISHU_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": app_id,
                "client_secret": app_secret,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("access_token"):
            return {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", refresh_token),
                "expires_in": data.get("expires_in", 7200),
                "refresh_expires_in": data.get("refresh_token_expires_in", 604800),
                "scope": data.get("scope", ""),
                "obtained_at": time.time(),
            }
        log.warning("Refresh failed: %s", data.get("error_description", data.get("error")))
    except Exception:
        log.exception("Refresh token error")
    return None


# ---------------------------------------------------------------------------
# Auth Cards (Feishu interactive cards)
# ---------------------------------------------------------------------------

def build_auth_card(verification_url: str, scopes: list[str],
                    expires_min: int = 4) -> dict:
    """Blue card with authorization link button."""
    applink_url = (
        f"https://applink.feishu.cn/client/web_url/open"
        f"?mode=sidebar-semi&max_width=800&reload=false"
        f"&url={requests.utils.quote(verification_url)}"
    )
    scope_list = "\n".join(f"- `{s}`" for s in scopes if s != "offline_access")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔐 请授权以继续操作"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"需要授权以下权限：\n{scope_list}\n\n"
                    f"授权后，应用将能够以你的身份执行相关操作。"
                ),
            },
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "前往授权"},
                    "type": "primary",
                    "multi_url": {
                        "url": applink_url,
                        "pc_url": applink_url,
                        "android_url": applink_url,
                        "ios_url": applink_url,
                    },
                }],
            },
            {
                "tag": "markdown",
                "content": f"<font color='grey'>授权链接将在 {expires_min} 分钟后失效</font>",
            },
        ],
    }


def build_auth_success_card() -> dict:
    """Green success card."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "✅ 授权成功"},
            "template": "green",
        },
        "elements": [{
            "tag": "markdown",
            "content": (
                "飞书账号已成功授权，正在继续执行操作。\n\n"
                "<font color='grey'>如需撤销授权，可告诉我「撤销飞书授权」。</font>"
            ),
        }],
    }


def build_auth_failed_card(reason: str = "授权链接已过期") -> dict:
    """Yellow warning card."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ 授权未完成"},
            "template": "yellow",
        },
        "elements": [{
            "tag": "markdown",
            "content": f"{reason}\n\n请重新发起授权。",
        }],
    }



# Human-readable scope descriptions (Chinese)
SCOPE_DESCRIPTIONS = {
    "docx:document:readonly": "文档只读",
    "docx:document:create": "创建文档",
    "docx:document": "文档读写",
    "wiki:wiki:readonly": "知识库只读",
    "wiki:wiki": "知识库读写",
    "sheets:spreadsheet:readonly": "表格只读",
    "sheets:spreadsheet": "表格读写",
    "drive:drive": "云盘完整权限",
    "drive:drive:readonly": "云盘只读",
    "drive:file:upload": "文件上传",
    "calendar:calendar": "日历读写",
    "calendar:calendar:readonly": "日历只读",
    "search:message": "消息搜索",
    "im:message:readonly": "消息只读",
    "bitable:app": "多维表格完整权限",
    "bitable:app:readonly": "多维表格只读",
    "task:task:read": "任务只读",
    "task:task:write": "任务读写",
    "task:tasklist:read": "任务清单只读",
    "mail:user_mailbox.message:readonly": "邮件读取",
    "mail:user_mailbox.message:send": "邮件发送",
    "mail:user_mailbox.message.subject:read": "邮件主题读取",
    "mail:user_mailbox.message.address:read": "邮件地址读取",
    "mail:user_mailbox.message.body:read": "邮件正文读取",
    "mail:user_mailbox.folder:read": "邮件文件夹只读",
    "mail:user_mailbox.folder:write": "邮件文件夹管理",
    "mail:user_mailbox.rule:write": "邮件规则管理",
    "mail:user_mailbox.rule:read": "邮件规则只读",
}

def build_app_scope_missing_card(app_id: str, scopes: list[str]) -> dict:
    """Orange card directing admin to enable app scopes."""
    scope_params = ",".join(scopes)
    admin_url = f"https://open.feishu.cn/app/{app_id}/auth?q={scope_params}"
    scope_list = "\n".join(
        f"- `{s}` ({SCOPE_DESCRIPTIONS.get(s, '')})" if s in SCOPE_DESCRIPTIONS
        else f"- `{s}`"
        for s in scopes
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔧 应用权限未开通"},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"应用尚未开通以下权限：\n{scope_list}\n\n"
                    f"请前往飞书开放平台开通，**开通后重新发送命令**即可。"
                ),
            },
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "前往开通"},
                    "type": "primary",
                    "multi_url": {
                        "url": admin_url,
                        "pc_url": admin_url,
                        "android_url": admin_url,
                        "ios_url": admin_url,
                    },
                }],
            },
        ],
    }


# ---------------------------------------------------------------------------
# FeishuAuth — High-level auth orchestrator
# ---------------------------------------------------------------------------

class FeishuAuth:
    """Orchestrates OAuth Device Flow with Feishu card UI.

    Usage:
        auth = FeishuAuth(app_id, app_secret, lark_client)
        token = auth.ensure_user_token(chat_id, user_open_id, ["task:task:read"])

    Locking contract:
        _get_user_lock() returns a process-wide lock keyed by (app_id, user_open_id).
        - get_valid_token()        acquires the lock for the refresh path.
        - ensure_user_token()      acquires the lock for the full OAuth flow.
        - _get_valid_token_unlocked()  assumes caller already holds the lock.
        Neither get_valid_token nor ensure_user_token calls the other, so no
        re-entrancy is needed and threading.Lock (non-reentrant) is correct.
    """

    # Class-level lock registry — shared across ALL FeishuAuth instances so
    # that FeishuDocs, FeishuSheets, etc. (each with their own FeishuAuth)
    # coordinate refresh-token rotation for the same user.
    _class_user_locks: dict[tuple, threading.Lock] = {}
    _class_locks_lock = threading.Lock()

    def __init__(self, app_id: str, app_secret: str, lark_client=None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.lark_client = lark_client  # lark_oapi.Client for sending cards

    def get_valid_token(self, user_open_id: str,
                        required_scopes: list[str] = None) -> Optional[str]:
        """Return a valid access_token from cache, refreshing if needed.

        Thread-safe: acquires a per-user lock before refreshing to prevent
        concurrent refresh-token rotation races.

        Returns None if no token or refresh fails.  Never prompts the user.
        """
        stored = load_token(self.app_id, user_open_id)
        if not stored:
            return None

        # Fast path: token still valid
        obtained_at = stored.get("obtained_at", 0)
        expires_in = stored.get("expires_in", 7200)
        if time.time() < obtained_at + expires_in - REFRESH_AHEAD_S:
            if required_scopes and not self._scopes_covered(stored, required_scopes):
                log.info("Stored token missing required scopes, need re-auth")
                return None
            return stored["access_token"]

        # Refresh path — acquire per-user lock to prevent concurrent
        # rotation of the single-use refresh token.
        with self._get_user_lock(user_open_id):
            return self._get_valid_token_unlocked(user_open_id, required_scopes)

    def _get_valid_token_unlocked(self, user_open_id: str,
                                  required_scopes: list[str] = None) -> Optional[str]:
        """Refresh-aware token retrieval — caller MUST hold the user lock.

        Re-reads from disk (another thread may have refreshed), then
        attempts refresh if still expired.  Never prompts the user.
        """
        stored = load_token(self.app_id, user_open_id)
        if not stored:
            return None
        obtained_at = stored.get("obtained_at", 0)
        expires_in = stored.get("expires_in", 7200)
        # Double-check: if another thread refreshed, the token is fresh now
        if time.time() < obtained_at + expires_in - REFRESH_AHEAD_S:
            if required_scopes and not self._scopes_covered(stored, required_scopes):
                return None
            return stored["access_token"]

        refresh_token = stored.get("refresh_token", "")
        if not refresh_token:
            return None

        refresh_expires = stored.get("refresh_expires_in", 604800)
        if time.time() > obtained_at + refresh_expires:
            log.info("Refresh token also expired, need full re-auth")
            delete_token(self.app_id, user_open_id)
            return None

        new_token = refresh_access_token(
            self.app_id, self.app_secret, refresh_token)
        if new_token:
            # Verify scopes are still covered after refresh
            if required_scopes and not self._scopes_covered(
                    new_token, required_scopes):
                log.info("Refreshed token missing required scopes")
                save_token(self.app_id, user_open_id, new_token)
                return None
            save_token(self.app_id, user_open_id, new_token)
            return new_token["access_token"]
        return None

    def _get_user_lock(self, user_open_id: str) -> threading.Lock:
        """Get or create a process-wide per-(app_id, user) lock."""
        key = (self.app_id, user_open_id)
        with FeishuAuth._class_locks_lock:
            if key not in FeishuAuth._class_user_locks:
                FeishuAuth._class_user_locks[key] = threading.Lock()
            return FeishuAuth._class_user_locks[key]

    def ensure_user_token(self, chat_id: str, user_open_id: str,
                          scopes: list[str],
                          stop_flag=None) -> Optional[str]:
        """Get a valid user token, triggering OAuth flow if needed.

        Sends auth card to chat_id, polls for completion, returns access_token.
        Blocking call — run in a thread if needed.

        Per-user lock prevents duplicate OAuth cards when concurrent
        requests arrive for the same user.
        """
        with self._get_user_lock(user_open_id):
            return self._ensure_user_token_inner(
                chat_id, user_open_id, scopes, stop_flag)

    def _ensure_user_token_inner(self, chat_id: str, user_open_id: str,
                                 scopes: list[str],
                                 stop_flag=None) -> Optional[str]:
        """Inner implementation of ensure_user_token (called under lock)."""
        # 1. Try cached token (unlocked — caller already holds the lock)
        token = self._get_valid_token_unlocked(user_open_id, scopes)
        if token:
            log.info("Using cached token for %s", user_open_id[:8])
            return token

        # 2. Start Device Flow — merge existing scopes with required scopes
        #    so the new token doesn't lose previously granted authorizations.
        stored = load_token(self.app_id, user_open_id)
        if stored:
            existing_scopes = stored.get("scope", "").split()
            merged = list(dict.fromkeys(existing_scopes + scopes))
        else:
            merged = scopes

        try:
            device = request_device_authorization(
                self.app_id, self.app_secret, merged)
        except RuntimeError as e:
            error_msg = str(e)
            if "99991672" in error_msg:
                self._send_card(chat_id, build_app_scope_missing_card(
                    self.app_id, scopes))
                return None
            # Invalid scope in merged set — stale scope from stored token?
            # Fall back to requesting only the required scopes.
            if ("invalid" in error_msg.lower() and "scope" in error_msg.lower()
                    and merged != scopes):
                log.warning("Merged scopes failed (%s), retrying with "
                            "required scopes only", error_msg)
                try:
                    device = request_device_authorization(
                        self.app_id, self.app_secret, scopes)
                except RuntimeError as e2:
                    err2 = str(e2)
                    if "99991672" in err2:
                        self._send_card(chat_id, build_app_scope_missing_card(
                            self.app_id, scopes))
                        return None
                    raise
            else:
                raise

        # 3. Send auth card and persist msg_id for post-delivery cleanup
        expires_min = max(1, device["expires_in"] // 60)
        msg_id = self._send_card(chat_id, build_auth_card(
            device["verification_uri_complete"], scopes, expires_min))

        if msg_id is None:
            log.error("Failed to send auth card for %s, aborting auth flow",
                      user_open_id[:8])
            return None

        save_auth_card_id(self.app_id, user_open_id, msg_id)

        # 4. Poll for token
        result = poll_device_token(
            self.app_id, self.app_secret,
            device["device_code"], device["expires_in"],
            device["interval"], stop_flag)

        # 5. Update card and store token
        #    Card will be deleted by the caller after result delivery;
        #    update it now so the user sees immediate feedback.
        if result["ok"]:
            token_data = result["token"]
            save_token(self.app_id, user_open_id, token_data)
            success_card = build_auth_success_card()
            if not self._update_card(msg_id, success_card):
                self._send_card(chat_id, success_card)
            return token_data["access_token"]
        else:
            failed_card = build_auth_failed_card(result["message"])
            if not self._update_card(msg_id, failed_card):
                self._send_card(chat_id, failed_card)
            return None

    def _scopes_covered(self, stored: dict, required: list[str]) -> bool:
        granted = set(stored.get("scope", "").split())
        return all(s in granted for s in required)

    def _send_card(self, chat_id: str, card: dict) -> Optional[str]:
        """Send interactive card to chat, return message_id."""
        if not self.lark_client:
            log.warning("No lark_client, cannot send card")
            return None
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest, CreateMessageRequestBody,
            )
            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(json.dumps(card))
                    .build()
                ).build()
            resp = self.lark_client.im.v1.message.create(req)
            if resp.success():
                return resp.data.message_id
            log.error("Send card failed: %s %s", resp.code, resp.msg)
        except Exception:
            log.exception("Send card error")
        return None

    def _update_card(self, msg_id: Optional[str], card: dict) -> bool:
        """Update existing card content. Returns True on success."""
        if not msg_id or not self.lark_client:
            return False
        try:
            from lark_oapi.api.im.v1 import (
                PatchMessageRequest, PatchMessageRequestBody,
            )
            req = PatchMessageRequest.builder() \
                .message_id(msg_id) \
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(json.dumps(card))
                    .build()
                ).build()
            resp = self.lark_client.im.v1.message.patch(req)
            if resp.success():
                return True
            log.error("Update card failed: code=%s msg=%s msg_id=%s",
                      resp.code, resp.msg, msg_id)
        except Exception:
            log.exception("Update card error for msg_id=%s", msg_id)
        return False

    def _delete_message(self, msg_id: str) -> bool:
        """Delete a bot message by ID. Returns True on success."""
        if not msg_id or not self.lark_client:
            return False
        try:
            from lark_oapi.api.im.v1 import DeleteMessageRequest
            req = DeleteMessageRequest.builder() \
                .message_id(msg_id).build()
            resp = self.lark_client.im.v1.message.delete(req)
            if resp.success():
                return True
            log.error("Delete message failed: code=%s msg=%s msg_id=%s",
                      resp.code, resp.msg, msg_id)
        except Exception:
            log.exception("Delete message error for msg_id=%s", msg_id)
        return False

    def cleanup_auth_card(self, user_open_id: str) -> bool:
        """Delete the auth card from chat after result delivery.

        Reads the persisted auth card msg_id (written by this or a CLI
        subprocess), deletes the Feishu message, and removes the IPC file
        only on success — so a transient API failure can be retried.
        Cross-process safe — uses filesystem for IPC.
        """
        msg_id = read_auth_card_id(self.app_id, user_open_id)
        if not msg_id:
            return False
        if self._delete_message(msg_id):
            remove_auth_card_id(self.app_id, user_open_id)
            return True
        return False
