"""自有 GCash Checkout、二维码授权与支付状态协议。"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import random
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from .identity import extract_email_from_access_token
from .sentinel import SentinelClient, SentinelError, default_sdk_url


CHATGPT_ORIGIN = "https://chatgpt.com"
CHECKOUT_URL = f"{CHATGPT_ORIGIN}/backend-api/payments/checkout"
CHECKOUT_UPDATE_URL = f"{CHATGPT_ORIGIN}/backend-api/payments/checkout/update"
CHECKOUT_TAXES_URL = f"{CHATGPT_ORIGIN}/backend-api/payments/checkout/taxes"
CHECKOUT_CONFIRM_URL = f"{CHATGPT_ORIGIN}/backend-api/payments/checkout/confirm"
CUSTOM_PAYMENT_START_URL = (
    f"{CHATGPT_ORIGIN}/backend-api/payments/checkout/custom_payment_method/start"
)
CUSTOM_PAYMENT_CONTINUE_URL = (
    f"{CHATGPT_ORIGIN}/backend-api/payments/checkout/custom_payment_method/continue"
)
PROMO_CAMPAIGN = "plus-1-month-free"

GCASH_MPAAS_URL = "https://mgs-gw.paas.mynt.xyz/mgw.htm"
GCASH_APP_ID = "D54528A131559"
GCASH_WORKSPACE_ID = "PROD"
GCASH_TENANT_ID = "MYNTPH"
GCASH_QR_OPERATION = "ap.mobilewallet.gka.authorisation.stateless.consult"
GCASH_AUTH_OPERATION = "ap.mobilewallet.query.accessToken"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Not.A/Brand";v="99", "Chromium";v="136", "Google Chrome";v="136"'
_TERMINAL_PAYMENT_STATUSES = frozenset({"paid", "completed", "success"})
_RETRYABLE_HTTP = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_LOCAL_PRE_PROXY = "socks5h://127.0.0.1:7897"

# GCash 源项目只使用已经验证过的 Chrome 画像子集；每个账号的整条
# Checkout/GCash 链路固定一个画像，避免中途切换 User-Agent 或 sec-ch-ua。
_GCASH_BROWSER_PROFILES = (
    {"impersonate": "chrome146", "version": "146.0.0.0", "not_a_brand": '"Not?A_Brand";v="99"'},
    {"impersonate": "chrome142", "version": "142.0.0.0", "not_a_brand": '"Not/A)Brand";v="8"'},
    {"impersonate": "chrome136", "version": "136.0.0.0", "not_a_brand": '"Not.A/Brand";v="99"'},
)
_GCASH_QR_PAGE_TIMEOUT = 15.0
_GCASH_QR_WAIT_SECONDS = 120.0
_GCASH_QR_RETRY_INTERVAL = 3.0
_GCASH_QR_RETRIES = 20
_GCASH_AUTH_POLL_INTERVAL = 10.0

ProgressCallback = Callable[..., None]
SessionFactory = Callable[[str], Any]


class ProtocolError(RuntimeError):
    """可面向任务列表显示的协议错误。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status: str = "failed",
        status_label: str = "失败",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.status_label = status_label


class AuthorizationTimeout(ProtocolError):
    def __init__(self, seconds: float, *, detail: str = "") -> None:
        super().__init__(
            f"GCash 扫码授权在 {int(seconds)} 秒内未完成{detail}",
            retryable=False,
            status="expired",
            status_label="授权超时",
        )


@dataclass
class BrowserContext:
    device_id: str
    session_id: str
    client_build_number: str = ""
    client_version: str = ""
    attestation: str = ""
    sentinel_sdk_url: str = ""
    user_agent: str = USER_AGENT
    sec_ch_ua: str = SEC_CH_UA


def _browser_profile(session: Any | None = None) -> dict[str, str]:
    profile = getattr(session, "_protocol_browser_profile", None) if session is not None else None
    if isinstance(profile, dict) and profile.get("impersonate"):
        return profile
    return _GCASH_BROWSER_PROFILES[-1]


def _browser_user_agent(session: Any | None = None) -> str:
    profile = _browser_profile(session)
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{profile.get('version', '136.0.0.0')} Safari/537.36"
    )


def _browser_sec_ch_ua(session: Any | None = None) -> str:
    profile = _browser_profile(session)
    major = str(profile.get("version") or "136").split(".", 1)[0]
    return (
        f"{profile.get('not_a_brand', SEC_CH_UA)}, "
        f'"Chromium";v="{major}", "Google Chrome";v="{major}"'
    )


def _apply_session_profile(context: BrowserContext, session: Any) -> None:
    context.user_agent = _browser_user_agent(session)
    context.sec_ch_ua = _browser_sec_ch_ua(session)


def create_session(proxy: str) -> Any:
    """创建固定出口和 Chrome 指纹的 curl_cffi Session。"""

    try:
        from curl_cffi import requests as curl_requests
        from curl_cffi.const import CurlOpt
    except ImportError as exc:
        raise ProtocolError(
            "缺少 curl_cffi，请重新运行 start.bat 安装依赖",
            retryable=False,
        ) from exc
    curl_options = {}
    pre_proxy = _pre_proxy_for(proxy)
    if pre_proxy:
        curl_options[CurlOpt.PRE_PROXY] = pre_proxy
    profiles = list(_GCASH_BROWSER_PROFILES)
    random.shuffle(profiles)
    unsupported: list[str] = []
    for profile in profiles:
        try:
            session = curl_requests.Session(
                impersonate=profile["impersonate"],
                proxies={"http": proxy, "https": proxy},
                curl_options=curl_options,
            )
            try:
                setattr(session, "_protocol_browser_profile", dict(profile))
            except Exception:
                pass
            return session
        except Exception as exc:
            text = str(exc).lower()
            if "impersonat" in text and "support" in text:
                unsupported.append(f"{profile['impersonate']}: {exc}")
                continue
            raise ProtocolError(f"创建代理会话失败：{type(exc).__name__}") from exc
    detail = "；".join(unsupported[-3:])
    raise ProtocolError(
        f"curl_cffi 没有可用的 Chrome 画像{(': ' + detail[:220]) if detail else ''}",
        retryable=False,
    )


def _pre_proxy_for(upstream_proxy: str) -> str | None:
    upstream = urlsplit(upstream_proxy)
    if upstream.scheme.lower() not in {"http", "https"}:
        return None
    # 部分供应商会拒绝国内公网 IP 直连。7897 可用时只把本机 Clash/VPN
    # 作为连接供应商代理的前置线路；不可用时仍直连，最终出口始终是用户输入的代理。
    if not _supports_socks5("127.0.0.1", 7897):
        return None
    return _LOCAL_PRE_PROXY


