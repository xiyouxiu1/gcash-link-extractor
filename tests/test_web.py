import base64
import json

from gcash_linker.inputs import parse_batch_request
from gcash_linker.web import build_task_rows


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
    assert rows[0]["progress"] == 15
    assert token not in json.dumps(rows)
    assert "proxy-a" not in json.dumps(rows)
