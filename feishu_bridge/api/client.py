"""
FeishuAPI — Base class for Feishu API wrappers with auto-auth.

Provides authenticated HTTP requests via Device Flow OAuth (UAT).
Service-specific subclasses define their own scopes and convenience methods.

Usage:
    class MyService(FeishuAPI):
        SCOPES = ["my:scope:read"]
        BASE_PATH = "/open-apis/my/v1"
"""

import json
import logging
import time
import uuid
from typing import Optional

import requests

from feishu_bridge.api.auth import FeishuAuth

log = logging.getLogger("feishu-api")

# MCP endpoint for Feishu (used by Docs)
MCP_ENDPOINT = "https://mcp.feishu.cn/mcp"


class FeishuAPIError(Exception):
    """Raised when a Feishu API returns an error."""

    def __init__(self, code: int, msg: str, path: str = ""):
        self.code = code
        self.msg = msg
        self.path = path
        super().__init__(f"Feishu API error {code}: {msg} (path={path})")


class FeishuAPI:
    """Base class for Feishu API wrappers with auto-auth via Device Flow OAuth.

    Subclasses should define:
        SCOPES: list[str]  — required OAuth scopes
        BASE_PATH: str     — API path prefix (e.g., "/open-apis/task/v2")
    """

    SCOPES: list[str] = []
    BASE_PATH: str = ""

    def __init__(self, app_id: str, app_secret: str, lark_client=None,
                 token_override: str = None):
        self.auth = FeishuAuth(app_id, app_secret, lark_client)
        self.app_id = app_id
        self.app_secret = app_secret
        self._token_override = token_override

    def get_token(self, chat_id: str, user_open_id: str) -> Optional[str]:
        """Get a valid UAT, triggering auth flow if needed.

        If token_override was set at init time, returns it directly
        (used by CLI where auth file provides a pre-authed token).
        """
        if self._token_override is not None:
            return self._token_override
        return self.auth.ensure_user_token(chat_id, user_open_id, self.SCOPES)

    def get_cached_token(self, user_open_id: str) -> Optional[str]:
        """Return a valid UAT from cache/refresh only — never prompts.

        Used by background features (auto-fetch URLs) that should not
        interrupt the user with an auth card.  Does NOT check scopes —
        auto-fetch only performs reads, and the server enforces scope
        boundaries, so any valid token is sufficient here.
        """
        if self._token_override is not None:
            return self._token_override
        return self.auth.get_valid_token(user_open_id)

    # HTTP status codes that warrant automatic retry
    _RETRYABLE_HTTP = {429, 500, 502, 503, 504}
    _MAX_RETRIES = 3

    def request(self, method: str, path: str, token: str,
                params: dict = None, json_body: dict = None) -> dict:
        """Make an authenticated OAPI request with retry on transient errors.

        Retries up to _MAX_RETRIES times on 429/5xx with exponential backoff.
        """
        url = f"https://open.feishu.cn{self.BASE_PATH}{path}"
        last_exc = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                headers = {"Authorization": f"Bearer {token}"}
                if json_body is not None:
                    headers["Content-Type"] = "application/json; charset=utf-8"
                resp = requests.request(
                    method, url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=15,
                )
            except requests.exceptions.Timeout:
                last_exc = FeishuAPIError(-1, "Request timeout", path)
                if attempt < self._MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise last_exc
            except requests.exceptions.ConnectionError as e:
                last_exc = FeishuAPIError(-1, f"Connection error: {e}", path)
                if attempt < self._MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise last_exc

            # Retry on transient HTTP errors
            if resp.status_code in self._RETRYABLE_HTTP and attempt < self._MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = int(retry_after) if retry_after else 2 ** attempt
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                log.warning("HTTP %d on %s, retry %d/%d in %ds",
                            resp.status_code, path, attempt + 1,
                            self._MAX_RETRIES, wait)
                time.sleep(min(wait, 30))
                continue

            try:
                data = resp.json()
            except (ValueError, requests.JSONDecodeError):
                raise FeishuAPIError(
                    resp.status_code,
                    f"Non-JSON response: {resp.text[:200]}",
                    path,
                )

            if data.get("code") != 0:
                code = data.get("code")
                msg = data.get("msg", "Unknown error")
                log.error("API error: code=%s msg=%s url=%s", code, msg, url)
                raise FeishuAPIError(code, msg, path)

            return data.get("data", {})

        # Should not reach here, but just in case
        raise last_exc or FeishuAPIError(-1, "Max retries exceeded", path)

    def mcp_call(self, tool_name: str, args: dict, token: str) -> dict:
        """Call a Feishu MCP tool (JSON-RPC over HTTPS).

        Used by Docs for Markdown read/write via mcp.feishu.cn.

        Args:
            tool_name: MCP tool name (e.g., "fetch-doc")
            args: tool arguments
            token: user access token

        Returns:
            Parsed result from MCP response.
        """
        call_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": args,
            },
        }
        try:
            resp = requests.post(
                MCP_ENDPOINT,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-MCP-UAT": token,
                    "X-Lark-MCP-Allowed-Tools": tool_name,
                },
                json=body,
                timeout=30,
            )
        except requests.exceptions.Timeout:
            raise FeishuAPIError(0, "MCP request timeout",
                                 f"mcp/{tool_name}")
        except requests.exceptions.ConnectionError as e:
            raise FeishuAPIError(0, f"MCP connection error: {e}",
                                 f"mcp/{tool_name}")

        if not resp.ok:
            raise FeishuAPIError(
                resp.status_code,
                f"MCP HTTP {resp.status_code}: {resp.text[:500]}",
                f"mcp/{tool_name}",
            )

        try:
            data = resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            raise FeishuAPIError(
                resp.status_code,
                f"Non-JSON MCP response: {resp.text[:200]}",
                f"mcp/{tool_name}",
            )

        # Unwrap JSON-RPC envelope
        if "error" in data:
            err = data["error"]
            raise FeishuAPIError(
                err.get("code", -1),
                err.get("message", "MCP error"),
                f"mcp/{tool_name}",
            )

        result = data.get("result", data)
        # Unwrap (some gateways double-wrap) — depth-limited
        max_unwrap = 5
        while max_unwrap > 0 and isinstance(result, dict) and "result" in result and "jsonrpc" not in result:
            result = result["result"]
            max_unwrap -= 1

        # Extract text content from MCP response format
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            if isinstance(content, list) and len(content) == 1:
                text = content[0].get("text", "")
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return {"text": text}
            # Multiple content blocks
            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
            return {"texts": texts}

        return result


    # Drive API base path (independent of subclass BASE_PATH)
    DRIVE_BASE = '/open-apis/drive/v1'

    def _drive(self, method: str, path: str, token: str,
               params: dict = None, json_body: dict = None) -> dict:
        """Drive API request, independent of BASE_PATH.

        Used for cross-module operations like file deletion.
        """
        url = f'https://open.feishu.cn{self.DRIVE_BASE}{path}'
        last_exc = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                resp = requests.request(
                    method, url,
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json; charset=utf-8',
                    },
                    params=params,
                    json=json_body,
                    timeout=15,
                )
            except requests.exceptions.Timeout:
                last_exc = FeishuAPIError(-1, 'Request timeout', path)
                if attempt < self._MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise last_exc
            except requests.exceptions.ConnectionError as e:
                last_exc = FeishuAPIError(-1, f'Connection error: {e}', path)
                if attempt < self._MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise last_exc

            if resp.status_code in self._RETRYABLE_HTTP and attempt < self._MAX_RETRIES:
                retry_after = resp.headers.get('Retry-After')
                try:
                    wait = int(retry_after) if retry_after else 2 ** attempt
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                log.warning('HTTP %d on drive %s, retry %d/%d in %ds',
                            resp.status_code, path, attempt + 1,
                            self._MAX_RETRIES, wait)
                time.sleep(min(wait, 30))
                continue

            try:
                data = resp.json()
            except (ValueError, requests.JSONDecodeError):
                raise FeishuAPIError(
                    resp.status_code,
                    f'Non-JSON response: {resp.text[:200]}',
                    path,
                )

            if data.get('code') != 0:
                code = data.get('code')
                msg = data.get('msg', 'Unknown error')
                log.error('Drive API error: code=%s msg=%s url=%s', code, msg, url)
                raise FeishuAPIError(code, msg, path)

            return data.get('data', {})

        raise last_exc or FeishuAPIError(-1, 'Max retries exceeded', path)

    def _auth_failed_message(self) -> str:
        return "🔐 已发送授权卡片，请完成授权后重试。"
