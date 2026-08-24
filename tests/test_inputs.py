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
    checkout = ProxyPool(request.checkout_proxies, role="Checkout")
    promotion = ProxyPool(request.promotion_proxies, role="促销")
    assert [checkout.acquire(), checkout.acquire(), checkout.acquire()] == [
        "http://proxy-a:8000",
        "http://proxy-b:8000",
        "http://proxy-a:8000",
    ]
    assert promotion.acquire() == "socks5h://proxy-c:9000"


def test_proxy_requires_explicit_url_scheme():
    with pytest.raises(InputValidationError, match="代理 URL"):
        parse_batch_request("token-a", "host:8000", "http://proxy-b:8000")


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
