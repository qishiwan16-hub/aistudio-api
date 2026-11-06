"""Rate-limited browserless cookie rotation and session expiry detection."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx

from aistudio_api.config import settings
from aistudio_api.infrastructure.account.session_cookies import (
    apply_set_cookies,
    load_cookies_from_auth_file,
    probe_session,
    session_anchor,
)
from aistudio_api.infrastructure.gateway.browserless import BROWSERLESS_USER_AGENT
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.application.account_rotator import AccountRotator
    from aistudio_api.infrastructure.account.account_store import AccountStore

logger = get_logger("cookie_maintenance")
_ROTATE_URL = "https://accounts.google.com/RotateCookies"
_ROTATION_CREDENTIALS = {"LSID", "__Host-1PLSID", "__Host-3PLSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS"}


class CookieMaintenance:
    def __init__(self, store: AccountStore, rotator: AccountRotator) -> None:
        self.store = store
        self.rotator = rotator
        self._last_attempt: dict[str, float] = {}

    def _expire(self, anchor: str) -> None:
        if not anchor:
            return
        for account in self.store.list_accounts():
            path = self.store.get_auth_path(account.id)
            if session_anchor(load_cookies_from_auth_file(str(path))) == anchor:
                self.rotator.record_session_expired(account.id)
                logger.warning("AI Studio session expired: account=%s; import fresh cookies", account.id)

    async def tick(self, client: httpx.AsyncClient) -> None:
        now = time.time()
        seen: set[str] = set()
        for account in self.store.list_accounts():
            stats = self.rotator.get_account_stats(account.id)
            if not account.enabled or (stats is not None and stats.session_expired):
                continue
            path = self.store.get_auth_path(account.id)
            cookies = load_cookies_from_auth_file(str(path))
            anchor = session_anchor(cookies)
            if not anchor or anchor in seen:
                continue
            seen.add(anchor)
            try:
                imported_at = path.stat().st_mtime
            except OSError:
                continue
            last = self._last_attempt.setdefault(anchor, imported_at)
            if now - last < settings.cookie_rotation_interval:
                continue
            self._last_attempt[anchor] = now
            alive = await probe_session(client, cookies, BROWSERLESS_USER_AGENT)
            if load_cookies_from_auth_file(str(path)) != cookies:
                continue  # Cookies were re-imported while the request was in flight.
            if alive is False:
                self._expire(anchor)
                continue
            if alive is None or not _ROTATION_CREDENTIALS.intersection(cookies):
                continue
            try:
                response = await client.post(
                    _ROTATE_URL,
                    headers={
                        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
                        "User-Agent": BROWSERLESS_USER_AGENT,
                        "Origin": "https://accounts.google.com",
                        "Referer": "https://accounts.google.com/RotateCookiesPage",
                    },
                    json=[None, "-1234567890", 0], follow_redirects=False,
                )
            except httpx.HTTPError:
                logger.debug("Cookie rotation network failure: account=%s", account.id)
                continue
            if load_cookies_from_auth_file(str(path)) != cookies:
                continue
            if response.status_code == 401:
                self._expire(anchor)
            elif response.status_code == 200:
                changed = apply_set_cookies(str(path), response.headers.get_list("set-cookie"), expected_anchor=anchor, expected_cookies=cookies)
                logger.debug("Cookie rotation complete: account=%s, updated=%s", account.id, changed)
            else:
                logger.debug("Cookie rotation skipped: account=%s, status=%d", account.id, response.status_code)
        self._last_attempt = {key: value for key, value in self._last_attempt.items() if key in seen}

    async def run(self) -> None:
        async with httpx.AsyncClient(proxy=settings.proxy_url, timeout=15.0) as client:
            while True:
                await asyncio.sleep(60)
                try:
                    await self.tick(client)
                except Exception:
                    logger.exception("Cookie maintenance failed; retry on next timer tick")
