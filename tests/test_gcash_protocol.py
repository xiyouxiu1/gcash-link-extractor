import base64
import json

import pytest

from gcash_linker.gcash_protocol import (
    AuthorizationTimeout,
    CHECKOUT_CONFIRM_URL,
    CHECKOUT_TAXES_URL,
    CHECKOUT_UPDATE_URL,
    CHECKOUT_URL,
    CUSTOM_PAYMENT_CONTINUE_URL,
    CUSTOM_PAYMENT_START_URL,
    GCASH_AUTH_OPERATION,
    GCASH_MPAAS_URL,
    GCASH_QR_OPERATION,
    GCashProtocol,
    ProtocolError,
    _pre_proxy_for,
    _safe_error_text,
)


AUTHORIZATION_URL = (
    "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html"
    "?bindingRequestID=binding-fixture&clientId=client-fixture"
)
ADYEN_START_URL = "https://checkoutshopper-live.adyen.com/checkoutshopper/start-fixture"
ADYEN_RETURN_URL = "https://checkoutshopper-live.adyen.com/checkoutshopper/return-fixture"
VERIFY_URL = "https://chatgpt.com/checkout/verify?redirectResult=redirect-fixture"


def _token(email="owner@example.test"):
    payload = {
        "https://api.openai.com/profile": {"email": email},
        "https://api.openai.com/auth": {"chatgpt_account_id": "account-fixture"},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class FakeCookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, **_):
        self.values[name] = value

    def get_dict(self):
        return dict(self.values)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, text="", headers=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.text = text or json.dumps(self.payload)
        self.content = self.text.encode()
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, role, *, amount=0, create_status=200, authorized=True, continue_status="success", bootstrap_status=200):
        self.role = role
        self.amount = amount
        self.create_status = create_status
        self.authorized = authorized
        self.continue_status = continue_status
        self.bootstrap_status = bootstrap_status
        self.cookies = FakeCookies()
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url == "https://chatgpt.com/":
            return FakeResponse(
                self.bootstrap_status,
                text=(
                    '<html data-seq="123" data-build="build-fixture">'
                    '"webDeploymentAttestation":"attestation-fixture"</html>'
                )
            )
        if url == "https://chatgpt.com/api/auth/csrf":
            return FakeResponse(payload={"csrfToken": "fixture"})
        if url == "https://chatgpt.com/checkout/openai_ie/oaics_fixture.data":
            return FakeResponse(
                text='"webDeploymentAttestation":"attestation-from-data" '
                '"sessionId":"22222222-3333-4444-5555-666666666666"'
            )
        if url == "https://chatgpt.com/payments/success.data":
            return FakeResponse(text='{"postCheckoutResult":"success"}')
        if url == "https://chatgpt.com/api/auth/session":
            return FakeResponse(payload={"account": {"planType": "plus"}})
        if url == ADYEN_START_URL:
            return FakeResponse(302, headers={"Location": AUTHORIZATION_URL})
        if url == AUTHORIZATION_URL:
            self.cookies.set("apsessionId", "ap-session-fixture")
            return FakeResponse(text="<html>GCash authorization</html>")
        if url == ADYEN_RETURN_URL:
            return FakeResponse(302, headers={"Location": VERIFY_URL})
        if url == VERIFY_URL:
            return FakeResponse(text='"sessionId":"11111111-2222-3333-4444-555555555555"')
        raise AssertionError(f"unexpected GET on {self.role}: {url}")

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        body = kwargs.get("json") or {}
        if url == CHECKOUT_URL:
            assert self.role == "billing"
            assert body["billing_details"] == {"country": "PH", "currency": "PHP"}
            assert body["promo_campaign"]["promo_campaign_id"] == "plus-1-month-free"
            if self.create_status != 200:
                return FakeResponse(self.create_status, {"error": {"code": "upstream_busy"}})
            return FakeResponse(
                payload={
                    "checkout_session_id": "oaics_fixture",
                    "processor_entity": "openai_ie",
                    "checkout_state": {
                        "email": "owner@example.test",
                        "billingAddress": {
                            "name": "Owner",
                            "address": {"line1": "1 Test Street", "city": "Makati"},
                        },
                    },
                }
            )
        if url == CHECKOUT_UPDATE_URL:
            assert self.role == "promotion"
            assert self.cookies.values["oai-did"]
            assert body["checkout_session_id"] == "oaics_fixture"
            return FakeResponse(payload={"success": True})
        if url == CHECKOUT_TAXES_URL:
            assert self.role == "billing"
            assert body["billing_country"] == "PH"
            return FakeResponse(
                payload={
                    "checkout_session": {
                        "total": {"total": self.amount},
                        "custom_payment_methods": [
                            {"id": "cpmt_gcash", "display_name": "GCash"}
                        ],
                    }
                }
            )
        if url == CHECKOUT_CONFIRM_URL:
            assert self.role == "billing"
            assert body["selected_payment_method_type"] == "cpmt_gcash"
            return FakeResponse(payload={"status": "success"})
        if url == CUSTOM_PAYMENT_START_URL:
            assert self.role == "billing"
            return FakeResponse(
                payload={"status": "requires_action", "next_action": {"url": ADYEN_START_URL}}
            )
        if url == GCASH_MPAAS_URL:
            assert self.role == "billing"
            operation = kwargs["data"]["operationType"]
            if operation == GCASH_QR_OPERATION:
                return FakeResponse(
                    payload={"resultStatus": "1000", "result": {"success": True, "qrCode": "qr-fixture"}}
                )
            if operation == GCASH_AUTH_OPERATION:
                result = {"success": self.authorized}
                if self.authorized:
                    result["redirectUrl"] = ADYEN_RETURN_URL
                return FakeResponse(payload={"resultStatus": "1000", "result": result})
        if url == CUSTOM_PAYMENT_CONTINUE_URL:
            assert self.role == "billing"
            assert body["action_result"]["redirectResult"] == "redirect-fixture"
            return FakeResponse(payload={"status": self.continue_status})
        raise AssertionError(f"unexpected POST on {self.role}: {url}")

    def close(self):
        self.closed = True


