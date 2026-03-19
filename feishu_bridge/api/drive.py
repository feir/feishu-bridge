"""
Feishu Drive file upload API wrapper.

Supports uploading local files and downloading+uploading from URLs.
All operations require user_access_token (UAT).

Usage:
    drive = FeishuDrive(app_id, app_secret)
    result = drive.upload_file(chat_id, user_open_id, file_path, folder_token)
    result = drive.upload_from_url(chat_id, user_open_id, url, folder_token)
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

import ipaddress
import socket
import requests as http_requests

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-drive")

MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20 MB


def _validate_url(url: str) -> None:
    """Validate URL to prevent SSRF attacks."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Missing hostname in URL")

    # Resolve and check IP
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed: {e}")

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(f"URL resolves to blocked address: {addr}")


class FeishuDrive(FeishuAPI):
    """Feishu Drive file upload via OAPI."""

    SCOPES = [
        "drive:drive",
    ]
    BASE_PATH = "/open-apis/drive/v1"

    def _get_root_folder_token(self, token: str) -> Optional[str]:
        """Get the user's root folder token."""
        url = "https://open.feishu.cn/open-apis/drive/explorer/v2/root_folder/meta"
        try:
            resp = http_requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {}).get("token")
        except Exception as e:
            log.warning("Failed to get root folder: %s", e)
        return None

    def upload_file(self, chat_id: str, user_open_id: str,
                    file_path: str, folder_token: str = None,
                    file_name: str = None) -> Optional[dict]:
        """Upload a local file to Feishu Drive.

        Args:
            file_path: absolute path to the local file
            folder_token: target folder token (default: user's root folder)
            file_name: override file name (default: basename of file_path)

        Returns:
            {"file_token": "...", "file_name": "..."} or None on auth failure
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}

        path = Path(file_path)
        if not path.is_file():
            return {"error": f"File not found: {file_path}"}

        file_size = path.stat().st_size
        if file_size == 0:
            return {"error": "Cannot upload empty file"}
        if file_size > MAX_UPLOAD_SIZE:
            return {"error": f"File too large: {file_size} bytes (max {MAX_UPLOAD_SIZE})"}

        if not folder_token:
            folder_token = self._get_root_folder_token(token)
            if not folder_token:
                return {"error": "No folder specified and failed to get root folder"}

        name = file_name or path.name

        url = f"https://open.feishu.cn{self.BASE_PATH}/files/upload_all"
        with open(path, "rb") as f:
            resp = http_requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data={
                    "file_name": name,
                    "parent_type": "explorer",
                    "parent_node": folder_token,
                    "size": str(file_size),
                },
                files={"file": (name, f)},
                timeout=120,
            )

        data = resp.json()
        if data.get("code") != 0:
            return {
                "error": f"Upload failed: {data.get('msg', 'unknown')}",
                "code": data.get("code"),
            }

        file_token = data.get("data", {}).get("file_token", "")
        return {
            "file_token": file_token,
            "file_name": name,
            "size": file_size,
            "url": f"https://www.feishu.cn/drive/file/{file_token}" if file_token else None,
        }

    def upload_from_url(self, chat_id: str, user_open_id: str,
                        url: str, folder_token: str = None,
                        file_name: str = None) -> Optional[dict]:
        """Download a file from URL and upload to Feishu Drive.

        Args:
            url: source URL to download from
            folder_token: target folder token (default: user's root folder)
            file_name: override file name (default: derived from URL)

        Returns:
            {"file_token": "...", "file_name": "...", "size": N} or error dict
        """
        if not file_name:
            parsed = urlparse(url)
            file_name = unquote(Path(parsed.path).name) or "downloaded_file"

        tmp_path = None
        try:
            _validate_url(url)
            resp = http_requests.get(url, stream=True, timeout=60,
                                     allow_redirects=False,
                                     headers={"User-Agent": "FeishuDrive/1.0"})
            # Handle redirects manually to re-validate target
            if resp.is_redirect and resp.headers.get("location"):
                redirect_url = resp.headers["location"]
                _validate_url(redirect_url)
                resp = http_requests.get(redirect_url, stream=True, timeout=60,
                                         allow_redirects=False,
                                         headers={"User-Agent": "FeishuDrive/1.0"})
            resp.raise_for_status()

            content_length = int(resp.headers.get("content-length", 0))
            if content_length > MAX_UPLOAD_SIZE:
                return {"error": f"File too large: {content_length} bytes (max {MAX_UPLOAD_SIZE})"}

            fd, tmp_path = tempfile.mkstemp(prefix="feishu_upload_")
            downloaded = 0
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    downloaded += len(chunk)
                    if downloaded > MAX_UPLOAD_SIZE:
                        return {"error": f"File too large: >{MAX_UPLOAD_SIZE} bytes"}
                    f.write(chunk)

            result = self.upload_file(chat_id, user_open_id,
                                      tmp_path, folder_token,
                                      file_name=file_name)
            return result

        except ValueError as e:
            return {"error": f"URL rejected: {e}"}
        except http_requests.exceptions.RequestException as e:
            return {"error": f"Download failed: {e}"}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
