"""Web server: FastAPI REST + Socket.IO, speaking the TwitchDropsMiner web UI contract."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote

import socketio
from curl_cffi.requests import AsyncSession
from fastapi import FastAPI, HTTPException
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictBool

from . import config
from .auth import AuthAPI, AuthMiddleware, AuthSocketServer, WebAuth
from .i18n import _
from .kick import AuthError, KickError, parse_cookie_input
from .miner import Miner
from .notify import discord_send, discord_valid, telegram_send

log = logging.getLogger("kdm.web")
MASK = "••••••••"

web_auth = WebAuth(config.AUTH_FILE, public_base_url=config.PUBLIC_BASE_URL)
sio = AuthSocketServer(web_auth)
miner: Miner | None = None


async def _emit(event, data):
    try:
        await sio.emit(event, data)
    except Exception:
        log.debug("emit %s failed", event, exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global miner
    miner = Miner(_emit)
    miner.start()
    miner.log("info", f"Kick Drops Miner {config.__version__}")
    yield
    await miner.shutdown()


app = FastAPI(title="Kick Drops Miner", version=config.__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(AuthAPI(web_auth, sio).router)
socket_app = AuthMiddleware(socketio.ASGIApp(sio, app), web_auth)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if request.url.path.startswith(("/api/auth/", "/api/settings")):
        return JSONResponse({"detail": "invalid_request"}, status_code=422)
    return await request_validation_exception_handler(request, exc)


def M() -> Miner:
    if miner is None:
        raise HTTPException(503, "not_initialized")
    return miner


# ============================================================================ settings
def get_settings() -> dict:
    m = M()
    s = m.settings.as_dict()
    s["games_available"] = sorted({c["game"] for c in m.campaigns if c["status"] != "expired"}, key=str.lower)
    s["discord_configured"] = bool(s["discord_webhook_url"])
    s["discord_webhook_url"] = MASK if s["discord_configured"] else ""
    s["telegram_configured"] = bool(s["telegram_bot_token"])
    s["telegram_bot_token"] = MASK if s["telegram_configured"] else ""
    return s


def update_settings(data: dict) -> dict:
    m = M()
    st = m.settings
    trigger = False

    def change(key, value, retrigger=False, show=None):
        nonlocal trigger
        if value is None or getattr(st, key) == value:
            return
        setattr(st, key, value)
        m.log("info", f"Setting changed: {key} = {show if show is not None else value}")
        trigger |= retrigger

    if "games_to_watch" in data and data["games_to_watch"] is not None:
        change("games_to_watch", [str(g) for g in data["games_to_watch"]], True)
    if data.get("drop_name_blacklist") is not None:
        change("drop_name_blacklist", config.normalize_keywords(data["drop_name_blacklist"]), True)
    change("dark_mode", data.get("dark_mode"))
    change("inventory_list_view", data.get("inventory_list_view"))
    change("connection_quality", data.get("connection_quality"))
    if data.get("minimum_refresh_interval_minutes") is not None:
        change("minimum_refresh_interval_minutes", max(1, min(1440, int(data["minimum_refresh_interval_minutes"]))))
    if data.get("language") and data["language"] != st.language:
        _.set_language(data["language"])            # raises ValueError for unknown
        change("language", data["language"])
        asyncio.create_task(sio.emit("language_changed", {"language": data["language"]}))
    if "proxy" in data:
        proxy = str(data["proxy"] or "").strip()
        if proxy != st.proxy:
            change("proxy", proxy, True)
            m.client.set_proxy(proxy)
    if data.get("mining_hours") is not None:
        change("mining_hours", config.normalize_hours(data["mining_hours"]), True,
               show=(lambda h: f"{h['start']}-{h['end']}" if h["mode"] == "range" else "always")(
                   config.normalize_hours(data["mining_hours"])))
    if isinstance(data.get("allow_unlinked_campaigns"), bool):
        change("allow_unlinked_campaigns", data["allow_unlinked_campaigns"], True)
    if data.get("mining_benefits") is not None:
        change("mining_benefits", {k: bool(v) for k, v in data["mining_benefits"].items()}, True)
    if data.get("inventory_filters") is not None:
        f = {**st.inventory_filters, **{k: v for k, v in data["inventory_filters"].items()
                                        if k in config.DEFAULT_FILTERS}}
        if f.get("link_status") not in config.LINK_STATUSES:
            raise ValueError("link_status")
        change("inventory_filters", f)
    if "telegram_bot_token" in data:
        tok = str(data["telegram_bot_token"] or "").strip()
        if tok and tok != MASK:
            change("telegram_bot_token", tok, show=MASK)
    if "telegram_chat_id" in data:
        change("telegram_chat_id", str(data["telegram_chat_id"] or "").strip())
    if data.get("discord_remove_webhook") is True:
        change("discord_webhook_url", "", show='""')
    elif "discord_webhook_url" in data:
        url = str(data["discord_webhook_url"] or "").strip()
        if url and url != MASK:
            if not discord_valid(url):
                raise ValueError("invalid_discord_webhook")
            change("discord_webhook_url", url, show=MASK)
    st.save()
    out = get_settings()
    asyncio.create_task(sio.emit("settings_updated", out))
    if trigger:
        m._merge()
        m.wake(reselect=True)
    return out


class SettingsUpdate(BaseModel):
    allow_unlinked_campaigns: StrictBool | None = None
    games_to_watch: list[str] | None = None
    drop_name_blacklist: list[str] | None = None
    dark_mode: bool | None = None
    language: str | None = None
    proxy: str | None = None
    connection_quality: int | None = None
    minimum_refresh_interval_minutes: int | None = None
    discord_webhook_url: str | None = None
    discord_remove_webhook: StrictBool | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    inventory_filters: dict | None = None
    inventory_list_view: bool | None = None
    mining_benefits: dict[str, bool] | None = None
    mining_hours: dict | None = None


# ============================================================================ pages
def _page(name: str) -> HTMLResponse:
    html = (config.WEB_DIR / name).read_text("utf-8").replace("__APP_VERSION__", config.__version__)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
async def index():
    return _page("index.html")


# ============================================================================ REST
@app.get("/api/status")
async def api_status():
    m = M()
    return {"status": m.status, "login": m.login_status(), "manual_mode": m.manual_info()}


@app.get("/api/channels")
async def api_channels():
    return {"channels": list(M().channels.values())}


class ChannelSelect(BaseModel):
    channel_id: int


@app.post("/api/channels/select")
async def select_channel(req: ChannelSelect):
    m = M()
    slug = m.channel_slug(req.channel_id)
    if not slug:
        raise HTTPException(404, "Channel not found")
    m.set_manual(channel=slug)
    return {"success": True}


class CampaignSelect(BaseModel):
    campaign_id: str


@app.post("/api/mode/manual-campaign")
async def manual_campaign(req: CampaignSelect):
    m = M()
    if not m.campaign(req.campaign_id):
        raise HTTPException(404, "Campaign not found")
    m.set_manual(campaign_id=req.campaign_id)
    return {"success": True}


@app.post("/api/mode/exit-manual")
async def exit_manual():
    m = M()
    if not (m.manual_campaign or m.manual_channel):
        return {"success": False, "message": "Not in manual mode"}
    m.set_manual()
    return {"success": True}


@app.get("/api/campaigns")
async def api_campaigns():
    return {"campaigns": M().inventory()}


@app.get("/api/console")
async def api_console():
    return {"lines": list(M().console)}


@app.get("/api/settings")
async def api_get_settings():
    return get_settings()


@app.post("/api/settings")
async def api_update_settings(body: SettingsUpdate):
    try:
        return {"success": True, "settings": update_settings(body.model_dump(exclude_unset=True))}
    except ValueError:
        raise HTTPException(422, "invalid_settings") from None


@app.get("/api/languages")
async def api_languages():
    return {"available": _.get_languages(), "current": _.current_language}


@app.get("/api/translations")
async def api_translations():
    return _.t


class ProxyVerify(BaseModel):
    proxy: str


@app.post("/api/settings/verify-proxy")
async def verify_proxy(req: ProxyVerify):
    proxy = req.proxy.strip()
    if not proxy:
        return {"success": False, "message": "Proxy URL is empty"}
    try:
        start = time.time()
        async with AsyncSession(impersonate="chrome", proxy=proxy, timeout=10) as s:
            r = await s.get("https://kick.com/")
        if r.status_code < 500:
            ms = round((time.time() - start) * 1000)
            return {"success": True, "message": f"Connected! ({ms}ms)", "latency": ms}
        return {"success": False, "message": f"Proxy reachable but returned {r.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {e}"}


class DiscordTest(BaseModel):
    discord_webhook_url: str = ""


@app.post("/api/settings/test-discord")
async def test_discord(req: DiscordTest):
    url = req.discord_webhook_url.strip()
    if not url or url == MASK:
        url = M().settings.discord_webhook_url
    if not discord_valid(url):
        return {"success": False, "error": "invalid_discord_webhook"}
    ok, _err = await discord_send(url, "✅ Kick Drops Miner – test notification")
    return {"success": ok} if ok else {"success": False, "error": "discord_connection_failed"}


class TelegramTest(BaseModel):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@app.post("/api/settings/test-telegram")
async def test_telegram(req: TelegramTest):
    st = M().settings
    token = req.telegram_bot_token.strip()
    if not token or token == MASK:
        token = st.telegram_bot_token
    chat = req.telegram_chat_id.strip() or st.telegram_chat_id
    if not token or not chat:
        return {"success": False, "message": "Bot token and chat ID are required"}
    ok, err = await telegram_send(token, chat, "✅ Kick Drops Miner – test notification")
    return {"success": ok, "message": "✓ Telegram connection successful!" if ok else f"Telegram error: {err}"}


@app.get("/api/version")
async def api_version():
    return {"current_version": config.__version__, "latest_version": None,
            "update_available": False, "download_url": None}


@app.post("/api/reload")
async def api_reload():
    M().wake(campaigns=True, progress=True, reselect=True)
    return {"success": True}


@app.post("/api/cache/clear")
async def api_cache_clear():
    m = M()
    m.raw_campaigns, m.raw_progress, m.cand_map, m.claim_cooldown = [], [], {}, {}
    m.link_warned.clear()
    m.bad.clear()
    m._merge()
    m.log("info", "Cache cleared")
    m.wake(campaigns=True, progress=True, reselect=True)
    return {"success": True}


@app.post("/api/diagnostics")
async def api_diagnostics(body: dict):
    m = M()
    out = config.DATA_DIR / "diagnostics"
    out.mkdir(exist_ok=True)
    name = f"diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    snap = {
        "version": config.__version__, "browser": body.get("browser"), "status": m.status,
        "logged_in": bool(m.user), "manual": m.manual_info(), "wanted": m.wanted,
        "current": {k: v for k, v in (m.current or {}).items() if k != "vod_id"},
        "watcher": {"connected": m.watcher.connected, "events": m.watcher.events_sent,
                    "error": m.watcher.error, "interval": m.watcher.interval},
        "campaigns": m.inventory(), "channels": list(m.channels.values()),
        "console": list(m.console)[-200:],
    }
    config.save_json(out / name, snap)
    return {"success": True, "file": f"diagnostics/{name}"}


# ---------------------------------------------------------------------------- history
def _since(v: str | None):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v)
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except ValueError:
        return None


@app.get("/api/history")
async def api_history(game: str | None = None, since: str | None = None, limit: int | None = None):
    h = M().history
    return {"total": h.total_count,
            "entries": h.get_entries(game or None, _since(since), min(limit, 5000) if limit else None)}


@app.get("/api/history/export.csv")
async def api_history_csv(game: str | None = None, since: str | None = None):
    name = f"drop_history_{game}.csv" if game else "drop_history.csv"
    return PlainTextResponse("﻿" + M().history.to_csv(game or None, _since(since)),
                             media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="drop_history.csv"; '
                                                            f"filename*=UTF-8''{quote(name, safe='')}"})


@app.get("/api/history/stats")
async def api_history_stats():
    h = M().history
    return {"total_drops": h.total_count, "by_game": h.stats_by_game(), "by_month": h.stats_by_month()}


@app.delete("/api/history")
async def api_history_clear():
    M().history.clear()
    return {"success": True}


# ---------------------------------------------------------------------------- Kick session
@app.get("/api/session")
async def api_session():
    return M().session_status()


class CookieLogin(BaseModel):
    cookies: str


@app.post("/api/session/login")
async def api_session_login(req: CookieLogin):
    cookies = parse_cookie_input(req.cookies)
    if "session_token" not in cookies:
        raise HTTPException(400, "no_session_token")
    try:
        user = await M().login(cookies)
    except AuthError:
        raise HTTPException(400, "invalid_session") from None  # 401 would make auth.js redirect
    except KickError as e:
        raise HTTPException(502, f"Kick API: {e}") from None
    return {"success": True, "username": user.get("username")}


@app.post("/api/session/logout")
async def api_session_logout():
    await M().logout()
    return {"success": True}


class ClaimRequest(BaseModel):
    campaign_id: str
    reward_id: str


@app.post("/api/claim")
async def api_claim(req: ClaimRequest):
    m = M()
    c = m.campaign(req.campaign_id)
    r = next((r for r in (c or {}).get("rewards", []) if r["id"] == req.reward_id), None)
    if not c or not r:
        raise HTTPException(404, "not_found")
    m.claim_cooldown.pop(r["id"], None)
    ok = await m.claim(c, r)
    m.wake(progress=True)
    return {"success": ok}


# ============================================================================ Socket.IO
@sio.event
async def connect(sid, environ):
    if not sio.register(sid, environ["asgi.scope"]):
        return False
    if miner:
        await sio.emit("initial_state", miner.initial_state(get_settings()), room=sid)
        await sio.emit("session_status", miner.session_status(), room=sid)


@sio.event
async def disconnect(sid):
    sio.forget(sid)


@sio.event
async def request_reload(sid):
    if await sio.authorize(sid) and miner:
        miner.wake(campaigns=True, progress=True, reselect=True)


@sio.event
async def get_wanted_items(sid):
    if await sio.authorize(sid) and miner:
        await sio.emit("wanted_items_update", miner.wanted_tree(), to=sid)


app.mount("/static", StaticFiles(directory=config.WEB_DIR / "static"), name="static")