def _supports_socks5(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35) as connection:
            connection.settimeout(0.35)
            connection.sendall(b"\x05\x01\x00")
            return connection.recv(2) == b"\x05\x00"
    except OSError:
        return False


class GCashProtocol:
    """单账号完整 OAICS GCash 协议；整条链路固定同一个 Session 和出口。"""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = create_session,
        sentinel: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        authorization_timeout: float = 300.0,
        poll_interval: float = 10.0,
        request_timeout: int = 50,
    ) -> None:
        self.session_factory = session_factory
        self.sentinel = sentinel or SentinelClient(timeout=min(request_timeout, 45))
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.authorization_timeout = authorization_timeout
        self.poll_interval = poll_interval
        self.request_timeout = request_timeout

    def run(
        self,
        access_token: str,
        billing_proxy: str,
        promotion_proxy: str,
        progress: ProgressCallback,
    ) -> dict[str, Any]:
        token = str(access_token or "").strip()
        email = extract_email_from_access_token(token) or ""
        context = BrowserContext(device_id=str(uuid.uuid4()), session_id="")
        session = None
        try:
            # 源项目 GCash 流程从创建 Checkout 到 continue 始终复用一个
            # Session/代理；promotion_proxy 仅保留为兼容旧 Web API，不在链路
            # 中切换出口，否则 GCash 的 Cookie、apsessionId 和授权状态会丢失。
            _ = promotion_proxy
            _report(progress, "正在建立 GCash 出口会话", 12)
            session = self.session_factory(billing_proxy)
            # 源项目在整个 OAICS 链路中复用同一 Session；把它挂到上下文，
            # 让后续请求头可以读取当前 Cookie 派生的浏览器观察标识。
            setattr(context, "_session", session)
            _apply_session_profile(context, session)
            self._bootstrap(session, context)

            _report(progress, "正在创建 PH/PHP GCash Checkout", 22)
            created = self._create_checkout(session, token, context)
            checkout_id = _first_text(created, "checkout_session_id", "session_id")
            processor = _first_text(created, "processor_entity", "processorEntity") or "openai_ie"
            if not checkout_id.startswith("oaics_"):
                raise ProtocolError("上游未返回 OAICS 自定义 Checkout", retryable=False)
            checkout_url = f"{CHATGPT_ORIGIN}/checkout/{processor}/{checkout_id}"

            page_data = self._fetch_checkout_page_data(
                session, token, context, checkout_id, processor,
                referer=checkout_url,
            )
            if not page_data.get("ok"):
                # `.data` 用于恢复动态 Attestation/Session；偶发 403 时仍让
                # 后续请求继续，和源项目的容错行为保持一致。
                _report(progress, "Checkout 页面上下文未完整返回，继续协议流程", 30)
            else:
                _report(progress, "Checkout 页面上下文已同步", 30)

            _report(progress, "正在提交 PH 账单地址并校验金额", 46)
            billing = _billing_details(created, email)
            taxed = self._taxes(
                session,
                token,
                context,
                checkout_id,
                processor,
                checkout_url,
                billing,
            )
            amount = _due_amount(taxed)
            if amount is None:
                amount = _due_amount(created)
            if amount is None:
                raise ProtocolError("上游未返回可验证的应付金额", retryable=False)
            if amount != 0:
                raise ProtocolError(
                    f"应付金额不是 0：{amount} PHP，已停止生成二维码",
                    retryable=False,
                    status="nonzero",
                    status_label="非 0 元",
                )
            custom_method = _gcash_custom_method(taxed) or _gcash_custom_method(created)
            if not custom_method:
                raise ProtocolError("Checkout 尚未返回 GCash 支付方式", retryable=False)

            _report(
                progress,
                "正在确认 GCash 支付方式",
                56,
                {"amount": "0", "currency": "PHP"},
            )
            self._confirm(
                session,
                token,
                context,
                checkout_id,
                processor,
                checkout_url,
                custom_method,
            )

            _report(progress, "正在获取 GCash 授权链接", 64)
            started = self._start(
                session,
                token,
                context,
                checkout_id,
                processor,
                checkout_url,
                custom_method,
            )
            next_action = started.get("next_action") or {}
            payment_url = str(next_action.get("url") or "").strip()
            if not payment_url:
                raise ProtocolError("GCash 未返回授权链接", retryable=False)
            qr = self._fetch_gcash_qr_png(session, payment_url, progress)
            qr_png = qr.get("qr_png")
            authorization_url = str(qr.get("authorization_url") or "").strip()
            display_payment_url = authorization_url or payment_url
            if not qr_png:
                raise ProtocolError(
                    str(qr.get("qr_error") or "GCash 未获取到二维码"),
                    retryable=True,
                )
            _report(
                progress,
                "GCash 二维码已生成，等待扫码授权",
                72,
                {
                    "qr_png": qr_png,
                    "checkout_url": checkout_url,
                    "payment_url": display_payment_url,
                    "redirect_url": payment_url,
                    "payment_status": "awaiting_authorization",
                    "amount": "0",
                    "currency": "PHP",
                },
            )

            redirect_result = str(qr.get("redirect_result") or "").strip()
            verify_url = str(qr.get("verify_url") or "").strip()
            if not redirect_result:
                if not authorization_url:
                    raise ProtocolError("GCash 二维码未返回授权页地址", retryable=True)
                redirect_result, verify_url = self._wait_authorization(
                    session, authorization_url, context, progress,
                )
            verify_session_id = str(
                getattr(session, "_protocol_oai_verify_session_id", "") or ""
            ).strip()
            if verify_session_id:
                context.session_id = verify_session_id
            verify_attestation = str(
                getattr(session, "_protocol_oai_web_deployment_attestation", "") or ""
            ).strip()
            if verify_attestation:
                context.attestation = verify_attestation
            _report(
                progress,
                "正在提交 GCash 授权结果",
                94,
                {"payment_status": "processing"},
            )
            continued = self._continue(
                session,
                token,
                context,
                checkout_id,
                processor,
                verify_url,
                redirect_result,
            )
            payment_status = str(
                continued.get("payment_status") or continued.get("status") or ""
            ).lower()
            if payment_status not in _TERMINAL_PAYMENT_STATUSES:
                raise ProtocolError(
                    f"GCash 授权回跳未完成：status={payment_status or 'unknown'}",
                    retryable=False,
                )
            sync = self._sync_gcash_success_state(
                session, token, context, checkout_id, processor, verify_url,
            )
            return {
                "status": "success",
                "status_label": "支付成功",
                "stage": "GCash 支付已完成",
                "progress": 100,
                "checkout_url": checkout_url,
                "payment_url": display_payment_url,
                "payment_status": "paid",
                "amount": "0",
                "currency": "PHP",
                "has_qr": True,
                "payment_sync": sync,
            }
        except ProtocolError:
            raise
        except SentinelError as exc:
            raise ProtocolError(str(exc), retryable=exc.retryable) from exc
        except Exception as exc:
            error_text = _safe_error_text(str(exc))
            if "curl: (97)" in error_text or "invalid version in initial SOCKS5 response" in error_text:
                raise ProtocolError(
                    "代理协议不匹配：当前地址按 SOCKS5 连接，但代理出口返回的不是 "
                    "SOCKS5。请改用 http://host:port，或直接输入 "
                    "host:port:user:password",
                    retryable=False,
                ) from exc
            raise ProtocolError(
                f"协议请求异常：{type(exc).__name__}: {error_text}"
            ) from exc
        finally:
            self._close(session)

    def _bootstrap(self, session: Any, context: BrowserContext) -> None:
        _set_cookie(session, "oai-did", context.device_id, ".chatgpt.com")
        # 先访问根页面取得动态上下文。带 promo_campaign 的页面地址在部分
        # 出口会被 ChatGPT/CDN 返回 403，但不代表后续 checkout 接口不可用。
        url = f"{CHATGPT_ORIGIN}/"
        response = session.get(
            url,
            headers={
                "User-Agent": context.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-PH,en;q=0.9",
            },
            timeout=self.request_timeout,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 403:
            html = ""
        else:
            self._require_http(response, "加载 ChatGPT Checkout 页面")
            html = str(getattr(response, "text", "") or "")
        _apply_page_context(context, html)
        if not context.session_id:
            context.session_id = str(uuid.uuid4())
        try:
            session.get(
                f"{CHATGPT_ORIGIN}/api/auth/csrf",
                headers={"User-Agent": context.user_agent, "Accept": "application/json,text/plain,*/*"},
                timeout=min(20, self.request_timeout),
            )
        except Exception:
            pass

    def _create_checkout(
        self,
        session: Any,
        token: str,
        context: BrowserContext,
    ) -> dict[str, Any]:
        route = "/backend-api/payments/checkout"
        page_url = f"{CHATGPT_ORIGIN}/?promo_campaign={PROMO_CAMPAIGN}"
        sentinel_headers = self.sentinel.headers(
            session,
            device_id=context.device_id,
            flow="chatgpt_checkout",
            page_url=page_url,
            user_agent=context.user_agent,
            sdk_url=context.sentinel_sdk_url or default_sdk_url(),
        )
        response = session.post(
            CHECKOUT_URL,
            json={
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": "PH", "currency": "PHP"},
                "checkout_ui_mode": "custom",
                "entry_point": "all_plans_pricing_modal",
                "promo_campaign": {
                    "promo_campaign_id": PROMO_CAMPAIGN,
                    "is_coupon_from_query_param": False,
                },
            },
            headers={
                **_checkout_headers(token, context, route, page_url),
                **sentinel_headers,
                "OAI-Telemetry": "[1,null]",
            },
            timeout=self.request_timeout,
        )
        return self._json_response(response, "创建 GCash Checkout")

    def _fetch_checkout_page_data(
        self,
        session: Any,
        token: str,
        context: BrowserContext,
        checkout_id: str,
        processor: str,
        *,
        referer: str,
    ) -> dict[str, Any]:
        """复刻源项目加载 Checkout RSC 的步骤，恢复动态页面上下文。"""
        route = f"/checkout/{processor or 'openai_ie'}/{checkout_id}.data"
        try:
            response = session.get(
                f"{CHATGPT_ORIGIN}{route}",
                params={"_routes": "routes/checkout.$entity.$checkoutId"},
                headers={
                    **_checkout_headers(token, context, route, referer),
                    "Accept": "*/*",
                    "Accept-Language": "en-PH,en;q=0.9",
                    "User-Agent": context.user_agent,
                },
                timeout=min(45, self.request_timeout),
            )
            status = int(getattr(response, "status_code", 0) or 0)
            body = str(getattr(response, "text", "") or "")
            # 源项目即使页面响应不是 200 也先尝试解析动态字段；部分 CDN
            # 响应会带上下文片段，后续协议请求仍应复用它。
            _apply_page_context(context, body, prefer_session_id=True)
            return {
                "ok": status == 200,
                "http": status,
                "attestation_applied": bool(context.attestation),
                "session_id_applied": bool(context.session_id),
            }
        except Exception as exc:
            return {"ok": False, "error": _safe_error_text(str(exc))}

    def _update_promotion(
        self,
        session: Any,
        token: str,
        context: BrowserContext,
        checkout_id: str,
        processor: str,
        checkout_url: str,
    ) -> dict[str, Any]:
        route = "/backend-api/payments/checkout/update"
        response = session.post(
            CHECKOUT_UPDATE_URL,
            json={
                "checkout_session_id": checkout_id,
                "processor_entity": processor,
                "plan_name": "chatgptplusplan",
                "price_interval": "month",
                "seat_quantity": 1,
                "discount_code": None,
                "promo_campaign": {
                    "promo_campaign_id": PROMO_CAMPAIGN,
                    "is_coupon_from_query_param": False,
                },
            },
            headers=_checkout_headers(token, context, route, checkout_url),
            timeout=self.request_timeout,
        )
        payload = self._json_response(response, "激活 Checkout 优惠")
        if payload.get("success") is False:
            raise ProtocolError("促销出口返回优惠激活失败", retryable=False)
        return payload

    def _taxes(
        self,
        session: Any,
        token: str,
        context: BrowserContext,
        checkout_id: str,
        processor: str,
        checkout_url: str,
        billing: dict[str, Any],
    ) -> dict[str, Any]:
        route = "/backend-api/payments/checkout/taxes"
        response = session.post(
            CHECKOUT_TAXES_URL,
            json={
                "checkout_session_id": checkout_id,
                "checkout_email": billing["email"],
                "billing_country": "PH",
                "billing_name": billing["name"],
                "currency": "php",
                "processor_entity": processor,
                "billing_address": billing["address"],
            },
            headers=_checkout_headers(token, context, route, checkout_url),
            timeout=self.request_timeout,
        )
        payload = self._json_response(response, "提交 PH 账单地址")
        checkout = payload.get("checkout_session")
        return checkout if isinstance(checkout, dict) else payload

    def _confirm(
        self,
        session: Any,
        token: str,
        context: BrowserContext,
        checkout_id: str,
        processor: str,
        checkout_url: str,
        custom_method: str,
    ) -> dict[str, Any]:
        route = "/backend-api/payments/checkout/confirm"
        sentinel_headers = self.sentinel.headers(
            session,
            device_id=context.device_id,
            flow="checkout_session_approval",
            page_url=checkout_url,
            user_agent=context.user_agent,
            sdk_url=context.sentinel_sdk_url or default_sdk_url(),
        )
        response = session.post(
            CHECKOUT_CONFIRM_URL,
            json={
                "checkout_session_id": checkout_id,
                "selected_payment_method_type": custom_method,
            },
            headers={
                **_checkout_headers(token, context, route, checkout_url),
                **sentinel_headers,
                "OAI-Telemetry": "[1,null]",
            },
            timeout=self.request_timeout,
        )
        payload = self._json_response(response, "确认 GCash 支付方式")
        status = str(payload.get("status") or "").lower()
        if status == "blocked":
            raise ProtocolError("GCash 支付方式确认被上游拦截", retryable=True)
        if status != "success":
            raise ProtocolError(
                f"确认 GCash 支付方式失败：status={status or 'unknown'}",
                retryable=False,
            )
        return payload

    def _start(
        self,
        session: Any,
        token: str,
        context: BrowserContext,
        checkout_id: str,
        processor: str,
        checkout_url: str,
        custom_method: str,
    ) -> dict[str, Any]:
        route = "/backend-api/payments/checkout/custom_payment_method/start"
        response = session.post(
            CUSTOM_PAYMENT_START_URL,
            json={
                "checkout_session_id": checkout_id,
                "custom_payment_method_type_id": custom_method,
            },
            headers=_checkout_headers(token, context, route, checkout_url),
            timeout=max(60, self.request_timeout),
        )
        payload = self._json_response(response, "启动 GCash 支付")
        if str(payload.get("status") or "").lower() != "requires_action":
            raise ProtocolError("GCash 支付没有进入授权阶段", retryable=False)
        return payload

    def _open_authorization_page(self, session: Any, payment_url: str) -> tuple[str, str]:
        current = payment_url
        for _ in range(8):
            if not _allowed_payment_url(current):
                raise ProtocolError("GCash 授权链接跳转到不允许的域名", retryable=False)
            response = session.get(
                current,
                headers={"User-Agent": _browser_user_agent(session), "Accept": "text/html,*/*;q=0.8"},
                timeout=self.request_timeout,
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            location = str((getattr(response, "headers", {}) or {}).get("Location") or "").strip()
            if 300 <= status < 400 and location:
                current = urljoin(current, location)
                continue
            self._require_http(response, "打开 GCash 授权页面")
            parsed = urlsplit(current)
            if (parsed.hostname or "").lower().rstrip(".") != "m.gcash.com":
                raise ProtocolError("最终授权链接没有进入 m.gcash.com", retryable=False)
            return current, str(getattr(response, "text", "") or "")
        raise ProtocolError("GCash 授权链接重定向次数过多", retryable=False)

    def _request_qr_value(self, session: Any, authorization_url: str) -> str:
        parsed = _require_gcash_authorization_url(authorization_url)
        query = parse_qs(parsed.query)
        channel = "aggregator" if query.get("sellerId") and query.get("sellerName") else "generic"
        env_token = _cookie_value(session, "env-token") or str(uuid.uuid4())
        if not _cookie_value(session, "env-token"):
            _set_cookie(session, "env-token", env_token, "m.gcash.com")
        original_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment or "/")
        )
        body = {
            "channel": channel,
            "urlParameters": parsed.query,
            "originalUrl": original_url,
            "expireSeconds": 300,
            "bizType": "ACQUIRING",
            "envInfo": {
                "tokenId": env_token,
                "osType": "Windows",
                "osVersion": "10",
                "browserType": "Chrome",
                "browserVersion": _browser_profile(session).get("version", "136.0.0.0"),
                "terminalType": "WEB",
            },
        }
        response = self._mpaas_post(session, GCASH_QR_OPERATION, body, authorization_url)
        payload = self._json_response(response, "读取 GCash 二维码")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if str(payload.get("resultStatus") or "1000") != "1000":
            raise ProtocolError("GCash 二维码接口尚未就绪")
        qr_value = str(result.get("qrCode") or "").strip()
        if not result.get("success") or not qr_value or len(qr_value) > 4096:
            raise ProtocolError("GCash 二维码接口未返回有效 qrCode")
        return qr_value

    def _fetch_gcash_qr_png(
        self,
        session: Any,
        redirect_url: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """按源项目顺序读取最终长链接，并调用真实 GCash QR RPC。

        只接受 GCash 页面返回的 PNG data URI，或 GCash 页面真实调用的
        ``stateless.consult`` 返回的 qrCode；不会用浏览器截图或外部图片替代。
        """
        result: dict[str, Any] = {
            "qr_png": None,
            "qr_source": "gcash_stateless_consult_missing",
            "qr_error": "",
            "redirect_result": "",
            "verify_url": "",
            "authorization_url": "",
        }
        if not _allowed_payment_url(redirect_url):
            result["qr_error"] = "GCash最终链接不是允许的 HTTPS 地址"
            return result
        deadline = self.monotonic() + _GCASH_QR_WAIT_SECONDS
        last_error = ""
        for attempt in range(1, _GCASH_QR_RETRIES + 1):
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            try:
                _report(
                    progress,
                    f"正在读取GCash二维码接口（第{attempt}/{_GCASH_QR_RETRIES}次）",
                    min(70, 65 + attempt),
                )
                current_url = redirect_url
                response = None
                for _ in range(6):
                    if not (_allowed_payment_url(current_url) or _is_verify_url(current_url)):
                        raise ProtocolError("最终链接重定向到不允许的地址", retryable=False)
                    request_remaining = deadline - self.monotonic()
                    if request_remaining <= 0:
                        raise TimeoutError(f"GCash最终长链接等待超过{int(_GCASH_QR_WAIT_SECONDS)}秒")
                    response = session.get(
                        current_url,
                        headers={
                            "User-Agent": _browser_user_agent(session),
                            "Accept": "text/html,*/*;q=0.8",
                        },
                        timeout=max(1, min(_GCASH_QR_PAGE_TIMEOUT, request_remaining)),
                        allow_redirects=False,
                    )
                    location = str((getattr(response, "headers", {}) or {}).get("Location") or "").strip()
                    status = int(getattr(response, "status_code", 0) or 0)
                    if not (300 <= status < 400 and location):
                        break
                    current_url = urljoin(current_url, location)
                    parsed = urlsplit(current_url)
                    if _is_verify_url(current_url):
                        result["verify_url"] = current_url
                        result["redirect_result"] = _redirect_result(current_url)
                    elif (
                        (parsed.hostname or "").lower().rstrip(".") == "m.gcash.com"
                        and parsed.path.startswith("/gcashapp/gcash-merchants-auth/")
                    ):
                        result["authorization_url"] = current_url
                if response is None:
                    raise RuntimeError("最终链接没有返回响应")
                response_text = str(getattr(response, "text", "") or "")
                if _is_verify_url(current_url):
                    _apply_page_context_from_html(session, response_text, verify=True)
                data_uri_png = _qr_from_data_uri(response_text)
                if data_uri_png:
                    result.update({"qr_png": data_uri_png, "qr_source": "final_link_data_uri"})
                    return result
                current = urlsplit(current_url)
                if (
                    (current.hostname or "").lower().rstrip(".") == "m.gcash.com"
                    and current.path.startswith("/gcashapp/gcash-merchants-auth/")
                ):
                    result["authorization_url"] = current_url
                    qr_value = self._request_qr_value(
                        session,
                        current_url,
                    )
                    result.update({
                        "qr_png": _render_qr_png(qr_value),
                        "qr_source": "gcash_stateless_consult",
                    })
                    return result
                if result.get("redirect_result"):
                    return result
                last_error = "GCash最终链接未进入二维码授权页面"
            except ProtocolError as exc:
                if not exc.retryable:
                    raise
                last_error = str(exc)
            except Exception as exc:
                last_error = _safe_error_text(str(exc))
            remaining = deadline - self.monotonic()
            if attempt < _GCASH_QR_RETRIES and remaining > 0:
                self._sleep_interruptibly(
                    min(_GCASH_QR_RETRY_INTERVAL, remaining), progress,
                )
        result["qr_error"] = (
            f"{last_error or '未获取到二维码'}；已在{int(_GCASH_QR_WAIT_SECONDS)}秒内重试"
            f"{attempt if 'attempt' in locals() else 0}次"
        )
        _report(progress, "GCash二维码接口未返回二维码，已记录失败原因", 70)
        return result

    def _wait_authorization(
        self,
        session: Any,
        authorization_url: str,
        context: BrowserContext,
        progress: ProgressCallback,
    ) -> tuple[str, str]:
        deadline = self.monotonic() + self.authorization_timeout
        attempt = 0
        last_error = ""
        while self.monotonic() < deadline:
            attempt += 1
            _report(
                progress,
                f"等待 GCash 扫码授权（第 {attempt} 次）",
                min(90, 72 + attempt),
                {"payment_status": "awaiting_authorization"},
            )
            try:
                state = self._request_authorization_state(session, authorization_url)
                redirect_url = str(state.get("redirect_url") or "").strip()
                if state.get("authorized") and redirect_url:
                    redirect_result, verify_url, html = self._follow_authorization_redirect(
                        session,
                        redirect_url,
                        deadline,
                    )
                    _apply_page_context(context, html, prefer_session_id=True)
                    if redirect_result:
                        return redirect_result, verify_url
                    last_error = "GCash已授权但回跳未返回 redirectResult"
                elif state.get("authorized"):
                    last_error = "GCash已授权但未返回 redirectUrl"
                else:
                    last_error = ""
            except ProtocolError as exc:
                if not exc.retryable:
                    raise
                last_error = str(exc)
            except Exception as exc:
                last_error = _safe_error_text(str(exc))
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self._sleep_interruptibly(min(self.poll_interval, remaining), progress)
        detail = f"：{last_error[:160]}" if last_error else ""
        raise AuthorizationTimeout(self.authorization_timeout, detail=detail)

    def _sleep_interruptibly(self, seconds: float, progress: ProgressCallback) -> None:
        deadline = self.monotonic() + max(0.0, seconds)
        while True:
            _check_cancelled(progress)
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return
            self.sleeper(min(0.25, remaining))

    def _request_authorization_state(self, session: Any, authorization_url: str) -> dict[str, Any]:
        parsed = _require_gcash_authorization_url(authorization_url)
        query = parse_qs(parsed.query)
        binding_id = _first_query(
            query,
            "bindingRequestID",
            "bindingId",
            "netAuthId",
            "state",
        )
        auth_id = _first_query(query, "clientId", "authId")
        if not binding_id or not auth_id:
            raise ProtocolError("GCash 授权链接缺少 bindingId 或 authId", retryable=False)
        response = self._mpaas_post(
            session,
            GCASH_AUTH_OPERATION,
            {"bindingId": binding_id, "authId": auth_id},
            authorization_url,
        )
        payload = self._json_response(response, "查询 GCash 授权状态")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        redirect_url = str(result.get("redirectUrl") or result.get("redirectURL") or "").strip()
        access_tokens = result.get("accessTokens")
        return {
            "authorized": bool(redirect_url)
            or (bool(result.get("success")) and isinstance(access_tokens, list) and bool(access_tokens)),
            "redirect_url": redirect_url,
        }

    def _follow_authorization_redirect(
        self,
        session: Any,
        redirect_url: str,
        deadline: float,
    ) -> tuple[str, str, str]:
        current = redirect_url
        latest_html = ""
        for _ in range(8):
            if not (_allowed_payment_url(current) or _is_verify_url(current)):
                raise ProtocolError("GCash 授权回跳到不允许的域名", retryable=False)
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise AuthorizationTimeout(self.authorization_timeout)
            response = session.get(
                current,
                headers={"User-Agent": _browser_user_agent(session), "Accept": "text/html,*/*;q=0.8"},
                timeout=max(1, min(self.request_timeout, remaining)),
                allow_redirects=False,
            )
            latest_html = str(getattr(response, "text", "") or "")
            status = int(getattr(response, "status_code", 0) or 0)
            location = str((getattr(response, "headers", {}) or {}).get("Location") or "").strip()
            if 300 <= status < 400 and location:
                current = urljoin(current, location)
                if _is_verify_url(current):
                    redirect_result = _redirect_result(current)
                    if redirect_result:
                        response = session.get(
                            current,
                            headers={"User-Agent": _browser_user_agent(session), "Accept": "text/html,*/*;q=0.8"},
                            timeout=max(1, min(self.request_timeout, remaining)),
                            allow_redirects=False,
                        )
                        latest_html = str(getattr(response, "text", "") or "")
                        return redirect_result, current, latest_html
                continue
            self._require_http(response, "跟随 GCash 授权回跳")
            if _is_verify_url(current):
                redirect_result = _redirect_result(current)
                if redirect_result:
                    return redirect_result, current, latest_html
            break
        raise ProtocolError("GCash 授权回跳未返回 redirectResult", retryable=False)

    def _continue(
        self,
        session: Any,
        token: str,
        context: BrowserContext,
        checkout_id: str,
        processor: str,
        verify_url: str,
        redirect_result: str,
    ) -> dict[str, Any]:
        route = "/backend-api/payments/checkout/custom_payment_method/continue"
        referer = verify_url or f"{CHATGPT_ORIGIN}/checkout/{processor}/{checkout_id}"
        response = session.post(
            CUSTOM_PAYMENT_CONTINUE_URL,
            json={
                "checkout_session_id": checkout_id,
                "action_result": {"redirectResult": redirect_result},
            },
            headers=_checkout_headers(token, context, route, referer),
            timeout=self.request_timeout,
        )
        return self._json_response(response, "提交 GCash 授权结果")

    def _sync_gcash_success_state(
        self,
        session: Any,
        token: str,
        context: BrowserContext,
        checkout_id: str,
        processor: str,
        verify_url: str,
    ) -> dict[str, Any]:
        """复刻源项目支付成功页的两个只读同步请求。

        目标工具不把新的 Token/Cookie 写回任务记录，但仍检查 success.data 和
        /api/auth/session，确保 continue 返回 success 后页面状态确实收敛。
        """
        processor = processor or "openai_ie"
        query = {
            "stripe_session_id": checkout_id,
            "plan_type": "plus",
            "processor_entity": processor,
        }
        success_page = f"{CHATGPT_ORIGIN}/payments/success?{urlencode(query)}"
        result = {
            "success_data_ok": False,
            "auth_session_ok": False,
            "plan_type": "",
            "token_rotated": False,
        }
        try:
            response = session.get(
                f"{CHATGPT_ORIGIN}/payments/success.data",
                params={**query, "_routes": "routes/payments.success"},
                headers={
                    "Accept": "*/*",
                    "Accept-Language": "en-PH,en;q=0.9",
                    "Referer": verify_url or success_page,
                    "User-Agent": context.user_agent,
                    **(
                        {"x-oai-is-client-observation": observation}
                        if (observation := _client_observation_from_session(
                            getattr(context, "_session", None)
                        )) else {}
                    ),
                },
                timeout=min(45, self.request_timeout),
            )
            body = str(getattr(response, "text", "") or "")
            result["success_data_ok"] = (
                int(getattr(response, "status_code", 0) or 0) == 200
                and "postCheckoutResult" in body
                and "success" in body.lower()
            )
            if not result["success_data_ok"]:
                result["error"] = "payments/success.data 未确认 success"
                return result
        except Exception as exc:
            result["error"] = _safe_error_text(str(exc))
            return result

        def read_auth_session(params: dict[str, str], referer: str) -> dict[str, Any]:
            try:
                route = "/api/auth/session"
                response = session.get(
                    f"{CHATGPT_ORIGIN}{route}",
                    params=params,
                    headers={
                        "Accept": "*/*",
                        "Accept-Language": "en-PH,en;q=0.9",
                        "Referer": referer,
                        "User-Agent": context.user_agent,
                        "x-openai-target-path": route,
                        "x-openai-target-route": route,
                        **(
                            {"x-oai-is-client-observation": observation}
                            if (observation := _client_observation_from_session(
                                getattr(context, "_session", None)
                            )) else {}
                        ),
                    },
                    timeout=min(45, self.request_timeout),
                )
                if int(getattr(response, "status_code", 0) or 0) != 200:
                    return {"ok": False, "error": f"{route} HTTP {getattr(response, 'status_code', 0)}"}
                payload = response.json() or {}
                account = payload.get("account") if isinstance(payload, dict) else {}
                plan_type = ""
                if isinstance(account, dict):
                    plan_type = str(
                        account.get("planType") or account.get("plan_type") or ""
                    ).lower()
                return {
                    "ok": True,
                    "plan_type": plan_type,
                    "access_token": str(payload.get("accessToken") or "") if isinstance(payload, dict) else "",
                }
            except Exception as exc:
                return {"ok": False, "error": _safe_error_text(str(exc))}

        first = read_auth_session(
            {
                "workspace_update": "true",
                "reason": "checkout_success",
                "path": "/payments/success",
            },
            success_page,
        )
        latest = first
        # 源项目观察到第一次会话偶尔仍返回 free，页面随后通过 refresh
        # 分支获取 plus 和新的 accessToken；保持同一 Session/Cookie。
        if first.get("ok") and first.get("plan_type") != "plus":
            latest = read_auth_session(
                {
                    "refresh": "true",
                    "reason": "token_expired",
                    "method": "GET",
                    "path": "/backend-api/images/bootstrap",
                },
                f"{CHATGPT_ORIGIN}/",
            )
        result.update({
            "auth_session_ok": bool(latest.get("ok")),
            "plan_type": latest.get("plan_type") or first.get("plan_type") or "",
            "token_rotated": bool(
                latest.get("access_token")
                and latest.get("access_token") != token
            ),
        })
        if not result["auth_session_ok"]:
            result["error"] = str(latest.get("error") or "api/auth/session 未确认")
        return result

    def _mpaas_post(
        self,
        session: Any,
        operation: str,
        body: dict[str, Any],
        authorization_url: str,
    ) -> Any:
        parsed = _require_gcash_authorization_url(authorization_url)
        return session.post(
            GCASH_MPAAS_URL,
            data={
                "operationType": operation,
                "requestData": json.dumps([body], ensure_ascii=False, separators=(",", ":")),
                "version": "2.0",
                "workspaceId": GCASH_WORKSPACE_ID,
                "appId": GCASH_APP_ID,
                "tenantId": GCASH_TENANT_ID,
            },
            headers={
                "User-Agent": _browser_user_agent(session),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-PH,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://m.gcash.com",
                "Referer": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")),
                "sessionType": "APLUS",
                "sessionId": _cookie_value(session, "apsessionId"),
                f"X-CORS-{GCASH_APP_ID}-{GCASH_WORKSPACE_ID}": "",
            },
            timeout=min(30, self.request_timeout),
        )

    def _json_response(self, response: Any, operation: str) -> dict[str, Any]:
        self._require_http(response, operation)
        try:
            payload = response.json()
        except Exception as exc:
            raise ProtocolError(f"{operation}返回非 JSON", retryable=False) from exc
        if not isinstance(payload, dict):
            raise ProtocolError(f"{operation}返回格式无效", retryable=False)
        return payload

    @staticmethod
    def _require_http(response: Any, operation: str) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status < 300:
            return
        detail = _known_error_detail(response)
        suffix = f"：{detail}" if detail else ""
        raise ProtocolError(
            f"{operation}失败：HTTP {status}{suffix}",
            retryable=status in _RETRYABLE_HTTP,
        )

    @staticmethod
    def _close(session: Any | None) -> None:
        if session is None:
            return
        try:
            session.close()
        except Exception:
            pass


def _report(
    callback: ProgressCallback,
    stage: str,
    progress: int,
    details: dict[str, Any] | None = None,
) -> None:
    if not callable(callback):
        return
    _check_cancelled(callback)
    if details:
        callback(stage, progress, details)
    else:
        callback(stage, progress)


def _check_cancelled(callback: ProgressCallback) -> None:
    checker = getattr(callback, "check_cancelled", None)
    if callable(checker):
        checker()


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)


