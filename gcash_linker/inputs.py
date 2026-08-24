"""多行 Token 与双代理池输入模型。

这里不做任何网络请求，也不持久化凭据。真实协议适配器只能接收本模块生成的
内存对象，并通过显式任务边界使用它们。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import cycle
from threading import Lock
from urllib.parse import quote, urlsplit


class InputValidationError(ValueError):
    """用户输入不符合任务契约。"""


_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h"})
DEFAULT_CONCURRENCY = 10
DEFAULT_RETRY_COUNT = 3
MAX_CONCURRENCY = 1000
MAX_RETRY_COUNT = 20


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
    try:
        port = parsed.port
    except ValueError as exc:
        raise InputValidationError(f"{field_name} 的端口无效") from exc
    if not parsed.hostname or port is None:
        raise InputValidationError(f"{field_name} 必须包含 host 和 port")
    if parsed.username is not None and not parsed.password:
        raise InputValidationError(f"{field_name} 含用户名时必须同时提供密码")
    return value


def _normalize_proxy(value: str, field_name: str) -> str:
    if "://" in value:
        return _validate_proxy(value, field_name)
    parts = value.split(":")
    if len(parts) == 2:
        host, port = parts
        normalized = f"http://{host}:{port}"
    elif len(parts) == 4:
        host, port, username, password = parts
        if not username or not password:
            raise InputValidationError(f"{field_name} 的代理用户名和密码不能为空")
        normalized = (
            f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
        )
    else:
        raise InputValidationError(
            f"{field_name} 必须是代理 URL、host:port 或 host:port:user:password"
        )
    return _validate_proxy(normalized, field_name)


def _redact(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://***@{host}{port}" if parsed.username else f"{parsed.scheme}://{host}{port}"


def _bounded_integer(value: object, field_name: str, *, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise InputValidationError(f"{field_name} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InputValidationError(f"{field_name} 必须是整数") from exc
    if isinstance(value, float) and not value.is_integer():
        raise InputValidationError(f"{field_name} 必须是整数")
    if isinstance(value, str) and str(parsed) != value.strip():
        raise InputValidationError(f"{field_name} 必须是整数")
    if not minimum <= parsed <= maximum:
        raise InputValidationError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


@dataclass(frozen=True)
class BatchRequest:
    """一次批量任务的内存输入，不包含任何持久化行为。"""

    access_tokens: tuple[str, ...] = field(repr=False)
    billing_exit_proxies: tuple[str, ...] = field(repr=False)
    promotion_exit_proxies: tuple[str, ...] = field(repr=False)
    concurrency: int = DEFAULT_CONCURRENCY
    retry_count: int = DEFAULT_RETRY_COUNT
    _metadata: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def redacted_summary(self) -> dict[str, object]:
        return {
            "token_count": len(self.access_tokens),
            "billing_exit_proxy_count": len(self.billing_exit_proxies),
            "promotion_exit_proxy_count": len(self.promotion_exit_proxies),
            "concurrency": self.concurrency,
            "retry_count": self.retry_count,
            "billing_exit_proxies": [_redact(value) for value in self.billing_exit_proxies],
            "promotion_exit_proxies": [_redact(value) for value in self.promotion_exit_proxies],
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
    billing_exit_proxy_text: str,
    promotion_exit_proxy_text: str,
    concurrency: object = DEFAULT_CONCURRENCY,
    retry_count: object = DEFAULT_RETRY_COUNT,
) -> BatchRequest:
    """解析并校验多行输入，保留顺序并去重。"""

    tokens = _unique_lines(access_token_text, "accessToken")
    billing_raw = _unique_lines(billing_exit_proxy_text, "账单出口代理池")
    promotion_raw = _unique_lines(promotion_exit_proxy_text, "促销出口代理池")
    billing = tuple(_normalize_proxy(item, "账单出口代理池") for item in billing_raw)
    promotion = tuple(_normalize_proxy(item, "促销出口代理池") for item in promotion_raw)
    worker_count = _bounded_integer(
        concurrency,
        "并发数",
        default=DEFAULT_CONCURRENCY,
        minimum=1,
        maximum=MAX_CONCURRENCY,
    )
    retries = _bounded_integer(
        retry_count,
        "重试数",
        default=DEFAULT_RETRY_COUNT,
        minimum=0,
        maximum=MAX_RETRY_COUNT,
    )
    return BatchRequest(tokens, billing, promotion, worker_count, retries)
