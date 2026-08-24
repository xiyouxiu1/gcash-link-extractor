import pytest

from gcash_linker import InputValidationError, ProxyPool, parse_batch_request


def test_multiline_tokens_are_trimmed_and_deduplicated():
    request = parse_batch_request(
        " token-a\n\n token-b\ntoken-a\n# comment",
        "http://proxy-a:8000\nhttp://proxy-b:8000",
        "socks5h://proxy-c:9000",
    )
    assert request.access_tokens == ("token-a", "token-b")
    assert request.redacted_summary()["token_count"] == 2


def test_proxy_pools_rotate_independently():
    request = parse_batch_request(
        "token-a\ntoken-b",
        "http://proxy-a:8000\nhttp://proxy-b:8000",
        "socks5h://proxy-c:9000",
    )
    checkout = ProxyPool(request.billing_exit_proxies, role="账单出口")
    promotion = ProxyPool(request.promotion_exit_proxies, role="促销出口")
    assert [checkout.acquire(), checkout.acquire(), checkout.acquire()] == [
        "http://proxy-a:8000",
        "http://proxy-b:8000",
        "http://proxy-a:8000",
    ]
    assert promotion.acquire() == "socks5h://proxy-c:9000"


def test_raw_proxy_formats_are_normalized_as_http():
    request = parse_batch_request(
        "token-a",
        "proxy-a.test:8000",
        "proxy-b.test:9000:owner:secret@word",
    )
    assert request.billing_exit_proxies == ("http://proxy-a.test:8000",)
    assert request.promotion_exit_proxies == (
        "http://owner:secret%40word@proxy-b.test:9000",
    )


def test_malformed_raw_proxy_is_rejected():
    with pytest.raises(InputValidationError, match="host:port:user:password"):
        parse_batch_request("token-a", "host:8000:user", "http://proxy-b:8000")


def test_summary_does_not_include_proxy_password():
    request = parse_batch_request(
        "token-a",
        "http://user:secret@proxy-a:8000",
        "http://user:secret@proxy-b:8000",
    )
    summary = str(request.redacted_summary())
    assert "secret" not in summary


def test_request_repr_does_not_include_credentials():
    request = parse_batch_request(
        "access-token-secret",
        "http://user:proxy-secret@proxy-a:8000",
        "http://proxy-b:8000",
    )
    rendered = repr(request)
    assert "access-token-secret" not in rendered
    assert "proxy-secret" not in rendered


def test_run_limits_have_requested_defaults_and_accept_explicit_values():
    default_request = parse_batch_request(
        "token-a",
        "http://proxy-a:8000",
        "http://proxy-b:8000",
    )
    assert default_request.concurrency == 10
    assert default_request.retry_count == 3

    configured = parse_batch_request(
        "token-a",
        "http://proxy-a:8000",
        "http://proxy-b:8000",
        concurrency=37,
        retry_count=6,
    )
    assert configured.concurrency == 37
    assert configured.retry_count == 6


@pytest.mark.parametrize(
    ("concurrency", "retry_count", "message"),
    [(0, 3, "并发数"), (1001, 3, "并发数"), (10, -1, "重试数"), (10, 21, "重试数")],
)
def test_run_limits_reject_out_of_range_values(concurrency, retry_count, message):
    with pytest.raises(InputValidationError, match=message):
        parse_batch_request(
            "token-a",
            "http://proxy-a:8000",
            "http://proxy-b:8000",
            concurrency=concurrency,
            retry_count=retry_count,
        )