def _first_text(payload: dict[str, Any], *names: str) -> str:
    for item in _walk_dicts(payload):
        for name in names:
            value = str(item.get(name) or "").strip()
            if value:
                return value
    return ""


def _jwt_account_id(token: str) -> str:
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        auth = payload.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
    except Exception:
        return ""


def _client_observation_from_session(session: Any | None) -> str:
    """从当前 Session 的 __Secure-oai-is Cookie 恢复浏览器观察标识。

    这是源项目浏览器请求中的短期身份头，只在本次协议 Session 内使用，
    不写入账号记录或任务结果。
    """
    if session is None:
        return ""
    existing = str(getattr(session, "_protocol_oai_is_client_observation", "") or "").strip()
    if existing:
        return existing
    try:
        raw = str((session.cookies.get_dict() or {}).get("__Secure-oai-is") or "").strip()
    except Exception:
        return ""
    parts = raw.split(".")
    if len(parts) < 4 or parts[0] != "ois1" or not parts[2]:
        return ""
    value = f"v1.r.p.{parts[2]}"
    try:
        setattr(session, "_protocol_oai_is_client_observation", value)
    except Exception:
        pass
    return value


def _checkout_headers(
    token: str,
    context: BrowserContext,
    route: str,
    referer: str,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Accept-Language": "en-PH,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": CHATGPT_ORIGIN,
        "Referer": referer,
        "User-Agent": context.user_agent,
        "oai-device-id": context.device_id,
        "oai-language": "en-US",
        "oai-session-id": context.session_id,
        "sec-ch-ua": context.sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-openai-target-path": route,
        "x-openai-target-route": route,
    }
    account_id = _jwt_account_id(token)
    if account_id:
        headers["chatgpt-account-id"] = account_id
    if context.client_build_number:
        headers["oai-client-build-number"] = context.client_build_number
    if context.client_version:
        headers["oai-client-version"] = context.client_version
    if context.attestation:
        headers["oai-web-deployment-attestation"] = context.attestation
    observation = _client_observation_from_session(getattr(context, "_session", None))
    if observation:
        headers["x-oai-is-client-observation"] = observation
    return headers


