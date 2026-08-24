"""使用 OpenAI 官方 Sentinel SDK 生成 Checkout 请求头。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_SENTINEL_VERSION = "20260810913b"
DEFAULT_SENTINEL_REQUEST_URL = "https://chatgpt.com/backend-api/sentinel/req"
_NODE_LIMIT = threading.Semaphore(16)
_SDK_CACHE: dict[str, Path] = {}
_SDK_CACHE_LOCK = threading.Lock()


class SentinelError(RuntimeError):
    """Sentinel 生成失败，且错误文本不包含账号凭据。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def default_sdk_url() -> str:
    version = os.getenv("CHATGPT_SENTINEL_VERSION", DEFAULT_SENTINEL_VERSION).strip()
    return f"https://chatgpt.com/sentinel/{version}/sdk.js"


class SentinelClient:
    """下载官方 SDK，并在隔离的 Node VM 中执行 requirements/solve。"""

    def __init__(self, *, timeout: int = 45) -> None:
        self.timeout = timeout
        self._runtime = Path(__file__).with_name("sentinel_runtime.js")

    def headers(
        self,
        session: Any,
        *,
        device_id: str,
        flow: str,
        page_url: str,
        user_agent: str,
        sdk_url: str = "",
    ) -> dict[str, str]:
        if not self._runtime.is_file():
            raise SentinelError("Sentinel Node 运行文件缺失", retryable=False)
        node = os.getenv("OPENAI_SENTINEL_NODE_PATH", "").strip() or shutil.which("node")
        if not node:
            raise SentinelError("未检测到 Node.js，无法运行 Sentinel SDK", retryable=False)

        resolved_sdk_url = sdk_url.strip() or default_sdk_url()
        sdk_file = self._download_sdk(session, resolved_sdk_url, page_url, user_agent)
        common = {
            "device_id": device_id,
            "flow": flow,
            "user_agent": user_agent,
            "language": "en-PH",
            "languages": ["en-PH", "en"],
            "timezone": "Asia/Manila",
            "page_url": page_url,
            "page_origin": "https://chatgpt.com",
            "current_script_url": resolved_sdk_url,
            "screen_width": 1920,
            "screen_height": 1080,
            "hardware_concurrency": 12,
        }
        requirements = self._run_node(node, sdk_file, {**common, "action": "requirements"})
        request_proof = str(requirements.get("request_p") or "").strip()
        if not request_proof:
            raise SentinelError("Sentinel requirements 未返回请求证明")

        challenge = self._request_challenge(
            session,
            device_id=device_id,
            flow=flow,
            request_proof=request_proof,
            page_url=page_url,
            user_agent=user_agent,
        )
        solved = self._run_node(
            node,
            sdk_file,
            {
                **common,
                "action": "solve",
                "request_p": request_proof,
                "challenge": challenge,
                "behavior_duration_ms": 4200,
            },
        )
        token = str(solved.get("token") or "").strip()
        so_token = str(solved.get("so_token") or "").strip()
        if not token:
            detail = str(solved.get("sdk_token_error") or "SDK token 为空").strip()
            raise SentinelError(f"Sentinel SDK 求解失败：{detail[:220]}")
        if bool((challenge.get("so") or {}).get("required")) and not so_token:
            raise SentinelError("Sentinel 服务端要求 SO token，但 SDK 未返回")
        headers = {"OpenAI-Sentinel-Token": token}
        if so_token:
            headers["OpenAI-Sentinel-SO-Token"] = so_token
        return headers

    def _download_sdk(
        self,
        session: Any,
        sdk_url: str,
        page_url: str,
        user_agent: str,
    ) -> Path:
        cached = _SDK_CACHE.get(sdk_url)
        if cached and cached.is_file() and cached.stat().st_size:
            return cached
        with _SDK_CACHE_LOCK:
            cached = _SDK_CACHE.get(sdk_url)
            if cached and cached.is_file() and cached.stat().st_size:
                return cached
            cache_key = hashlib.sha256(sdk_url.encode("utf-8")).hexdigest()[:20]
            cache_dir = Path(tempfile.gettempdir()) / "gcash-link-extractor" / "sentinel" / cache_key
            cache_dir.mkdir(parents=True, exist_ok=True)
            sdk_file = cache_dir / "sdk.js"
            if not sdk_file.is_file() or not sdk_file.stat().st_size:
                try:
                    response = session.get(
                        sdk_url,
                        headers={
                            "User-Agent": user_agent,
                            "Accept": "*/*",
                            "Accept-Language": "en-PH,en;q=0.9",
                            "Referer": page_url,
                            "Sec-Fetch-Dest": "script",
                            "Sec-Fetch-Mode": "no-cors",
                            "Sec-Fetch-Site": "same-origin",
                        },
                        timeout=self.timeout,
                    )
                except Exception as exc:
                    raise SentinelError(f"下载 Sentinel SDK 失败：{type(exc).__name__}") from exc
                status = int(getattr(response, "status_code", 0) or 0)
                if status != 200:
                    raise SentinelError(
                        f"下载 Sentinel SDK 失败：HTTP {status}",
                        retryable=status == 429 or status >= 500,
                    )
                content = bytes(getattr(response, "content", b"") or b"")
                if not content:
                    content = str(getattr(response, "text", "") or "").encode("utf-8")
                if not content:
                    raise SentinelError("下载 Sentinel SDK 失败：响应为空")
                temporary = sdk_file.with_suffix(".tmp")
                temporary.write_bytes(content)
                os.replace(temporary, sdk_file)
            _SDK_CACHE[sdk_url] = sdk_file
            return sdk_file

    def _request_challenge(
        self,
        session: Any,
        *,
        device_id: str,
        flow: str,
        request_proof: str,
        page_url: str,
        user_agent: str,
    ) -> dict[str, Any]:
        body = json.dumps(
            {"p": request_proof, "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        try:
            response = session.post(
                DEFAULT_SENTINEL_REQUEST_URL,
                data=body,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "*/*",
                    "Accept-Language": "en-PH,en;q=0.9",
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Origin": "https://chatgpt.com",
                    "Referer": page_url,
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                },
                timeout=self.timeout,
            )
        except Exception as exc:
            raise SentinelError(f"请求 Sentinel challenge 失败：{type(exc).__name__}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise SentinelError(
                f"请求 Sentinel challenge 失败：HTTP {status}",
                retryable=status == 429 or status >= 500,
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise SentinelError("Sentinel challenge 返回非 JSON") from exc
        if not isinstance(payload, dict) or not str(payload.get("token") or "").strip():
            raise SentinelError("Sentinel challenge 响应缺少 token")
        return payload

    def _run_node(self, node: str, sdk_file: Path, payload: dict[str, Any]) -> dict[str, Any]:
        environment = dict(os.environ)
        environment["OPENAI_SENTINEL_SDK_FILE"] = str(sdk_file)
        try:
            with _NODE_LIMIT:
                process = subprocess.run(
                    [node, str(self._runtime)],
                    input=json.dumps(payload, ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout + 15,
                    env=environment,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise SentinelError("Sentinel SDK 运行超时") from exc
        except OSError as exc:
            raise SentinelError("无法启动 Node.js", retryable=False) from exc
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "未知错误").strip()
            raise SentinelError(f"Sentinel SDK 运行失败：{detail[:240]}")
        try:
            result = json.loads((process.stdout or "").strip())
        except json.JSONDecodeError as exc:
            raise SentinelError("Sentinel SDK 输出不是有效 JSON") from exc
        if not isinstance(result, dict):
            raise SentinelError("Sentinel SDK 输出格式无效")
        return result


def is_same_origin(url: str, page_url: str) -> bool:
    """供测试核对 Sentinel URL 与页面来源是否一致。"""

    left = urlsplit(url)
    right = urlsplit(page_url)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)
