"""Kick HTTP client (Cloudflare-friendly via curl_cffi browser impersonation)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from curl_cffi.requests import AsyncSession

log = logging.getLogger("kdm.kick")

# Constant client token used by Kick's own web client (see lurkloot / kickautodrops).
KICK_CLIENT_TOKEN = "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ASSET_BASES = ("https://ext.cdn.kick.com/", "https://ext.kick.com/")


class KickError(Exception):
    def __init__(self, msg: str, status: int = 0, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


class AuthError(KickError):
    pass


# --------------------------------------------------------------------------- cookies
def parse_cookie_input(text: str) -> dict[str, str]:
    """Accepts: bare session_token, Cookie header, JSON export, or Netscape cookies.txt."""
    text = (text or "").strip()
    if not text:
        return {}
    cookies: dict[str, str] = {}
    # JSON (EditThisCookie / Cookie-Editor array, or {"name": "value"} dict)
    if text[0] in "[{":
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("cookies", data)
            if isinstance(data, dict):
                cookies = {str(k): str(v) for k, v in data.items()}
            elif isinstance(data, list):
                for c in data:
                    if isinstance(c, dict) and c.get("name"):
                        dom = str(c.get("domain", "kick.com"))
                        if "kick.com" in dom:
                            cookies[c["name"]] = str(c.get("value", ""))
        except json.JSONDecodeError:
            pass
    # Netscape cookies.txt
    if not cookies and "\t" in text:
        for line in text.splitlines():
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            parts = line.strip().split("\t")
            if len(parts) >= 7 and "kick.com" in parts[0]:
                cookies[parts[5]] = parts[6]
    # Cookie header "a=b; c=d"
    if not cookies and "=" in text and ("session_token" in text or ";" in text):
        for part in text.removeprefix("Cookie:").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
    # Bare token
    if not cookies and "\n" not in text and " " not in text:
        cookies["session_token"] = text
    return cookies


def session_token_from(cookies: dict[str, str]) -> str:
    return unquote(cookies.get("session_token", "")).strip()


# --------------------------------------------------------------------------- helpers
def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError):
        return None


def asset_url(path: str | None) -> str:
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return ASSET_BASES[0] + path.lstrip("/")


def _list(data: Any, *keys: str) -> list:
    """Kick wraps lists inconsistently ({data:[...]}, {data:{campaigns:[...]}}, ...)."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    inner = data.get("data", data)
    if isinstance(inner, list):
        return inner
    if isinstance(inner, dict):
        out: list = []
        for k in keys:
            if isinstance(inner.get(k), list):
                out.extend(inner[k])
        return out
    return []


# --------------------------------------------------------------------------- client
class KickClient:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.proxy: str = ""
        self._session: AsyncSession | None = None

    @property
    def token(self) -> str:
        return session_token_from(self.cookies)

    def set_cookies(self, cookies: dict[str, str]) -> None:
        self.cookies = dict(cookies)

    def set_proxy(self, proxy: str) -> None:
        if proxy != self.proxy:
            self.proxy = proxy
            self._session = None

    def _sess(self) -> AsyncSession:
        if self._session is None:
            kw: dict[str, Any] = {"impersonate": "chrome", "timeout": 20}
            if self.proxy:
                kw["proxy"] = self.proxy
            self._session = AsyncSession(**kw)
        return self._session

    def _headers(self, auth: bool, extra: dict | None = None) -> dict:
        h = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://kick.com",
            "Referer": "https://kick.com/",
            "User-Agent": USER_AGENT,
        }
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
            h["X-Client-Token"] = KICK_CLIENT_TOKEN
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if extra:
            h.update(extra)
        return h

    async def request(self, method: str, url: str, *, auth: bool = True,
                      json_body: Any = None, headers: dict | None = None) -> Any:
        hdrs = self._headers(auth, headers)
        if json_body is not None:
            hdrs["Content-Type"] = "application/json"
        try:
            r = await self._sess().request(
                method, url, headers=hdrs,
                data=json.dumps(json_body) if json_body is not None else None,
            )
        except Exception as e:  # network / TLS
            raise KickError(f"{method} {url}: {e}") from e
        body = r.text or ""
        if r.status_code in (401, 403) and "security policy" not in body.lower() and auth:
            raise AuthError(f"Unauthorized ({r.status_code})", r.status_code, body[:300])
        if r.status_code >= 400:
            raise KickError(f"{method} {url} → HTTP {r.status_code}", r.status_code, body[:300])
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise KickError(f"Invalid JSON from {url} (Cloudflare?)", r.status_code, body[:200]) from e

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    # ---------------------------------------------------------------- endpoints
    async def get_user(self) -> dict:
        data = await self.request("GET", "https://kick.com/api/v1/user")
        if not isinstance(data, dict) or not (data.get("id") or data.get("username")):
            raise AuthError("Session token is invalid or expired")
        return data

    async def get_campaigns(self) -> list[dict]:
        data = await self.request("GET", "https://web.kick.com/api/v1/drops/campaigns", auth=False)
        return _list(data, "campaigns", "active", "current", "upcoming")

    async def get_progress(self) -> list[dict]:
        data = await self.request("GET", "https://web.kick.com/api/v1/drops/progress")
        return _list(data, "progress", "campaigns", "active", "current", "completed")

    async def get_livestreams(self, category_id: Any, limit: int = 24) -> list[dict]:
        url = (f"https://web.kick.com/api/v1/livestreams?limit={limit}"
               f"&sort=viewer_count_desc&category_id={category_id}")
        data = await self.request("GET", url, auth=False)
        return _list(data, "livestreams")

    async def get_channel(self, slug: str) -> dict:
        return await self.request("GET", f"https://kick.com/api/v2/channels/{slug}", auth=False)

    async def get_watch_interval(self) -> int:
        try:
            data = await self.request("GET", "https://kick.com/api/internal/settings/global", auth=False)
            sec = data["events"]["tracking.user.watch.livestream"]["interval"]
            return int(min(300, max(30, float(sec))))
        except Exception:
            return 120

    async def get_viewer_token(self) -> str:
        data = await self.request("GET", "https://websockets.kick.com/viewer/v1/token")
        token = (data.get("data") or {}).get("token") if isinstance(data, dict) else None
        if not token:
            raise AuthError("Kick returned no viewer token – sign in again")
        return token

    async def claim(self, campaign_id: str, reward_id: str, claim_id: str | None = None) -> Any:
        body: dict[str, Any] = {"campaign_id": campaign_id, "reward_id": reward_id}
        if claim_id:
            body["claim_id"] = claim_id
        return await self.request("POST", "https://web.kick.com/api/v1/drops/claim", json_body=body)