def _apply_page_context(
    context: BrowserContext,
    html: str,
    *,
    prefer_session_id: bool = False,
) -> None:
    patterns = {
        "client_build_number": r'data-seq="([^"]+)"',
        "client_version": r'data-build="([^"]+)"',
    }
    for field, pattern in patterns.items():
        match = re.search(pattern, html)
        if match:
            setattr(context, field, match.group(1))

    # Checkout/.data/verify 页面动态签发的值必须沿用到后续 taxes、confirm、
    # start 和 continue 请求；不要把抓包中的旧值固化到配置里。
    for key, field in (
        ("webDeploymentAttestation", "attestation"),
        ("sessionId", "session_id"),
    ):
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
            html,
        )
        if not match:
            continue
        value = match.group(1)
        try:
            value = json.loads(f'"{value}"')
        except Exception:
            pass
        value = str(value or "").strip()
        if value and (field != "session_id" or prefer_session_id or not context.session_id):
            setattr(context, field, value)
    sdk_match = re.search(r'(?:https://chatgpt\.com)?(/sentinel/[A-Za-z0-9_-]+/sdk\.js)', html)
    if sdk_match:
        context.sentinel_sdk_url = urljoin(CHATGPT_ORIGIN, sdk_match.group(1))
    elif not context.sentinel_sdk_url:
        context.sentinel_sdk_url = os.getenv("CHATGPT_SENTINEL_SDK_URL", "").strip()


