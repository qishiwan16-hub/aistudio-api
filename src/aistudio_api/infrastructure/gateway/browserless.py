"""无浏览器引擎：远程 WAA 签名 + 直连 Google HTTP。

与浏览器引擎 (session.BrowserSession) 共用同一套账号与 Cookie 文件
(data/accounts/<id>/auth.json + meta.json)，对上层提供相同的会话接口。
通过 AISTUDIO_ENGINE=browserless 启用。

移植自上游 DeconstructedCube/aistudio-api 分支 feat/browserless-waa-engine
(GatewaySession / HTTPStreamTransport)，签名服务协议见 Lianues/aistudio-pg-bridge。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx

from aistudio_api.config import settings
from aistudio_api.domain.errors import BrowserTransportError
from aistudio_api.infrastructure.account.cookie_parser import (
    calculate_sapisid_hash,
    filter_minimal_cookies,
    get_tier_header_value,
    normalize_benefit_tier,
)
from aistudio_api.infrastructure.gateway.session_state import (
    SessionContext,
    SessionReadinessGate,
    SessionState,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.utils.common import atomic_write_json
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.waa.signer import WaaSigner

log = get_logger("browserless")

# 签名服务要求请求 Google 时 User-Agent 与其 VM 指纹一致
BROWSERLESS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
GENERATE_CONTENT_URL = (
    "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
    "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent"
)

# 浏览器模式下由页面 Hook 截获，这里用固定模板代替。
# body 只需提供 MODEL..SNAPSHOT 五个槽位，其余由 wire_codec 按请求填充。
DEFAULT_BOOTSTRAP_TEMPLATE: dict[str, object] = {
    "url": GENERATE_CONTENT_URL,
    "headers": {
        "Content-Type": "application/json+protobuf",
        "X-User-Agent": "grpc-web-javascript/0.1",
        "X-Goog-Api-Key": "AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs",
        "X-Goog-AuthUser": "0",
        "X-Goog-Ext-519733851-bin": "CAASAUIwATgEQABQBFgDYgJKUHAAeAGQAQCYAQE=",
        "Origin": "https://aistudio.google.com",
        "Referer": "https://aistudio.google.com/",
        "User-Agent": BROWSERLESS_USER_AGENT,
    },
    "body": '["models/gemini-3.7-flash",[],null,[],null]',
}


# ── auth.json / meta.json 读取（与浏览器模式同一套文件）──────────────────


def _read_meta(auth_file: str | None) -> dict[str, object]:
    if not auth_file:
        return {}
    try:
        meta_path = Path(auth_file).parent / "meta.json"
        if meta_path.exists():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def get_auth_user_from_meta(auth_file: str | None) -> str:
    return str(_read_meta(auth_file).get("auth_user") or "0")


def get_tier_from_meta(auth_file: str | None) -> str:
    return normalize_benefit_tier(str(_read_meta(auth_file).get("tier") or "Free"))


def load_cookies_from_auth_file(auth_file: str | None) -> dict[str, str]:
    if not auth_file or not Path(auth_file).is_file():
        return {}
    try:
        data = json.loads(Path(auth_file).read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if isinstance(cookies, list):
            return {
                str(c.get("name") or ""): str(c.get("value") or "")
                for c in cookies
                if isinstance(c, dict) and c.get("name")
            }
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items() if k}
    except Exception as e:
        log.warning("读取 Cookie 文件失败: %s, 错误=%s", auth_file, e)
    return {}


def _apply_set_cookies(auth_file: str | None, set_cookies: list[str]) -> list[str]:
    """把 Google 响应里的 Set-Cookie 写回 auth.json（两种模式共用的文件）。"""
    if not auth_file or not set_cookies or not Path(auth_file).is_file():
        return []
    updates: dict[str, str] = {}
    for raw in set_cookies:
        nv = raw.split(";", 1)[0].strip()
        name, sep, value = nv.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name or not value or value == "deleted":
            continue
        low = raw.lower()
        if "gstatic.com" in low or "expires=thu, 01 jan 1970" in low:
            continue
        updates[name] = value
    if not updates:
        return []
    try:
        path = Path(auth_file)
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if not isinstance(cookies, list):
            return []
        changed: list[str] = []
        for c in cookies:
            if isinstance(c, dict) and c.get("name") in updates:
                new_val = updates.pop(str(c["name"]))
                if c.get("value") != new_val:
                    c["value"] = new_val
                    changed.append(str(c["name"]))
        for name, value in updates.items():
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".google.com",
                    "path": "/",
                    "expires": int(time.time()) + 86400 * 180,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            )
            changed.append(name)
        if changed:
            data["cookies"] = cookies
            atomic_write_json(path, data)
        return changed
    except Exception as e:
        log.warning("回写 Set-Cookie 失败: %s, 错误=%s", auth_file, e)
        return []


# ── 传输层 ────────────────────────────────────────────────────────────


def _build_request_headers(
    headers: dict[str, str],
    *,
    auth_user: str,
    tier: str | None,
    auth_file: str | None,
) -> dict[str, str]:
    clean = {
        k: v
        for k, v in headers.items()
        if k.lower()
        not in (
            "host",
            "content-length",
            "authorization",
            "cookie",
            "x-goog-authuser",
            "x-aistudio-g1-tier",
            "user-agent",
        )
    }
    clean["X-Goog-AuthUser"] = str(auth_user or "0")
    clean["User-Agent"] = BROWSERLESS_USER_AGENT
    tier_header = get_tier_header_value(tier)
    if tier_header in ("TIER1", "TIER2"):
        clean["X-AIStudio-G1-Tier"] = tier_header

    cookies = filter_minimal_cookies(load_cookies_from_auth_file(auth_file))
    if not cookies:
        raise BrowserTransportError(f"无浏览器模式: 账号 Cookie 为空 ({auth_file})")
    auth = calculate_sapisid_hash(cookies)
    if auth:
        clean["Authorization"] = auth
    clean["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return clean


class HTTPStreamTransport:
    """直连 Google 的 HTTP 传输（非流式 + 流式）。"""

    def __init__(self, proxy_url: str | None = None) -> None:
        self.proxy_url = proxy_url
        self._client: httpx.AsyncClient | None = None
        self._active: set[str] = set()

    @property
    def in_flight_count(self) -> int:
        return len(self._active)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # 不设读超时：长输出流式生成可能持续数分钟
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=15.0),
                proxy=self.proxy_url,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def send_hooked_request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: str,
        auth_user: str = "0",
        tier: str | None = None,
        auth_file: str | None = None,
    ) -> tuple[int, bytes]:
        hdrs = _build_request_headers(
            headers, auth_user=auth_user, tier=tier, auth_file=auth_file
        )
        t0 = time.perf_counter()
        client = await self._get_client()
        try:
            resp = await client.post(url, headers=hdrs, content=body.encode("utf-8"))
        except Exception as exc:
            log.error("直连请求失败: auth_user=%s, 错误=%r", auth_user, exc)
            raise BrowserTransportError(f"HTTP request failed: {exc}") from exc
        elapsed = (time.perf_counter() - t0) * 1000
        log.info(
            "直连 Google 完成: 非流式, auth_user=%s, HTTP %d, 耗时=%.1fms, 响应=%d 字节",
            auth_user,
            resp.status_code,
            elapsed,
            len(resp.content),
        )
        self._sync_cookies(resp, auth_file)
        return resp.status_code, resp.content

    async def send_streaming_request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: str,
        auth_user: str = "0",
        tier: str | None = None,
        auth_file: str | None = None,
    ) -> AsyncGenerator[tuple[str, object], None]:
        hdrs = _build_request_headers(
            headers, auth_user=auth_user, tier=tier, auth_file=auth_file
        )
        rid = uuid.uuid4().hex[:8]
        self._active.add(rid)
        t0 = time.perf_counter()
        total = 0
        status = 0
        client = await self._get_client()
        try:
            req = client.build_request(
                "POST", url, headers=hdrs, content=body.encode("utf-8")
            )
            try:
                resp = await client.send(req, stream=True)
            except Exception as exc:
                log.error("直连流式请求失败: rid=%s, auth_user=%s, 错误=%r", rid, auth_user, exc)
                raise BrowserTransportError(f"streaming request failed: {exc}") from exc
            try:
                status = resp.status_code
                log.info(
                    "直连 Google 流式开始: rid=%s, auth_user=%s, HTTP %d, 首包耗时=%.1fms",
                    rid,
                    auth_user,
                    status,
                    (time.perf_counter() - t0) * 1000,
                )
                self._sync_cookies(resp, auth_file)
                yield ("status", status)
                if status != 200:
                    err_body = await resp.aread()
                    total = len(err_body)
                    yield ("chunk", err_body)
                    return
                try:
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            total += len(chunk)
                            yield ("chunk", chunk)
                except httpx.HTTPError as exc:
                    log.warning("直连流式中断: rid=%s, 已收=%d 字节, 错误=%r", rid, total, exc)
                    raise BrowserTransportError(
                        f"streaming request aborted: {exc}"
                    ) from exc
            finally:
                await resp.aclose()
        finally:
            self._active.discard(rid)
            log.info(
                "直连 Google 流式结束: rid=%s, HTTP %d, 共 %d 字节, 总耗时=%.1fms",
                rid,
                status,
                total,
                (time.perf_counter() - t0) * 1000,
            )

    @staticmethod
    def _sync_cookies(resp: httpx.Response, auth_file: str | None) -> None:
        set_cookies = resp.headers.get_list("set-cookie")
        if not set_cookies:
            return
        changed = _apply_set_cookies(auth_file, set_cookies)
        if changed:
            log.info("Google 下发新 Cookie 已写回账号文件: %s", ",".join(changed))


# ── 会话 ──────────────────────────────────────────────────────────────


class BrowserlessSession:
    """无浏览器会话。接口与 BrowserSession 对齐，供 AIStudioClient 使用。"""

    engine = "browserless"

    def __init__(self) -> None:
        self._auth_file: str | None = settings.auth_file or None
        self._gate = SessionReadinessGate()
        self._transition_lock = asyncio.Lock()
        self._transport = HTTPStreamTransport(proxy_url=settings.proxy_url)
        self._signer = WaaSigner(
            proof_server_url=settings.proof_server_url,
            proxy_url=settings.proxy_url,
        )
        self._in_flight = 0

    # 浏览器专属属性：无浏览器模式下恒为 None，便于上层按 page 判断
    @property
    def page(self) -> None:
        return None

    @property
    def state(self) -> SessionState:
        return self._gate.state

    @property
    def is_ready(self) -> bool:
        return self._gate.is_ready()

    @property
    def active_context(self) -> SessionContext | None:
        return self._gate.active_context

    def get_current_auth_user(self) -> str:
        ctx = self._gate.active_context
        return ctx.auth_user if ctx is not None else get_auth_user_from_meta(self._auth_file)

    def get_current_tier(self) -> str:
        ctx = self._gate.active_context
        if ctx is not None and ctx.tier:
            return ctx.tier
        return get_tier_from_meta(self._auth_file)

    async def extract_account_email(self) -> str | None:
        email = _read_meta(self._auth_file).get("email")
        return str(email).strip() if isinstance(email, str) and "@" in email else None

    async def extract_account_tier(self) -> str | None:
        from aistudio_api.infrastructure.account.cookie_parser import (
            probe_account_benefit_tier,
        )

        cookies = load_cookies_from_auth_file(self._auth_file)
        if not cookies:
            return None
        with suppress(Exception):
            return await probe_account_benefit_tier(
                cookies, auth_user=self.get_current_auth_user()
            )
        return None

    @asynccontextmanager
    async def request_scope(self):
        self._in_flight += 1
        try:
            yield self.active_context
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    async def ensure_ready(self) -> SessionContext:
        ctx = self._gate.active_context
        if self._gate.is_ready() and ctx is not None:
            return ctx
        if self._gate.state in (SessionState.UNINITIALIZED, SessionState.FAILED):
            return await self.transition_to_account(self._auth_file)
        return await self._gate.wait_ready()

    async def ensure_botguard_service(self) -> None:
        """浏览器模式的预热入口；无浏览器模式只需准备会话上下文。"""
        await self.ensure_ready()

    async def is_alive(self, timeout_s: float = 1.5) -> bool:
        return self._gate.is_ready()

    async def try_recover_page(self) -> bool:
        """无页面可恢复。签名客户端内部已重试，这里返回 False 让上层切换账号。"""
        return False

    async def switch_auth(self, auth_file: str | None) -> None:
        await self.transition_to_account(auth_file, force_refresh=True)

    async def transition_to_account(
        self, auth_file: str | None, force_refresh: bool = False
    ) -> SessionContext:
        async with self._transition_lock:
            ctx = self._gate.active_context
            if (
                not force_refresh
                and self._gate.is_ready()
                and ctx is not None
                and ctx.auth_file == auth_file
            ):
                return ctx
            self._gate.set_preparing()
            try:
                cookies = load_cookies_from_auth_file(auth_file)
                if not filter_minimal_cookies(cookies):
                    raise RuntimeError(f"账号 Cookie 为空或缺少鉴权项: {auth_file}")
                auth_user = get_auth_user_from_meta(auth_file)
                tier = get_tier_from_meta(auth_file)
                template = json.loads(json.dumps(DEFAULT_BOOTSTRAP_TEMPLATE))
                template["headers"]["X-Goog-AuthUser"] = auth_user
                new_ctx = SessionContext(
                    page=None,  # type: ignore[arg-type]
                    snap_key="waa_proof",
                    auth_user=auth_user,
                    auth_file=auth_file,
                    base_template=template,
                    tier=tier,
                )
                self._auth_file = auth_file
                self._gate.set_ready(new_ctx)
                log.info(
                    "[engine=browserless] 会话就绪: auth_file=%s, auth_user=%s, tier=%s, Cookie 数=%d",
                    auth_file,
                    auth_user,
                    tier,
                    len(cookies),
                )
                return new_ctx
            except Exception as exc:
                self._gate.set_failed(exc)
                log.error("[engine=browserless] 会话切换失败: %s, 错误=%r", auth_file, exc)
                raise

    async def capture_template(
        self, model: str, ctx: SessionContext | None = None
    ) -> dict[str, object]:
        active = ctx or await self.ensure_ready()
        tpl = json.loads(json.dumps(active.base_template or DEFAULT_BOOTSTRAP_TEMPLATE))
        normalized = model if model.startswith("models/") else f"models/{model}"
        with suppress(Exception):
            body = json.loads(str(tpl.get("body") or "[]"))
            if isinstance(body, list) and body:
                body[0] = normalized
                tpl["body"] = json.dumps(body, separators=(",", ":"))
        return tpl

    async def generate_snapshot(
        self,
        contents: list[AistudioContent],
        ctx: SessionContext | None = None,
        model: str = "models/gemini-3.7-flash",
    ) -> str:
        active = ctx or await self.ensure_ready()
        cookies = load_cookies_from_auth_file(active.auth_file)
        return await self._signer.generate_snapshot(
            contents=contents,
            cookies=cookies,
            auth_user=active.auth_user or "0",
            tier=active.tier or "Free",
            model=model,
        )

    def _resolve(
        self, ctx: object | None, url: str | None, headers: dict[str, str] | None
    ) -> tuple[SessionContext | None, str, dict[str, str]]:
        c = ctx if isinstance(ctx, SessionContext) else None
        tpl = (c.base_template if c else None) or DEFAULT_BOOTSTRAP_TEMPLATE
        hdrs = tpl.get("headers")
        return (
            c,
            url or str(tpl.get("url") or GENERATE_CONTENT_URL),
            headers
            or ({str(k): str(v) for k, v in hdrs.items()} if isinstance(hdrs, dict) else {}),
        )

    async def send_hooked_request(
        self,
        *,
        body: str,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        ctx: object | None = None,
    ) -> tuple[int, bytes]:
        c, u, h = self._resolve(ctx, url, headers)
        c = c or await self.ensure_ready()
        c.in_flight += 1
        try:
            return await self._transport.send_hooked_request(
                url=u,
                headers=h,
                body=body,
                auth_user=c.auth_user,
                tier=c.tier,
                auth_file=c.auth_file,
            )
        finally:
            c.decr_in_flight()

    async def send_streaming_request(
        self,
        *,
        body: str,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        ctx: object | None = None,
    ) -> AsyncGenerator[tuple[str, object], None]:
        c, u, h = self._resolve(ctx, url, headers)
        c = c or await self.ensure_ready()
        c.in_flight += 1
        try:
            async for event in self._transport.send_streaming_request(
                url=u,
                headers=h,
                body=body,
                auth_user=c.auth_user,
                tier=c.tier,
                auth_file=c.auth_file,
            ):
                yield event
        finally:
            c.decr_in_flight()

    async def import_cookies(
        self, cookie_string: str, auth_file: str | None = None
    ) -> int:
        """直接解析并写入 auth.json（不经过浏览器），格式与浏览器模式一致。"""
        from aistudio_api.infrastructure.account.cookie_parser import (
            parse_cookie_string,
        )

        state = parse_cookie_string(cookie_string, discard_transient=False)
        cookies = state.get("cookies")
        cookie_list = cookies if isinstance(cookies, list) else []
        target = auth_file or self._auth_file
        if target:
            atomic_write_json(Path(target), {"cookies": cookie_list, "origins": []})
            log.info("[engine=browserless] 已写入 Cookie: %d 个 -> %s", len(cookie_list), target)
            ctx = self._gate.active_context
            if ctx is not None and ctx.auth_file == target:
                await self.transition_to_account(target, force_refresh=True)
        return len(cookie_list)

    async def _close_internal(self) -> None:
        await self._transport.close()
        self._gate.reset()

    async def close(self) -> None:
        await self._transport.close()
        await self._signer.close()
        self._gate.reset()


__all__ = [
    "BROWSERLESS_USER_AGENT",
    "DEFAULT_BOOTSTRAP_TEMPLATE",
    "BrowserlessSession",
    "HTTPStreamTransport",
]
