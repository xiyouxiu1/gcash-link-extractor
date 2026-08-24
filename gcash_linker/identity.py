"""从 Access Token 的 JWT payload 中读取邮箱，不发起网络请求。"""
from __future__ import annotations

import base64
import json
import re
from typing import Any


class TokenIdentityError(ValueError):
    """Token 不是可解析的 JWT。"""


_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def decode_access_token_payload(token: str) -> dict[str, Any]:
    """解码 JWT payload；不验证签名，也不发送请求。"""

    if not isinstance(token, str):
        raise TokenIdentityError("Token 必须是文本")
    parts = token.strip().split(".")
    if len(parts) < 2 or not parts[1]:
        raise TokenIdentityError("Token 格式无效")
    encoded = parts[1].replace("-", "+").replace("_", "/")
    encoded += "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise TokenIdentityError("Token payload 无法解析") from exc
    if not isinstance(payload, dict):
        raise TokenIdentityError("Token payload 必须是对象")
    return payload


def extract_email_from_access_token(token: str) -> str | None:
    """读取 profile.email，兼容 payload 顶层的 email 字段。"""

    payload = decode_access_token_payload(token)
    profile = payload.get("https://api.openai.com/profile")
    candidates = []
    if isinstance(profile, dict):
        candidates.append(profile.get("email"))
    candidates.append(payload.get("email"))
    for value in candidates:
        email = str(value or "").strip()
        if email and _EMAIL_RE.fullmatch(email):
            return email
    return None