def _apply_page_context_from_html(session: Any, html: str, *, verify: bool = False) -> None:
    """把 verify 页面返回的短期字段暂存在当前 Session，不落盘。"""
    text = str(html or "")
    for key, attr in (
        ("webDeploymentAttestation", "_protocol_oai_web_deployment_attestation"),
        ("sessionId", "_protocol_oai_session_id"),
    ):
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
            text,
        )
        if not match:
            continue
        value = match.group(1)
        try:
            value = json.loads(f'"{value}"')
        except Exception:
            pass
        if value:
            try:
                setattr(session, attr, str(value))
                if verify and key == "sessionId":
                    setattr(session, "_protocol_oai_verify_session_id", str(value))
            except Exception:
                pass


def _copy_chatgpt_cookies(source: Any, target: Any) -> None:
    try:
        cookies = source.cookies.get_dict()
    except Exception:
        cookies = {}
    for name, value in (cookies or {}).items():
        for domain in (".chatgpt.com", "chatgpt.com"):
            try:
                target.cookies.set(name, value, domain=domain, path="/")
                break
            except Exception:
                continue


def _set_cookie(session: Any, name: str, value: str, domain: str) -> None:
    try:
        session.cookies.set(name, value, domain=domain, path="/")
    except Exception:
        pass