class FakeSentinel:
    def __init__(self):
        self.flows = []

    def headers(self, _session, **kwargs):
        self.flows.append(kwargs["flow"])
        return {"OpenAI-Sentinel-Token": f"sentinel-{kwargs['flow']}"}


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def _environment(**options):
    billing = FakeSession("billing", **options)
    promotion = FakeSession("promotion", **options)
    sessions = {
        "http://billing.test:8000": billing,
        "http://promotion.test:8000": promotion,
    }
    sentinel = FakeSentinel()
    protocol = GCashProtocol(
        session_factory=lambda proxy: sessions[proxy],
        sentinel=sentinel,
        sleeper=lambda _: None,
    )
    return protocol, billing, promotion, sentinel


def test_full_gcash_flow_reuses_one_session_and_reaches_paid():
    protocol, billing, promotion, sentinel = _environment()
    updates = []

    result = protocol.run(
        _token(),
        "http://billing.test:8000",
        "http://promotion.test:8000",
        lambda stage, progress, details=None: updates.append((stage, progress, details or {})),
    )

    assert result["status"] == "success"
    assert result["payment_status"] == "paid"
    assert result["payment_url"] == AUTHORIZATION_URL
    assert sentinel.flows == ["chatgpt_checkout", "checkout_session_approval"]
    assert not promotion.calls
    billing_posts = [url for method, url, _ in billing.calls if method == "POST"]
    assert billing_posts[0] == CHECKOUT_URL
    assert CHECKOUT_UPDATE_URL not in billing_posts
    assert CHECKOUT_TAXES_URL in billing_posts
    assert CHECKOUT_CONFIRM_URL in billing_posts
    assert CUSTOM_PAYMENT_CONTINUE_URL in billing_posts
    billing_gets = [url for method, url, _ in billing.calls if method == "GET"]
    assert "https://chatgpt.com/checkout/openai_ie/oaics_fixture.data" in billing_gets
    assert "https://chatgpt.com/payments/success.data" in billing_gets
    assert "https://chatgpt.com/api/auth/session" in billing_gets
    assert any(update[2].get("qr_png", b"").startswith(b"\x89PNG") for update in updates)
    assert billing.closed


def test_nonzero_checkout_stops_before_confirm_and_is_not_retryable():
    protocol, billing, _, _ = _environment(amount=1999)

    with pytest.raises(ProtocolError) as captured:
        protocol.run(
            _token(),
            "http://billing.test:8000",
            "http://promotion.test:8000",
            lambda *_: None,
        )

    assert captured.value.status == "nonzero"
    assert captured.value.retryable is False
    assert CHECKOUT_CONFIRM_URL not in [url for _, url, _ in billing.calls]


def test_retryable_checkout_http_failure_keeps_specific_stage():
    protocol, _, _, _ = _environment(create_status=503)

    with pytest.raises(ProtocolError) as captured:
        protocol.run(
            _token(),
            "http://billing.test:8000",
            "http://promotion.test:8000",
            lambda *_: None,
        )

    assert captured.value.retryable is True
    assert "HTTP 503" in str(captured.value)


def test_checkout_page_preflight_403_does_not_block_checkout():
    protocol, billing, _, _ = _environment(bootstrap_status=403)

    result = protocol.run(
        _token(),
        "http://billing.test:8000",
        "http://promotion.test:8000",
        lambda *_: None,
    )

    assert result["status"] == "success"
    assert [url for method, url, _ in billing.calls if method == "POST"][0] == CHECKOUT_URL


