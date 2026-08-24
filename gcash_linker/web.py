"""本地 GCash 任务控制台。当前只做输入校验和邮箱识别。"""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .identity import TokenIdentityError, extract_email_from_access_token
from .inputs import BatchRequest, InputValidationError, parse_batch_request


_WEB_ROOT = Path(__file__).with_name("web")
_MAX_BODY_BYTES = 2 * 1024 * 1024


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def build_task_rows(request: BatchRequest, *, batch_id: str | None = None) -> list[dict[str, Any]]:
    """为每个 Token 生成不含凭据的内存任务摘要。"""

    batch_id = batch_id or uuid.uuid4().hex[:12]
    created_at = _now_text()
    rows: list[dict[str, Any]] = []
    for index, token in enumerate(request.access_tokens, start=1):
        task_id = f"{batch_id}-{index:03d}"
        try:
            email = extract_email_from_access_token(token)
        except TokenIdentityError as exc:
            rows.append(
                {
                    "task_id": task_id,
                    "batch_id": batch_id,
                    "position": index,
                    "email": "",
                    "status": "invalid",
                    "status_label": "Token 无法解析",
                    "stage": str(exc),
                    "progress": 0,
                    "created_at": created_at,
                }
            )
            continue
        if email:
            rows.append(
                {
                    "task_id": task_id,
                    "batch_id": batch_id,
                    "position": index,
                    "email": email,
                    "status": "ready",
                    "status_label": "已识别",
                    "stage": "邮箱名已解析，等待协议执行器",
                    "progress": 15,
                    "created_at": created_at,
                }
            )
        else:
            rows.append(
                {
                    "task_id": task_id,
                    "batch_id": batch_id,
                    "position": index,
                    "email": "",
                    "status": "missing_email",
                    "status_label": "未找到邮箱",
                    "stage": "Token 中没有可用邮箱字段",
                    "progress": 0,
                    "created_at": created_at,
                }
            )
    return rows


class TaskStore:
    """只保留任务摘要；Token 和代理不会进入此对象。"""

    def __init__(self, max_tasks: int = 1000) -> None:
        self._max_tasks = max_tasks
        self._rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            self._rows = (rows + self._rows)[: self._max_tasks]
            return list(self._rows)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._rows)

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise InputValidationError("请求体长度无效") from exc
    if length <= 0 or length > _MAX_BODY_BYTES:
        raise InputValidationError("请求体不能为空且不能超过 2 MB")
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputValidationError("请求体不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise InputValidationError("请求体必须是 JSON 对象")
    return payload


def _text_field(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InputValidationError(f"{name} 必须是文本")
    return value


class ConsoleHandler(BaseHTTPRequestHandler):
    store = TaskStore()

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file() or _WEB_ROOT not in path.parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        elif path == "/api/health":
            self._send_json({"status": "ok", "mode": "local-email-preview"})
        elif path == "/api/tasks":
            self._send_json({"tasks": self.store.list()})
        elif path == "/":
            self._send_file(_WEB_ROOT / "index.html")
        elif path in {"/app.js", "/styles.css"}:
            self._send_file(_WEB_ROOT / path.lstrip("/"))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/tasks":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = _read_json(self)
            request = parse_batch_request(
                _text_field(payload, "access_tokens"),
                _text_field(payload, "checkout_proxies"),
                _text_field(payload, "promotion_proxies"),
            )
            batch_id = uuid.uuid4().hex[:12]
            rows = build_task_rows(request, batch_id=batch_id)
        except InputValidationError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "batch_id": batch_id,
                "accepted": len(rows),
                "tasks": self.store.add(rows),
            },
            HTTPStatus.CREATED,
        )

    def do_DELETE(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/tasks":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.store.clear()
        self._send_json({"tasks": []})

    def log_message(self, format: str, *args: object) -> None:
        # 不记录请求体，避免凭据进入普通日志。
        print(f"[web] {self.command} {urlparse(self.path).path}")


class ConsoleServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GCash 本地 Web 控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)
    server = ConsoleServer((args.host, args.port), ConsoleHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"GCash Web 控制台已启动：{url}")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 Web 控制台。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