def _cookie_value(session: Any, name: str) -> str:
    try:
        return str((session.cookies.get_dict() or {}).get(name) or "").strip()
    except Exception:
        return ""


def _billing_details(created: dict[str, Any], email: str) -> dict[str, Any]:
    state = created.get("checkout_state") or {}
    billing = state.get("billingAddress") or state.get("billing_address") or {}
    address = billing.get("address") or {}
    normalized = {
        key: str(address.get(key) or "").strip()
        for key in ("line1", "line2", "city", "state", "postal_code")
        if str(address.get(key) or "").strip()
    }
    normalized["country"] = "PH"
    if not normalized.get("line1"):
        # 与源项目 GCash fallback 保持一致；只有创建响应没有账单档案时才使用。
        normalized.update({
            "line1": "1000 Roxas Boulevard",
            "city": "Manila",
            "state": "Metro Manila",
            "postal_code": "1000",
        })
    return {
        "email": str(state.get("email") or email),
        "name": str(billing.get("name") or "GCash Account Owner"),
        "address": normalized,
    }


# OAICS Checkout 的应付金额可能被包在 checkout_session/session/data/result
# 等多层对象中。金额对象的 minorUnitsAmount 才是源项目使用的最小货币单位；
# 不读取 unit price，避免把原价误判成折后应付金额。
_OAICS_WRAPPER_KEYS = (
    "checkout_session", "checkoutSession", "session", "checkout", "data",
    "result", "payload", "response", "checkout_state", "checkoutState",
    "checkout_snapshot", "checkoutSnapshot",
)
_OAICS_AMOUNT_PATHS = (
    ("checkout_amount_minor",),
    ("total_summary", "due"), ("totalSummary", "due"),
    ("invoice", "amount_due"), ("invoice", "amountDue"),
    ("amount_due",), ("amountDue",), ("amount_total",), ("amountTotal",),
    ("total", "total"), ("total", "due"),
    ("total", "taxInclusive"), ("total", "taxInclusiveAmount"),
)