def test_authorization_timeout_happens_after_qr_is_reported():
    billing = FakeSession("billing", authorized=False)
    promotion = FakeSession("promotion", authorized=False)
    sessions = {
        "http://billing.test:8000": billing,
        "http://promotion.test:8000": promotion,
    }
    clock = FakeClock()
    updates = []
    protocol = GCashProtocol(
        session_factory=lambda proxy: sessions[proxy],
        sentinel=FakeSentinel(),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
        authorization_timeout=1,
        poll_interval=0.5,
    )

    with pytest.raises(AuthorizationTimeout):
        protocol.run(
            _token(),
            "http://billing.test:8000",
            "http://promotion.test:8000",
            lambda stage, progress, details=None: updates.append((stage, progress, details or {})),
        )

    assert any(update[2].get("qr_png", b"").startswith(b"\x89PNG") for update in updates)
    assert any("等待 GCash 扫码授权" in update[0] for update in updates)


def test_unsuccessful_continue_is_terminal_protocol_failure():
    protocol, _, _, _ = _environment(continue_status="failed")

    with pytest.raises(ProtocolError) as captured:
        protocol.run(
            _token(),
            "http://billing.test:8000",
            "http://promotion.test:8000",
            lambda *_: None,
        )

    assert captured.value.retryable is False
    assert "status=failed" in str(captured.value)


def test_network_error_text_redacts_proxy_credentials_and_jwt():
    token = "a" * 20 + "." + "b" * 20 + "." + "c" * 12
    rendered = _safe_error_text(
        f"proxy http://owner:secret@proxy.test:8000 rejected token {token}"
    )

    assert "secret" not in rendered
    assert token not in rendered
    assert "http://***@proxy.test:8000" in rendered


def test_socks_handshake_mismatch_is_actionable_and_not_retryable():
    class BrokenProxySession:
        def __init__(self):
            self.cookies = FakeCookies()
            self.closed = False

        def get(self, *_args, **_kwargs):
            raise RuntimeError(
                "Failed to perform, curl: (97) Received invalid version in initial SOCKS5 response"
            )

        def close(self):
            self.closed = True

    billing = BrokenProxySession()
    protocol = GCashProtocol(session_factory=lambda _proxy: billing, sentinel=FakeSentinel())

    with pytest.raises(ProtocolError) as captured:
        protocol.run(
            _token(),
            "socks5h://billing.test:8000",
            "http://promotion.test:8000",
            lambda *_: None,
        )

    assert captured.value.retryable is False
    assert "代理协议不匹配" in str(captured.value)
    assert "http://host:port" in str(captured.value)
    assert billing.closed


def test_authorization_wait_checks_cancellation_in_short_intervals():
    clock = FakeClock()
    protocol = GCashProtocol(
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )
    checks = 0

    def progress(*_args):
        return None

    def check_cancelled():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("cancelled")

    progress.check_cancelled = check_cancelled
    with pytest.raises(RuntimeError, match="cancelled"):
        protocol._sleep_interruptibly(10, progress)

    assert clock.value == 0.25


def test_fixed_local_pre_proxy_is_applied_only_before_http_upstream(monkeypatch):
    monkeypatch.setattr(
        "gcash_linker.gcash_protocol._supports_socks5",
        lambda host, port: (host, port) == ("127.0.0.1", 7897),
    )

    assert _pre_proxy_for("http://provider.test:3010") == "socks5h://127.0.0.1:7897"
    assert _pre_proxy_for("socks5h://provider.test:3010") is None


def test_missing_fixed_local_pre_proxy_falls_back_to_direct(monkeypatch):
    monkeypatch.setattr(
        "gcash_linker.gcash_protocol._supports_socks5",
        lambda _host, _port: False,
    )

    assert _pre_proxy_for("http://provider.test:3010") is None


def test_due_amount_reads_stripe_checkout_session_amount_total():
    from gcash_linker.gcash_protocol import _due_amount

    assert _due_amount({"checkout_session": {"amount_total": 0}}) == 0
    assert _due_amount({"checkout_session": {"amount_total": 1999}}) == 1999


def test_due_amount_matches_source_oaics_minor_units_wrappers():
    from gcash_linker.gcash_protocol import _due_amount, oaics_amount_observations

    zero_payload = {
        "result": {
            "checkout_session": {
                "payment_method_types": ["gcash"],
                "total": {"total": {"minorUnitsAmount": 0, "currency": "PHP"}},
                # 原价不是应付金额，不能被优先读取。
                "unit_price": {"minorUnitsAmount": 110000},
            }
        }
    }
    nonzero_payload = {
        "data": {
            "checkoutState": {
                "totalSummary": {"due": {"minor_units_amount": 1999}},
            }
        }
    }

    assert _due_amount(zero_payload) == 0
    assert _due_amount(nonzero_payload) == 1999
    assert all("unit_price" not in path for path, _ in oaics_amount_observations(zero_payload))
