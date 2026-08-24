import base64
import json

import pytest

from gcash_linker.identity import TokenIdentityError, extract_email_from_access_token


def _token(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{encoded}.signature"


def test_extracts_profile_email_without_network_access():
    token = _token({"https://api.openai.com/profile": {"email": "owner@example.test"}})
    assert extract_email_from_access_token(token) == "owner@example.test"


def test_falls_back_to_top_level_email():
    assert extract_email_from_access_token(_token({"email": "fallback@example.test"})) == (
        "fallback@example.test"
    )


def test_missing_email_returns_none():
    assert extract_email_from_access_token(_token({"sub": "account"})) is None


def test_invalid_payload_does_not_echo_token():
    with pytest.raises(TokenIdentityError, match="Token"):
        extract_email_from_access_token("token-without-payload")