def _nested_value(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _oaics_money_minor(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("minorUnitsAmount", "minor_units_amount", "amount"):
            if value.get(key) is not None:
                return _oaics_money_minor(value.get(key))
        return None
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def oaics_amount_observations(payload: Any) -> list[tuple[str, int]]:
    """从 OAICS 响应中提取应付金额证据，保留字段路径便于诊断。"""
    observations: list[tuple[str, int]] = []
    visited: set[int] = set()

    def visit(value: Any, prefix: str = "") -> None:
        if not isinstance(value, dict) or id(value) in visited:
            return
        visited.add(id(value))
        for path in _OAICS_AMOUNT_PATHS:
            amount = _oaics_money_minor(_nested_value(value, path))
            if amount is not None:
                observations.append((f"{prefix}{'.'.join(path)}", amount))
        for key in _OAICS_WRAPPER_KEYS:
            nested = value.get(key)
            if isinstance(nested, dict):
                visit(nested, f"{prefix}{key}.")

    visit(payload or {})
    return list(dict.fromkeys(observations))


def _due_amount(payload: dict[str, Any]) -> int | None:
    """返回 OAICS 应付金额最小单位；兼容目标项目旧 amount_total fixture。"""
    observations = oaics_amount_observations(payload)
    if not observations:
        return None
    priority = ("total.total", "total_summary.due", "invoice.amount_due", "amount_due")
    for label, amount in observations:
        if label.endswith(priority[0]) or label.endswith(priority[1]):
            return amount
    return observations[0][1]


# 源项目公开的命名，便于后续协议步骤和测试直接复用同一金额口径。
oaics_due_amount = _due_amount


def _gcash_custom_method(payload: dict[str, Any]) -> str:
    for item in _walk_dicts(payload):
        for key in ("custom_payment_methods", "customPaymentMethods"):
            methods = item.get(key)
            if not isinstance(methods, list):
                continue
            for method in methods:
                if not isinstance(method, dict):
                    continue
                method_id = str(method.get("id") or "").strip()
                if method_id.startswith("cpmt_") and "gcash" in json.dumps(method).lower():
                    return method_id
    return ""


def _require_gcash_authorization_url(url: str):
    parsed = urlsplit(str(url or "").strip())
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower().rstrip(".") != "m.gcash.com"
        or not parsed.path.startswith("/gcashapp/gcash-merchants-auth/")
    ):
        raise ProtocolError("GCash 授权地址不正确", retryable=False)
    return parsed


def _allowed_payment_url(url: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and (
        host == "gcash.com"
        or host.endswith(".gcash.com")
        or host == "adyen.com"
        or host.endswith(".adyen.com")
    )


def _is_verify_url(url: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower().rstrip(".") == "chatgpt.com"
        and parsed.path == "/checkout/verify"
    )


def _redirect_result(url: str) -> str:
    values = parse_qs(urlsplit(url).query).get("redirectResult") or []
    return str(values[0] if values else "").strip()


def _first_query(query: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = query.get(name) or []
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return ""


def _render_qr_png(value: str) -> bytes:
    if not value or len(value) > 4096:
        raise ProtocolError("GCash 二维码内容无效", retryable=False)
    try:
        import qrcode
    except ImportError as exc:
        raise ProtocolError(
            "缺少 qrcode/Pillow，请重新运行 start.bat 安装依赖",
            retryable=False,
        ) from exc
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    payload = output.getvalue()
    if not payload.startswith(_PNG_SIGNATURE) or len(payload) > 512 * 1024:
        raise ProtocolError("GCash 二维码 PNG 生成失败", retryable=False)
    return payload


def _qr_from_data_uri(html: str) -> bytes:
    match = re.search(r"data:image/png;base64,([A-Za-z0-9+/=\s]+)", html or "")
    if not match:
        return b""
    try:
        payload = base64.b64decode(re.sub(r"\s+", "", match.group(1)), validate=True)
    except ValueError:
        return b""
    if not payload.startswith(_PNG_SIGNATURE) or len(payload) > 512 * 1024:
        return b""
    return payload


def _known_error_detail(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    for item in _walk_dicts(payload):
        for key in ("code", "message", "error"):
            value = item.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return _safe_error_text(str(value), limit=160)
    return ""


def _safe_error_text(value: str, *, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@",
        r"\1***@",
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])",
        "<redacted-token>",
        text,
    )
    return text[:limit]


_DEFAULT_PROTOCOL = GCashProtocol()


def run_gcash_protocol(
    access_token: str,
    billing_proxy: str,
    promotion_proxy: str,
    progress: ProgressCallback,
) -> dict[str, Any]:
    """BatchExecutor 使用的默认真实协议入口。"""

    return _DEFAULT_PROTOCOL.run(access_token, billing_proxy, promotion_proxy, progress)
