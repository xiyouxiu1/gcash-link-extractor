"""本地 WebUI 的 Playwright 冒烟验收，不接触真实账号或代理。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_PACKAGES.is_dir():
    sys.path.insert(0, str(VENV_PACKAGES))
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from gcash_linker.gcash_protocol import _render_qr_png  # noqa: E402
from gcash_linker.web import BatchExecutor, ConsoleHandler, ConsoleServer, TaskStore  # noqa: E402


def _sample_store(data_dir: Path) -> TaskStore:
    store = TaskStore(storage_path=data_dir / "tasks.json")
    rows = [
        {
            "task_id": "e2e-001",
            "batch_id": "e2e",
            "position": 1,
            "email": "owner@example.test",
            "status": "running",
            "status_label": "执行中",
            "stage": "GCash 二维码已生成，等待扫码授权",
            "progress": 72,
            "created_at": "2026-08-25 02:20:00",
            "finished_at": "",
            "attempt": 1,
            "max_attempts": 4,
            "has_qr": False,
            "payment_status": "awaiting_authorization",
            "payment_url": (
                "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html"
                "?bindingRequestID=e2e&clientId=e2e"
            ),
        },
        {
            "task_id": "e2e-002",
            "batch_id": "e2e",
            "position": 2,
            "email": "paid@example.test",
            "status": "success",
            "status_label": "支付成功",
            "stage": "GCash 支付已完成",
            "progress": 100,
            "created_at": "2026-08-25 02:20:00",
            "finished_at": "2026-08-25 02:21:05",
            "attempt": 1,
            "max_attempts": 4,
            "has_qr": False,
            "payment_status": "paid",
            "amount": "0",
            "payment_url": (
                "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html"
                "?bindingRequestID=paid&clientId=paid"
            ),
        },
        {
            "task_id": "e2e-003",
            "batch_id": "e2e",
            "position": 3,
            "email": "failed@example.test",
            "status": "failed",
            "status_label": "失败",
            "stage": "测试失败任务",
            "progress": 100,
            "created_at": "2026-08-25 02:20:00",
            "finished_at": "2026-08-25 02:22:00",
            "attempt": 4,
            "max_attempts": 4,
            "has_qr": False,
            "payment_status": "",
        },
    ]
    store.add(rows)
    qr_png = _render_qr_png("https://m.gcash.com/e2e-fixture")
    store.update("e2e-001", qr_png=qr_png)
    store.update("e2e-002", qr_png=qr_png)
    return store


def _assert_local_storage(page) -> None:
    values = {
        "gcash-link-extractor.access-tokens.v1": "fixture-token",
        "gcash-link-extractor.billing-exit-proxies.v1": "http://billing.test:8000",
        "gcash-link-extractor.promotion-exit-proxies.v1": "http://promotion.test:8000",
        "gcash-link-extractor.concurrency.v1": "17",
        "gcash-link-extractor.retry-count.v1": "5",
    }
    page.evaluate("values => Object.entries(values).forEach(([key, value]) => localStorage.setItem(key, value))", values)
    page.reload(wait_until="networkidle")
    assert page.locator("#access-tokens").input_value() == values["gcash-link-extractor.access-tokens.v1"]
    assert page.locator("#billing-exit-proxies").input_value() == values["gcash-link-extractor.billing-exit-proxies.v1"]
    assert page.locator("#promotion-exit-proxies").input_value() == values["gcash-link-extractor.promotion-exit-proxies.v1"]
    assert page.locator("#concurrency").input_value() == "17"
    assert page.locator("#retry-count").input_value() == "5"


def main() -> int:
    screenshot_dir = Path(os.getenv("TEMP") or tempfile.gettempdir())
    desktop_shot = screenshot_dir / "gcash-link-extractor-desktop.png"
    mobile_shot = screenshot_dir / "gcash-link-extractor-mobile.png"
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="gcash-e2e-") as temporary:
        data_dir = Path(temporary)
        ConsoleHandler.store = _sample_store(data_dir)
        ConsoleHandler.executor = BatchExecutor(ConsoleHandler.store)
        server = ConsoleServer(("127.0.0.1", 0), ConsoleHandler)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = context.new_page()
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

                assert page.locator(".task-row").count() == 3
                assert page.locator("text=owner@example.test").is_visible()
                _assert_local_storage(page)

                first_row = page.locator('[data-task-id="e2e-001"]')
                handle = first_row.element_handle()
                page.wait_for_timeout(1300)
                assert page.evaluate("node => node.isConnected", handle)
                assert first_row.locator(".task-email").text_content() == "owner@example.test"
                assert first_row.locator(".task-status").text_content() == "执行中"
                assert page.locator('[data-task-id="e2e-002"] .task-status').text_content() == "支付成功"
                assert page.locator("#task-count").text_content() == "3"
                assert page.locator("#progress-count").text_content() == "1"
                assert page.locator("#success-count").text_content() == "1"
                assert page.locator("#failure-count").text_content() == "1"

                first_row.locator(".task-copy-token").click()
                assert page.evaluate("navigator.clipboard.readText()") == "fixture-token"
                assert page.locator("#notice").text_content() == "accessToken 已复制"

                qr_response = page.request.get(f"http://127.0.0.1:{port}/api/tasks/e2e-001/qr")
                assert qr_response.ok
                assert qr_response.headers["content-type"] == "image/png"
                assert qr_response.body().startswith(b"\x89PNG")

                first_row.locator(".qr-preview").click()
                dialog = page.locator(".qr-dialog")
                assert dialog.is_visible()
                assert dialog.locator("img").evaluate("image => image.naturalWidth > 0")
                dialog.locator(".qr-dialog-close").click()

                first_row.locator(".task-result-copy").click()
                assert page.evaluate("navigator.clipboard.readText()") == (
                    "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html"
                    "?bindingRequestID=e2e&clientId=e2e"
                )
                assert page.locator("#notice").text_content() == "GCash 链接已复制"
                page.screenshot(path=str(desktop_shot), full_page=True)

                dialogs: list[str] = []
                def dismiss_dialog(dialog):
                    dialogs.append(dialog.message)
                    dialog.dismiss()

                page.on("dialog", dismiss_dialog)
                page.locator('[data-task-id="e2e-002"] .task-delete').click()
                page.wait_for_function("document.querySelectorAll('.task-row').length === 2")
                assert page.locator(".task-row").count() == 2
                assert dialogs == []
                deleted_qr = page.request.get(
                    f"http://127.0.0.1:{port}/api/tasks/e2e-002/qr"
                )
                assert deleted_qr.status == 404

                clear_button = page.locator("#clear-completed-button")
                assert clear_button.text_content() == "清理未在进行中的任务"
                clear_button.click()
                page.wait_for_function("document.querySelectorAll('.task-row').length === 1")
                assert page.locator('[data-task-id="e2e-001"]').is_visible()
                assert page.locator('[data-task-id="e2e-003"]').count() == 0
                assert page.locator("#notice").text_content() == "已清理 1 条未在进行中的任务"

                first_row.locator(".task-delete").click()
                page.wait_for_timeout(150)
                assert dialogs == ["该任务仍在执行。确定要强制结束并删除吗？"]
                assert first_row.is_visible()

                page.remove_listener("dialog", dismiss_dialog)
                page.once("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
                first_row.locator(".task-delete").click()
                page.wait_for_function("document.querySelectorAll('.task-row').length === 0")
                assert len(dialogs) == 2
                context.close()

                ConsoleHandler.store = _sample_store(data_dir)
                ConsoleHandler.executor = BatchExecutor(ConsoleHandler.store)
                mobile_context = browser.new_context(viewport={"width": 390, "height": 844})
                mobile = mobile_context.new_page()
                mobile.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                mobile.on("pageerror", lambda error: errors.append(str(error)))
                mobile.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert mobile.locator("#batch-form").is_visible()
                assert mobile.locator(".task-row").count() == 3
                mobile.screenshot(path=str(mobile_shot), full_page=True)
                mobile_context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    if errors:
        raise AssertionError("浏览器控制台错误：" + " | ".join(errors))
    print(json.dumps({"desktop": str(desktop_shot), "mobile": str(mobile_shot)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
