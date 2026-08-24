"""本地 GCash 任务控制台与批量调度。"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .gcash_protocol import run_gcash_protocol
from .identity import TokenIdentityError, extract_email_from_access_token
from .inputs import BatchRequest, InputValidationError, ProxyPool, parse_batch_request


_WEB_ROOT = Path(__file__).with_name("web")
_MAX_BODY_BYTES = 2 * 1024 * 1024
_TERMINAL_STATUSES = frozenset(
    {"success", "failed", "invalid", "missing_email", "expired", "nonzero"}
)
_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PROGRESS_FIELDS = frozenset(
    {"checkout_url", "payment_url", "payment_status", "amount", "currency", "qr_error"}
)

TaskProgress = Callable[..., None]
TaskRunner = Callable[[str, str, str, TaskProgress], dict[str, Any]]


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _default_task_store_path() -> Path:
    configured = os.getenv("GCASH_LINK_EXTRACTOR_DATA_DIR", "").strip()
    if configured:
        data_dir = Path(configured).expanduser()
    elif os.name == "nt":
        data_dir = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        data_dir /= "GCashLinkExtractor"
    else:
        data_dir = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        data_dir /= "gcash-link-extractor"
    return data_dir / "tasks.json"


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
                    "finished_at": created_at,
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
                    "status": "queued",
                    "status_label": "排队中",
                    "stage": "邮箱名已解析，等待执行",
                    "progress": 5,
                    "created_at": created_at,
                    "finished_at": "",
                    "attempt": 0,
                    "max_attempts": request.retry_count + 1,
                    "has_qr": False,
                    "payment_status": "",
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
                    "finished_at": created_at,
                }
            )
    return rows


class TaskStore:
    """持久化任务摘要；Token 和代理不会进入此对象。"""

    def __init__(self, max_tasks: int = 1000, storage_path: str | Path | None = None) -> None:
        self._max_tasks = max_tasks
        self._storage_path = Path(storage_path) if storage_path else None
        self._qr_dir = self._storage_path.parent / "qr" if self._storage_path else None
        self._qr_cache: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._rows = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if self._storage_path is None or not self._storage_path.is_file():
            return []
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
            tasks = payload.get("tasks") if isinstance(payload, dict) else None
            if not isinstance(tasks, list):
                raise ValueError("tasks 字段不是数组")
            return [dict(row) for row in tasks if isinstance(row, dict)][: self._max_tasks]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[web] 任务记录读取失败，保留原文件：{exc}")
            return []

    def _persist_locked(self) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._storage_path.with_suffix(self._storage_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "tasks": self._rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._storage_path)

    def add(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            self._rows = (rows + self._rows)[: self._max_tasks]
            self._persist_locked()
            return [dict(row) for row in self._rows]

    def update(self, task_id: str, **changes: Any) -> dict[str, Any] | None:
        qr_png = changes.pop("qr_png", None)
        with self._lock:
            for row in self._rows:
                if row.get("task_id") == task_id:
                    if qr_png is not None:
                        self._save_qr_locked(task_id, qr_png)
                        changes["has_qr"] = True
                    row.update(changes)
                    self._persist_locked()
                    return dict(row)
        return None

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._rows:
                if row.get("task_id") == task_id:
                    return dict(row)
        return None

    def _delete_qr_locked(self, task_id: str) -> None:
        self._qr_cache.pop(task_id, None)
        if self._qr_dir is None or not _TASK_ID_RE.fullmatch(task_id):
            return
        try:
            (self._qr_dir / f"{task_id}.png").unlink(missing_ok=True)
        except OSError:
            pass

    def delete(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            for index, row in enumerate(self._rows):
                if row.get("task_id") != task_id:
                    continue
                deleted = self._rows.pop(index)
                self._delete_qr_locked(task_id)
                self._persist_locked()
                return dict(deleted)
        return None

    def clear_terminal(self) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            deleted = [
                row for row in self._rows if str(row.get("status") or "") in _TERMINAL_STATUSES
            ]
            if deleted:
                deleted_ids = {id(row) for row in deleted}
                self._rows = [row for row in self._rows if id(row) not in deleted_ids]
                for row in deleted:
                    self._delete_qr_locked(str(row.get("task_id") or ""))
                self._persist_locked()
            return len(deleted), [dict(row) for row in self._rows]

    def _save_qr_locked(self, task_id: str, payload: Any) -> None:
        if not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError("任务 ID 无效")
        image = bytes(payload)
        if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 512 * 1024:
            raise ValueError("二维码必须是 512 KB 以内的 PNG")
        if self._qr_dir is None:
            self._qr_cache[task_id] = image
            return
        self._qr_dir.mkdir(parents=True, exist_ok=True)
        target = self._qr_dir / f"{task_id}.png"
        temporary = target.with_suffix(".png.tmp")
        temporary.write_bytes(image)
        os.replace(temporary, target)

    def read_qr(self, task_id: str) -> bytes | None:
        if not _TASK_ID_RE.fullmatch(task_id):
            return None
        with self._lock:
            if self._qr_dir is None:
                value = self._qr_cache.get(task_id)
                return bytes(value) if value else None
            target = self._qr_dir / f"{task_id}.png"
            try:
                payload = target.read_bytes()
            except OSError:
                return None
            if not payload.startswith(b"\x89PNG\r\n\x1a\n") or len(payload) > 512 * 1024:
                return None
            return payload

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._rows]

    def clear(self) -> None:
        with self._lock:
            task_ids = [str(row.get("task_id") or "") for row in self._rows]
            self._rows.clear()
            for task_id in task_ids:
                self._delete_qr_locked(task_id)
            self._persist_locked()


class TaskCancelled(RuntimeError):
    """任务被用户强制结束；不得重试或写回结果。"""


class BatchExecutor:
    """按批次并发执行账号，并在失败时完整重试。"""

    def __init__(self, store: TaskStore, runner: TaskRunner | None = None) -> None:
        self.store = store
        self.runner = runner or run_gcash_protocol
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()

    def submit(self, request: BatchRequest, rows: list[dict[str, Any]]) -> None:
        runnable = [row for row in rows if row.get("status") == "queued"]
        if not runnable:
            return
        billing_pool = ProxyPool(request.billing_exit_proxies, role="账单出口")
        promotion_pool = ProxyPool(request.promotion_exit_proxies, role="促销出口")
        worker_count = min(request.concurrency, len(runnable))
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="gcash")
        for row in runnable:
            cancel_event = threading.Event()
            with self._cancel_lock:
                self._cancel_events[row["task_id"]] = cancel_event
            token = request.access_tokens[int(row["position"]) - 1]
            executor.submit(
                self._run_task,
                row["task_id"],
                token,
                billing_pool,
                promotion_pool,
                request.retry_count,
                cancel_event,
            )
        executor.shutdown(wait=False)

    def delete_task(self, task_id: str, *, force: bool = False) -> dict[str, Any]:
        row = self.store.get(task_id)
        if row is None:
            return {"deleted": False, "not_found": True}
        active = str(row.get("status") or "") not in _TERMINAL_STATUSES
        if active and not force:
            return {"deleted": False, "requires_confirmation": True, "task": row}
        if active:
            with self._cancel_lock:
                cancel_event = self._cancel_events.get(task_id)
            if cancel_event is not None:
                cancel_event.set()
        deleted = self.store.delete(task_id)
        return {"deleted": deleted is not None, "forced": active, "task": deleted}

    def _run_task(
        self,
        task_id: str,
        token: str,
        billing_pool: ProxyPool,
        promotion_pool: ProxyPool,
        retry_count: int,
        cancel_event: threading.Event,
    ) -> None:
        def ensure_not_cancelled() -> None:
            if cancel_event.is_set():
                raise TaskCancelled("任务已被用户强制结束")

        try:
            max_attempts = retry_count + 1
            for attempt in range(1, max_attempts + 1):
                ensure_not_cancelled()
                self.store.update(
                    task_id,
                    status="running",
                    status_label="执行中",
                    stage=f"正在执行第 {attempt}/{max_attempts} 次尝试",
                    progress=10,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    finished_at="",
                )

                def report(
                    stage: str,
                    progress: int,
                    details: dict[str, Any] | None = None,
                ) -> None:
                    ensure_not_cancelled()
                    changes: dict[str, Any] = {
                        "stage": str(stage)[:400],
                        "progress": max(0, min(100, int(progress))),
                    }
                    for key, value in (details or {}).items():
                        if key == "qr_png":
                            changes[key] = value
                        elif key in _PROGRESS_FIELDS:
                            changes[key] = str(value)[:4096]
                    ensure_not_cancelled()
                    self.store.update(task_id, **changes)

                report.check_cancelled = ensure_not_cancelled  # type: ignore[attr-defined]

                try:
                    result = dict(
                        self.runner(
                            token,
                            billing_pool.acquire(),
                            promotion_pool.acquire(),
                            report,
                        )
                        or {}
                    )
                    ensure_not_cancelled()
                    status = str(result.get("status") or "success")
                    result["status"] = status
                    result.setdefault("status_label", "成功" if status == "success" else "已完成")
                    result.setdefault("progress", 100 if status in _TERMINAL_STATUSES else 15)
                    result["attempt"] = attempt
                    result["max_attempts"] = max_attempts
                    result["finished_at"] = _now_text() if status in _TERMINAL_STATUSES else ""
                    self.store.update(task_id, **result)
                    return
                except Exception as exc:
                    if cancel_event.is_set():
                        return
                    retryable = bool(getattr(exc, "retryable", True))
                    if retryable and attempt <= retry_count:
                        self.store.update(
                            task_id,
                            status="retrying",
                            status_label="等待重试",
                            stage=f"第 {attempt} 次失败，准备重试：{exc}",
                            progress=10,
                        )
                        continue
                    status = str(getattr(exc, "status", "failed") or "failed")
                    status_label = str(getattr(exc, "status_label", "失败") or "失败")
                    prefix = f"已尝试 {attempt} 次" if retryable else "任务已停止"
                    self.store.update(
                        task_id,
                        status=status,
                        status_label=status_label,
                        stage=f"{prefix}：{exc}",
                        progress=100,
                        finished_at=_now_text(),
                    )
                    return
        except TaskCancelled:
            return
        finally:
            with self._cancel_lock:
                if self._cancel_events.get(task_id) is cancel_event:
                    self._cancel_events.pop(task_id, None)


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


def _text_field(payload: dict[str, Any], name: str, *fallback_names: str) -> str:
    value = next((payload[key] for key in (name, *fallback_names) if key in payload), "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InputValidationError(f"{name} 必须是文本")
    return value


class ConsoleHandler(BaseHTTPRequestHandler):
    store = TaskStore(storage_path=_default_task_store_path())
    executor = BatchExecutor(store)

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

    def _send_png(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        elif path == "/api/health":
            self._send_json({"status": "ok", "mode": "gcash-protocol"})
        elif path == "/api/tasks":
            self._send_json({"tasks": self.store.list()})
        elif path.startswith("/api/tasks/") and path.endswith("/qr"):
            task_id = unquote(path[len("/api/tasks/") : -len("/qr")]).strip("/")
            qr_png = self.store.read_qr(task_id)
            if qr_png is None:
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self._send_png(qr_png)
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
                _text_field(payload, "billing_exit_proxies", "checkout_proxies"),
                _text_field(payload, "promotion_exit_proxies", "promotion_proxies"),
                payload.get("concurrency", 10),
                payload.get("retry_count", 3),
            )
            batch_id = uuid.uuid4().hex[:12]
            rows = build_task_rows(request, batch_id=batch_id)
        except InputValidationError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        tasks = self.store.add(rows)
        self.executor.submit(request, rows)
        self._send_json(
            {
                "batch_id": batch_id,
                "accepted": len(rows),
                "concurrency": request.concurrency,
                "retry_count": request.retry_count,
                "tasks": tasks,
            },
            HTTPStatus.CREATED,
        )

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in {"/api/tasks", "/api/tasks/completed"}:
            deleted, tasks = self.store.clear_terminal()
            self._send_json({"deleted": deleted, "tasks": tasks})
            return
        prefix = "/api/tasks/"
        if not path.startswith(prefix):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        task_id = unquote(path[len(prefix) :])
        if not _TASK_ID_RE.fullmatch(task_id):
            self._send_json({"error": "任务 ID 无效"}, HTTPStatus.BAD_REQUEST)
            return
        force = parse_qs(parsed.query).get("force", [""])[0].lower() in {"1", "true", "yes"}
        result = self.executor.delete_task(task_id, force=force)
        if result.get("not_found"):
            self._send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
        elif result.get("requires_confirmation"):
            self._send_json(
                {"error": "任务仍在运行，强制结束前需要确认", **result},
                HTTPStatus.CONFLICT,
            )
        else:
            self._send_json({**result, "tasks": self.store.list()})

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
