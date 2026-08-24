"""多行 Token 与双代理池输入模型。

这里不做任何网络请求，也不持久化凭据。真实协议适配器只能接收本模块生成的
内存对象，并通过显式任务边界使用它们。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import cycle
from threading import Lock
from urllib.parse import urlsplit


class InputValidationError(ValueError):
    """用户输入不符合任务契约。"""


_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h"})


def _unique_lines(value: str, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise InputValidationError(f"{field_name} 必须是文本")
    values: list[str] = []
    seen: set[str] = set()
    for raw in value.splitlines():
        item = raw.strip()
        if not item or item.startswith("#"):
            continue
        if any(char.isspace() for char in item):
            raise InputValidationError(f"{field_name} 每行只能包含一个值")
        if item not in seen:
            seen.add(item)
            values.append(item)
    if not values:
        raise InputValidationError(f"{field_name} 不能为空")
    return tuple(values)


def _validate_proxy(value: str, field_name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in _PROXY_SCHEMES:
        raise InputValidationError(
            f"{field_name} 只支持 http/https/socks4/socks5/socks5h 代理 URL"
        )
    if not parsed.hostname or parsed.port is None:
        raise InputValidationError(f"{field_name} 必须包含 host 和 port")
    if parsed.username is not None and not parsed.password:
        raise InputValidationError(f"{field_name} 含用户名时必须同时提供密码")
    return value


def _redact(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://***@{host}{port}" if parsed.username else f"{parsed.scheme}://{host}{port}"


@dataclass(frozen=True)
class BatchRequest:
    """一次批量任务的内存输入，不包含任何持久化行为。"""

    access_tokens: tuple[str, ...] = field(repr=False)
    checkout_proxies: tuple[str, ...] = field(repr=False)
    promotion_proxies: tuple[str, ...] = field(repr=False)
    _metadata: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def redacted_summary(self) -> dict[str, object]:
        return {
            "token_count": len(self.access_tokens),
            "checkout_proxy_count": len(self.checkout_proxies),
            "promotion_proxy_count": len(self.promotion_proxies),
            "checkout_proxies": [_redact(value) for value in self.checkout_proxies],
            "promotion_proxies": [_redact(value) for value in self.promotion_proxies],
        }


class ProxyPool:
    """线程安全的独立轮询代理池；不会在日志中暴露代理凭据。"""

    def __init__(self, proxies: tuple[str, ...], *, role: str) -> None:
        if not proxies:
            raise InputValidationError(f"{role} 代理池不能为空")
        self._proxies = tuple(proxies)
        self._iterator = cycle(self._proxies)
        self._lock = Lock()
        self.role = role

    def acquire(self) -> str:
        with self._lock:
            return next(self._iterator)

    @property
    def size(self) -> int:
        return len(self._proxies)


def parse_batch_request(
    access_token_text: str,
    checkout_proxy_text: str,
    promotion_proxy_text: str,
) -> BatchRequest:
    """解析并校验多行输入，保留顺序并去重。"""

    tokens = _unique_lines(access_token_text, "accessToken")
    checkout_raw = _unique_lines(checkout_proxy_text, "Checkout 代理池")
    promotion_raw = _unique_lines(promotion_proxy_text, "促销代理池")
    checkout = tuple(_validate_proxy(item, "Checkout 代理池") for item in checkout_raw)
    promotion = tuple(_validate_proxy(item, "促销代理池") for item in promotion_raw)
    return BatchRequest(tokens, checkout, promotion)
