"""Cookie persistence and isolation by Google login session, not authUser index."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from threading import RLock

import httpx

from aistudio_api.infrastructure.utils.common import atomic_write_json
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("session_cookies")
_cookie_lock = RLock()


def load_cookies_from_auth_file(auth_file: str | None) -> dict[str, str]:
    if not auth_file:
        return {}
    try:
        data = json.loads(Path(auth_file).read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        if isinstance(cookies, list):
            return {
                str(c["name"]): str(c.get("value") or "")
                for c in cookies if isinstance(c, dict) and c.get("name")
            }
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items() if k}
    except (OSError, ValueError, AttributeError):
        logger.warning("Cannot read cookie file: %s", auth_file)
    return {}


def session_anchor(cookies: dict[str, str]) -> str:
    """Hash a stable session credential. An authUser value is never an identity."""
    for name in ("__Secure-1PSID", "__Secure-3PSID", "SID"):
        if cookies.get(name):
            return hashlib.sha256(f"{name}:{cookies[name]}".encode()).hexdigest()
    return ""


def session_files(auth_file: str) -> list[Path]:
    path = Path(auth_file)
    anchor = session_anchor(load_cookies_from_auth_file(auth_file))
    paths = [path]
    # Only account directories, never scan parents of a legacy standalone auth file.
    if anchor and path.name == "auth.json" and (path.parent / "meta.json").is_file():
        for candidate in path.parent.parent.glob("*/auth.json"):
            if candidate == path or candidate.is_symlink() or candidate.parent.is_symlink():
                continue
            if (candidate.parent / "meta.json").is_file() and session_anchor(
                load_cookies_from_auth_file(str(candidate))
            ) == anchor:
                paths.append(candidate)
    return paths


def apply_set_cookies(
    auth_file: str | None,
    headers: list[str],
    *,
    expected_anchor: str | None = None,
    expected_cookies: dict[str, str] | None = None,
) -> list[str]:
    """Atomically update same-session siblings, preserving each storage state."""
    if not auth_file or not headers:
        return []
    updates: dict[str, dict[str, object]] = {}
    for raw in headers:
        parsed: SimpleCookie[str] = SimpleCookie()
        try:
            parsed.load(raw)
            for name, morsel in parsed.items():
                domain = morsel["domain"].lstrip(".").lower()
                if domain and domain != "google.com" and not domain.endswith(".google.com"):
                    continue
                if not morsel.value or morsel.value.lower() == "deleted":
                    continue
                if morsel["max-age"] and int(morsel["max-age"]) <= 0:
                    continue
                if morsel["expires"]:
                    expires = parsedate_to_datetime(morsel["expires"])
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)
                    if expires <= datetime.now(UTC):
                        continue
                updates[name] = {
                    "name": name, "value": morsel.value,
                    "domain": morsel["domain"] or ".google.com",
                    "path": morsel["path"] or "/",
                    "expires": int(time.time()) + 86400 * 180,
                    "httpOnly": bool(morsel["httponly"]),
                    "secure": True, "sameSite": morsel["samesite"] or "None",
                }
        except (CookieError, ValueError, OverflowError, TypeError):
            continue
    if not updates:
        return []
    with _cookie_lock:
        current = load_cookies_from_auth_file(auth_file)
        anchor = session_anchor(current)
        if expected_anchor is not None and anchor != expected_anchor:
            logger.debug("Ignoring cookie response for a replaced login session")
            return []
        if expected_cookies is not None and current != expected_cookies:
            logger.debug("Ignoring stale cookie response after a newer update or import")
            return []
        changed: set[str] = set()
        paths = session_files(auth_file)
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cookies = data.get("cookies", [])
                dirty = False
                if isinstance(cookies, dict):
                    for name, update in updates.items():
                        if cookies.get(name) != update["value"]:
                            cookies[name] = update["value"]
                            changed.add(name)
                            dirty = True
                elif isinstance(cookies, list):
                    by_name = {str(c["name"]): c for c in cookies if isinstance(c, dict) and c.get("name")}
                    for name, update in updates.items():
                        previous = by_name.get(name)
                        if previous is None:
                            cookies.append(dict(update))
                            dirty = True
                            changed.add(name)
                        elif previous.get("value") != update["value"]:
                            previous["value"] = update["value"]
                            dirty = True
                            changed.add(name)
                else:
                    continue
                if dirty:
                    data["cookies"] = cookies
                    atomic_write_json(path, data)
            except (OSError, ValueError, AttributeError):
                logger.warning("Cannot update cookie file: %s", path)
        return sorted(changed)


async def probe_session(
    client: httpx.AsyncClient, cookies: dict[str, str], user_agent: str,
) -> bool | None:
    """Only an explicit signed-out redirect proves expiry; transient errors do not."""
    if not cookies:
        return None
    try:
        response = await client.get(
            "https://aistudio.google.com/",
            headers={"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()), "User-Agent": user_agent},
            follow_redirects=False, timeout=15.0,
        )
    except httpx.HTTPError:
        return None
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        target = httpx.URL("https://aistudio.google.com/").join(location)
        if target.host == "aistudio.google.com" and target.path in ("/welcome", "/welcome/"):
            return False
        if target.host == "accounts.google.com" and target.path.startswith(("/ServiceLogin", "/signin/")):
            return False
        return None
    return True if response.status_code == 200 else None
