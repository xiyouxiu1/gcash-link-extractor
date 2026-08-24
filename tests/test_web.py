import base64
import json
import threading
import time

from gcash_linker.gcash_protocol import ProtocolError
from gcash_linker.inputs import parse_batch_request
from gcash_linker.web import BatchExecutor, TaskStore, build_task_rows


def _token(email: str) -> str:
    payload = {"https://api.openai.com/profile": {"email": email}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_task_rows_keep_only_redacted_identity_summary():
    token = _token("owner@example.test")
    request = parse_batch_request(token, "http://proxy-a:8000", "http://proxy-b:8000")
    rows = build_task_rows(request, batch_id="batch-test")
    assert rows[0]["task_id"] == "batch-test-001"
    assert rows[0]["email"] == "owner@example.test"
    assert rows[0]["status"] == "queued"
    assert rows[0]["progress"] == 5
    assert rows[0]["finished_at"] == ""
    assert token not in json.dumps(rows)
    assert "proxy-a" not in json.dumps(rows)


def test_terminal_identity_failure_has_finished_at():
    request = parse_batch_request(
        "not-a-jwt",
        "http://proxy-a:8000",
        "http://proxy-b:8000",
    )
    rows = build_task_rows(request, batch_id="batch-test")
    assert rows[0]["status"] == "invalid"
    assert rows[0]["finished_at"]


def test_task_store_restores_complete_progress_after_reopen(tmp_path):
    storage_path = tmp_path / "tasks.json"
    row = {
        "task_id": "persist-001",
        "email": "owner@example.test",
        "status": "running",
        "status_label": "执行中",
        "stage": "正在等待扫码",
        "progress": 72,
        "created_at": "2026-08-25 02:00:00",
        "finished_at": "",
    }
    store = TaskStore(storage_path=storage_path)
    store.add([row])
    store.update("persist-001", progress=78, stage="二维码已生成")

    reopened = TaskStore(storage_path=storage_path)
    assert reopened.list() == [{**row, "progress": 78, "stage": "二维码已生成"}]
    persisted_text = storage_path.read_text(encoding="utf-8")
    assert "accessToken" not in persisted_text
    assert "proxy" not in persisted_text


def test_task_store_persists_qr_separately_from_task_json(tmp_path):
    storage_path = tmp_path / "tasks.json"
    store = TaskStore(storage_path=storage_path)
    store.add([{"task_id": "qr-001", "status": "running", "has_qr": False}])
    png = b"\x89PNG\r\n\x1a\nfixture"

    store.update("qr-001", qr_png=png, payment_status="awaiting_authorization")

    reopened = TaskStore(storage_path=storage_path)
    assert reopened.list()[0]["has_qr"] is True
    assert reopened.read_qr("qr-001") == png
    assert "qr_png" not in storage_path.read_text(encoding="utf-8")


def test_task_store_clears_only_terminal_rows_and_their_qr(tmp_path):
    storage_path = tmp_path / "tasks.json"
    store = TaskStore(storage_path=storage_path)
    store.add(
        [
            {"task_id": "running-001", "status": "running", "has_qr": False},
            {"task_id": "success-001", "status": "success", "has_qr": False},
            {"task_id": "failed-001", "status": "failed", "has_qr": False},
        ]
    )
    png = b"\x89PNG\r\n\x1a\nfixture"
    store.update("running-001", qr_png=png)
    store.update("success-001", qr_png=png)

    deleted, remaining = store.clear_terminal()

    assert deleted == 2
    assert [row["task_id"] for row in remaining] == ["running-001"]
    assert store.read_qr("running-001") == png
    assert store.read_qr("success-001") is None
    assert TaskStore(storage_path=storage_path).list() == remaining


def _wait_for_terminal_rows(store: TaskStore, expected: int, timeout: float = 3) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = store.list()
        terminal = [row for row in rows if row.get("status") in {"success", "failed"}]
        if len(terminal) == expected:
            return rows
        time.sleep(0.01)
    raise AssertionError(f"任务未在 {timeout} 秒内结束：{store.list()}")


def test_batch_executor_enforces_configured_concurrency():
    active = 0
    peak = 0
    lock = threading.Lock()

    def runner(token, billing_proxy, promotion_proxy, progress):
        nonlocal active, peak
        assert token
        assert billing_proxy.startswith("http://")
        assert promotion_proxy.startswith("http://")
        progress("测试执行中", 50)
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return {"status": "success", "status_label": "成功", "stage": "测试完成"}

    tokens = "\n".join(_token(f"owner-{index}@example.test") for index in range(6))
    request = parse_batch_request(
        tokens,
        "http://proxy-a:8000\nhttp://proxy-b:8000",
        "http://proxy-c:8000\nhttp://proxy-d:8000",
        concurrency=2,
        retry_count=0,
    )
    rows = build_task_rows(request, batch_id="concurrency")
    store = TaskStore()
    store.add(rows)
    BatchExecutor(store, runner).submit(request, rows)

    finished = _wait_for_terminal_rows(store, expected=6)
    assert peak == 2
    assert all(row["finished_at"] for row in finished)


def test_batch_executor_retries_failed_attempts():
    calls = []

    def runner(token, billing_proxy, promotion_proxy, progress):
        calls.append((billing_proxy, promotion_proxy))
        if len(calls) < 3:
            raise RuntimeError("模拟上游失败")
        return {"status": "success", "status_label": "成功", "stage": "第三次成功"}

    request = parse_batch_request(
        _token("owner@example.test"),
        "http://proxy-a:8000\nhttp://proxy-b:8000",
        "http://proxy-c:8000\nhttp://proxy-d:8000",
        concurrency=1,
        retry_count=2,
    )
    rows = build_task_rows(request, batch_id="retry")
    store = TaskStore()
    store.add(rows)
    BatchExecutor(store, runner).submit(request, rows)

    finished = _wait_for_terminal_rows(store, expected=1)
    assert len(calls) == 3
    assert finished[0]["attempt"] == 3
    assert finished[0]["max_attempts"] == 3
    assert finished[0]["status"] == "success"


def test_batch_executor_does_not_retry_terminal_protocol_error():
    calls = 0

    def runner(*_):
        nonlocal calls
        calls += 1
        raise ProtocolError(
            "模拟非 0 元",
            retryable=False,
            status="nonzero",
            status_label="非 0 元",
        )

    request = parse_batch_request(
        _token("owner@example.test"),
        "http://proxy-a:8000",
        "http://proxy-b:8000",
        concurrency=1,
        retry_count=3,
    )
    rows = build_task_rows(request, batch_id="terminal")
    store = TaskStore()
    store.add(rows)
    BatchExecutor(store, runner).submit(request, rows)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not store.list()[0].get("finished_at"):
        time.sleep(0.01)
    row = store.list()[0]
    assert calls == 1
    assert row["status"] == "nonzero"
    assert row["status_label"] == "非 0 元"


def test_batch_executor_force_delete_cancels_without_retry_or_writeback():
    started = threading.Event()
    release = threading.Event()
    runner_finished = threading.Event()
    calls = 0

    def runner(_token, _billing_proxy, _promotion_proxy, progress):
        nonlocal calls
        calls += 1
        progress("模拟长任务已开始", 40)
        started.set()
        try:
            assert release.wait(2)
            progress("取消后不应写回", 90)
            return {"status": "success", "stage": "取消后不应完成"}
        finally:
            runner_finished.set()

    request = parse_batch_request(
        _token("owner@example.test"),
        "http://proxy-a:8000",
        "http://proxy-b:8000",
        concurrency=1,
        retry_count=3,
    )
    rows = build_task_rows(request, batch_id="cancel")
    store = TaskStore()
    store.add(rows)
    executor = BatchExecutor(store, runner)
    executor.submit(request, rows)
    assert started.wait(2)

    needs_confirmation = executor.delete_task("cancel-001")
    assert needs_confirmation["requires_confirmation"] is True
    assert store.get("cancel-001") is not None

    deleted = executor.delete_task("cancel-001", force=True)
    assert deleted["deleted"] is True
    assert deleted["forced"] is True
    assert store.get("cancel-001") is None
    release.set()
    assert runner_finished.wait(2)
    time.sleep(0.05)
    assert calls == 1
    assert store.get("cancel-001") is None
