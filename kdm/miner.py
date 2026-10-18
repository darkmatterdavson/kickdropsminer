"""Mining engine: campaigns → wanted queue → channel selection → watcher → auto-claim.

Publishes state through `emit(event, data)` using the TwitchDropsMiner web UI event contract.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import zlib
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import config
from .history import DropHistory
from .i18n import _
from .kick import AuthError, KickClient, KickError, asset_url, parse_dt
from .notify import discord_send, telegram_send
from .watcher import ViewerWatcher

log = logging.getLogger("kdm.miner")
TICK = 60
PROGRESS_REFRESH = 180          # Kick tracks watch time every 2 minutes
CHANNELS_REFRESH = 180
STALL_LIMIT = 600              # healthy watching without progress → channel can't be mined
BAD_CHANNEL_TTL = 1800
MAX_CATEGORIES = 8
INVENTORY_URL = "https://kick.com/drops/inventory"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _slug_of(obj: dict) -> str:
    ch = obj.get("channel") if isinstance(obj.get("channel"), dict) else {}
    user = obj.get("user") if isinstance(obj.get("user"), dict) else {}
    cu = ch.get("user") if isinstance(ch.get("user"), dict) else {}
    for v in (ch.get("slug"), obj.get("slug"), ch.get("username"), cu.get("username"),
              obj.get("username"), user.get("slug"), user.get("username"), obj.get("user_slug")):
        if v:
            return str(v).lower()
    return ""


def _stream_category(ls: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(ls, dict):
        return None, None
    cats = ls.get("categories")
    cat = cats[0] if isinstance(cats, list) and cats and isinstance(cats[0], dict) else ls.get("category")
    if isinstance(cat, dict) and cat.get("id") is not None:
        return str(cat["id"]), cat.get("name")
    return (str(ls["category_id"]) if ls.get("category_id") is not None else None), None


BENEFIT_TYPES = ("DIRECT_ENTITLEMENT", "BADGE", "EMOTE", "UNKNOWN")
_TYPE_ALIASES = {"item": "DIRECT_ENTITLEMENT", "game_item": "DIRECT_ENTITLEMENT", "in_game": "DIRECT_ENTITLEMENT",
                 "direct_entitlement": "DIRECT_ENTITLEMENT", "badge": "BADGE", "chat_badge": "BADGE",
                 "emote": "EMOTE", "emoji": "EMOTE"}
_BADGE_WORDS = ("badge", "flair")
_EMOTE_WORDS = ("emote", "emoji")
_OTHER_RE = re.compile(r"\b(subscriptions?|subs?|gift ?cards?|coupons?|discount|vouchers?|codes?|keys?|"
                       r"nitro|premium|membership|trial|kicks)\b", re.I)


def benefit_type(reward: dict, name: str) -> str:
    """Kick has no official reward type: use an API field if one appears, else guess from the name."""
    for k in ("type", "reward_type", "benefit_type", "kind", "category"):
        v = reward.get(k)
        if isinstance(v, str) and v.strip():
            t = _TYPE_ALIASES.get(v.strip().lower()) or (v.upper() if v.upper() in BENEFIT_TYPES else None)
            if t:
                return t
    n = f" {name.lower()} "
    if any(w in n for w in _BADGE_WORDS):
        return "BADGE"
    if any(w in n for w in _EMOTE_WORDS):
        return "EMOTE"
    if _OTHER_RE.search(n):
        return "UNKNOWN"
    return "DIRECT_ENTITLEMENT"


def _thumb(v) -> str:
    if isinstance(v, dict):
        v = v.get("src") or v.get("url")
    return v if isinstance(v, str) else ""


class Miner:
    def __init__(self, emit: Callable[[str, Any], Awaitable[None]]):
        self._emit = emit
        self.settings = config.Settings()
        try:
            _.set_language(self.settings.language)
        except ValueError:
            self.settings.language = _.current_language
        self.client = KickClient()
        self.client.set_proxy(self.settings.proxy)
        self.client.set_cookies(config.load_json(config.SESSION_FILE, {}).get("cookies", {}))
        self.watcher = ViewerWatcher(self.client, self.log)
        self.history = DropHistory()

        self.console: deque[str] = deque(maxlen=1000)
        self.user: dict | None = None
        self.login_state = "logged_out"          # logged_out | verifying | logged_in | error
        self.login_error: str | None = None
        self.status = _.t["gui"]["header"].get("initializing", "Initializing...")
        self.raw_campaigns: list[dict] = []
        self.raw_progress: list[dict] = []
        self.campaigns: list[dict] = []          # internal model
        self.wanted: list[str] = []
        self.channels: dict[int, dict] = {}       # id -> UI channel dict
        self.cand_map: dict[str, list[dict]] = {}
        self.current: dict | None = None
        self.manual_campaign: str | None = None
        self.manual_channel: str | None = None
        self.outside_hours = False

        self.campaigns_at = self.progress_at = self.user_at = self.channels_at = 0.0
        self.claim_cooldown: dict[str, float] = {}
        self.link_warned: set[str] = set()
        self.unlinked_logged: set[str] = set()
        self.bad: dict[str, float] = {}         # 'campaign|slug' -> until
        self.stall: dict | None = None
        self._wake = asyncio.Event()
        self._force_campaigns = self._force_progress = True
        self._force_reselect = self._force_channels = False
        self._sent: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ plumbing
    def emit(self, event: str, data: Any = None, dedupe: bool = False) -> None:
        if dedupe:
            key = json.dumps(data, sort_keys=True, default=str)
            if self._sent.get(event) == key:
                return
            self._sent[event] = key
        asyncio.create_task(self._emit(event, data if data is not None else {}))

    def log(self, level: str, msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] | {msg}"
        self.console.append(line)
        getattr(log, level if level in ("info", "warning", "error", "debug") else "info")(msg)
        self.emit("console_output", {"message": line})

    def set_status(self, s: str) -> None:
        if s != self.status:
            self.status = s
            self.emit("status_update", {"status": s})

    def wake(self, campaigns=False, progress=False, reselect=False) -> None:
        self._force_campaigns |= campaigns
        self._force_progress |= progress
        self._force_reselect |= reselect
        self._force_channels |= reselect
        self._wake.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="miner")

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
        await self.watcher.stop()
        await self.client.close()

    def gs(self, key: str, default: str = "") -> str:
        return _.t["gui"]["status"].get(key, default)

    # ------------------------------------------------------------------ session
    def login_status(self) -> dict:
        t = _.t["login"]["status"]
        d: dict[str, Any] = {
            "status": t["logged_in"] if self.user else t["required"],
            "user_id": (self.user or {}).get("id"),
            "username": (self.user or {}).get("username"),
        }
        pic = (self.user or {}).get("profilepic") or (self.user or {}).get("profile_pic")
        if isinstance(pic, str) and pic.startswith("https://"):
            d["avatar_url"] = pic
        return d

    def session_status(self) -> dict:
        return {"logged_in": bool(self.user), "has_token": bool(self.client.token),
                "state": self.login_state, "error": self.login_error,
                "username": (self.user or {}).get("username")}

    def publish_login(self) -> None:
        self.emit("login_status", self.login_status())
        self.emit("session_status", self.session_status())

    def save_session(self) -> None:
        config.save_json(config.SESSION_FILE, {"cookies": self.client.cookies})

    async def login(self, cookies: dict[str, str]) -> dict:
        old = self.client.cookies
        self.client.set_cookies(cookies)
        self.login_state, self.login_error = "verifying", None
        self.publish_login()
        try:
            user = await self.client.get_user()
        except Exception as e:
            self.client.set_cookies(old)
            self.login_state, self.login_error = ("logged_in" if self.user else "error"), str(e)
            self.publish_login()
            raise
        self.user, self.user_at = user, time.time()
        self.login_state, self.login_error = "logged_in", None
        self.save_session()
        self.log("info", f"{_.t['login']['status']['logged_in']}: {user.get('username')}")
        self.publish_login()
        self.wake(campaigns=True, progress=True, reselect=True)
        return user

    async def logout(self) -> None:
        await self.stop_watching()
        self.client.set_cookies({})
        self.save_session()
        self.user, self.raw_progress = None, []
        self.login_state, self.login_error = "logged_out", None
        self.log("info", _.t["login"]["status"]["logged_out"])
        self.publish_login()
        self.wake(progress=True)

    # ------------------------------------------------------------------ manual mode
    def manual_info(self) -> dict:
        if self.manual_campaign:
            c = self.campaign(self.manual_campaign)
            return {"active": True, "game_name": c["game"] if c else "", "campaign": c["name"] if c else ""}
        if self.manual_channel:
            game = (self.current or {}).get("category") or self.manual_channel
            return {"active": True, "game_name": game, "channel": self.manual_channel}
        return {"active": False, "game_name": None}

    def set_manual(self, campaign_id: str | None = None, channel: str | None = None) -> None:
        self.manual_campaign, self.manual_channel = campaign_id, channel
        if campaign_id:
            c = self.campaign(campaign_id)
            self.log("info", f"Manual mode: {c['game']} – {c['name']}" if c else "Manual mode")
        elif channel:
            self.log("info", f"Manual mode: {channel}")
        else:
            self.log("info", "Automatic mode")
        self._merge()
        self.emit("manual_mode_update", self.manual_info())
        self.wake(reselect=True)

    def channel_slug(self, channel_id: int) -> str | None:
        ch = self.channels.get(channel_id)
        return ch["login"] if ch else None

    # ------------------------------------------------------------------ loop
    async def _loop(self) -> None:
        await asyncio.sleep(0.3)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except AuthError as e:
                self.user = None
                self.login_state, self.login_error = "error", str(e)
                await self.stop_watching()
                self.set_status(_.t["login"]["status"]["required"])
                self.log("error", f"Kick: {e}")
                self.publish_login()
            except KickError as e:
                self.log("warning", f"Kick API: {e} {e.body[:120] if e.body else ''}")
            except Exception as e:
                log.exception("tick failed")
                self.log("error", f"{type(e).__name__}: {e}")
            self.publish_progress()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass

    def _in_hours(self) -> bool:
        h = self.settings.mining_hours or {}
        if h.get("mode") != "range":
            return True
        now = datetime.now().strftime("%H:%M")
        start, end = h.get("start", "00:00"), h.get("end", "00:00")
        if start == end:
            return True
        return start <= now < end if start < end else (now >= start or now < end)

    async def tick(self) -> None:
        s = self.settings
        now = time.time()
        if self._force_campaigns or now - self.campaigns_at > s.minimum_refresh_interval_minutes * 60:
            await self._refresh_campaigns()

        if not self.client.token:
            await self.stop_watching()
            self.user = None
            self._merge()
            self.set_status(_.t["login"]["status"]["required"])
            return

        if self.user is None or now - self.user_at > 1800:
            self.user = await self.client.get_user()
            self.user_at = now
            self.login_state, self.login_error = "logged_in", None
            self.publish_login()

        if self._force_progress or now - self.progress_at > PROGRESS_REFRESH:
            self.set_status(self.gs("fetching_inventory", "Fetching inventory..."))
            self.raw_progress = await self.client.get_progress()
            self.progress_at = time.time()
            self._force_progress = False
        self._merge()
        await self._auto_claim()

        if not self._in_hours():
            if not self.outside_hours:
                self.log("info", self.gs("outside_hours", "Outside mining hours"))
            self.outside_hours = True
            await self.stop_watching()
            self.set_status(self.gs("outside_hours", "Outside mining hours"))
            return
        self.outside_hours = False
        self._check_stall()
        await self._select_and_watch()

    async def _refresh_campaigns(self) -> None:
        self.set_status(self.gs("fetching_campaigns", "Fetching campaigns..."))
        self.raw_campaigns = await self.client.get_campaigns()
        self.campaigns_at = time.time()
        self._force_campaigns = False
        self._force_channels = True
        self._merge()
        games = sorted({c["game"] for c in self.campaigns if c["status"] != "expired"}, key=str.lower)
        self.emit("games_available", {"games": games}, dedupe=True)

    # ------------------------------------------------------------------ model
    def _merge(self) -> None:
        s = self.settings
        prog_by_id: dict[str, dict] = {}
        for p in self.raw_progress:
            if isinstance(p, dict):
                cid = p.get("id") or p.get("campaign_id") or p.get("drop_campaign_id")
                if cid is not None:
                    prog_by_id[str(cid)] = p
        keywords = [k.lower() for k in s.drop_name_blacklist]
        now = _now()
        out, seen = [], set()
        active_ids = {str(c.get("id")) for c in self.raw_campaigns if isinstance(c, dict)}
        # campaigns known only from progress (ended / not listed anymore) → Expired/Finished filters
        progress_only = [p for cid, p in prog_by_id.items() if cid not in active_ids]
        for c in [*self.raw_campaigns, *progress_only]:
            if not isinstance(c, dict) or c.get("id") is None or str(c["id"]) in seen:
                continue
            listed = str(c["id"]) in active_ids
            cid = str(c["id"])
            seen.add(cid)
            cat = c.get("category") if isinstance(c.get("category"), dict) else {}
            p = prog_by_id.get(cid, {})
            if not cat and isinstance(p.get("category"), dict):
                cat = p["category"]
            starts, ends = parse_dt(c.get("starts_at")), parse_dt(c.get("ends_at"))
            status = "expired" if (ends and ends <= now) or not listed \
                else "upcoming" if starts and starts > now else "active"
            units = float(p.get("progress_units") or 0)
            prew = {}
            for r in p.get("rewards") or []:
                if isinstance(r, dict):
                    rid = r.get("id") or r.get("reward_id") or r.get("drop_id")
                    if rid is not None:
                        prew[str(rid)] = r
            rewards = []
            for r in c.get("rewards") or c.get("drops") or []:
                if not isinstance(r, dict) or r.get("id") is None:
                    continue
                rid = str(r["id"])
                pr = prew.get(rid, {})
                req = float(pr.get("required_units") or r.get("required_units")
                            or r.get("required_minutes") or 0)
                frac = pr.get("progress")
                if isinstance(frac, (int, float)):
                    frac = float(frac)
                    if frac > 1.0 and req > 0:
                        frac /= req
                else:
                    frac = units / req if req > 0 else 0.0
                frac = max(0.0, min(1.0, frac))
                claimed = bool(pr.get("claimed") or pr.get("is_claimed") or r.get("claimed")
                               or str(pr.get("status", "")).lower() == "claimed")
                name = str(r.get("name") or r.get("title") or "Reward")
                kw = next((k for k in keywords if k in name.lower()), None)
                rewards.append({
                    "id": rid, "name": name,
                    "image": asset_url(r.get("image_url") or r.get("image")),
                    "required_min": req, "watched_min": min(req, frac * req) if req else 0.0,
                    "progress": frac, "claimed": claimed, "claim_id": pr.get("claim_id"),
                    "ignored_keyword": kw, "btype": benefit_type(r, name),
                    "complete": frac >= 1.0 and not claimed,
                })
            rewards.sort(key=lambda r: r["required_min"])
            chans = []
            for ch in c.get("channels") or []:
                sl = _slug_of(ch) if isinstance(ch, dict) else str(ch).lower()
                if sl:
                    chans.append(sl)
            connected = p.get("user_app_connected") if p else None
            linked = connected if isinstance(connected, bool) else True
            mineable = [r for r in rewards if self.wanted_reward(r)]
            out.append({
                "id": cid,
                "name": str(c.get("name") or c.get("title") or f"Campaign {cid}"),
                "game": str(cat.get("name") or "Unknown"),
                "game_image": asset_url(cat.get("image_url") or cat.get("banner")),
                "category_id": str(cat.get("id") or c.get("category_id") or "") or None,
                "starts_at": _iso(starts), "ends_at": _iso(ends), "status": status,
                "connect_url": c.get("connect_url") or p.get("connect_url") or "",
                "info_url": c.get("url") or "",
                "linked": linked, "channels": chans, "general": not chans,
                "rewards": rewards, "progress_units": units, "mineable": len(mineable),
            })
        self.campaigns = out
        self.wanted = [c["id"] for c in self._wanted()]
        self.emit("inventory_batch_update", {"campaigns": [self._ser_campaign(c) for c in out]}, dedupe=True)
        self.emit("wanted_items_update", self.wanted_tree(), dedupe=True)

    def _wanted(self) -> list[dict]:
        s = self.settings
        prio = [g.lower() for g in s.games_to_watch]
        res = []
        for c in self.campaigns:
            if self.manual_campaign and c["id"] != self.manual_campaign:
                continue
            if c["status"] != "active" or not c["mineable"] or not c["category_id"]:
                continue
            if not self.manual_campaign:
                if c["game"].lower() not in prio:      # TDM behaviour: queue = Games to Watch only
                    continue
                if not c["linked"] and not s.allow_unlinked_campaigns:
                    if c["id"] not in self.unlinked_logged:
                        self.unlinked_logged.add(c["id"])
                        self.log("info", f"Skipping {c['game']} – {c['name']}: account not linked "
                                         "(link it on Kick or enable 'Allow mining campaigns reported as Not Linked')")
                    continue
            res.append(c)

        def key(c):
            g = c["game"].lower()
            return (prio.index(g) if g in prio else len(prio), c["ends_at"] or "9999",
                    0 if c["progress_units"] > 0 else 1)
        res.sort(key=key)
        return res

    def benefit_enabled(self, r: dict) -> bool:
        return self.settings.mining_benefits.get(r["btype"], True) is not False

    def wanted_reward(self, r: dict) -> bool:
        return (not r["claimed"] and not r["ignored_keyword"] and r["progress"] < 1
                and self.benefit_enabled(r))

    def campaign(self, cid: str | None) -> dict | None:
        return next((c for c in self.campaigns if c["id"] == cid), None)

    # ------------------------------------------------------------------ serializers
    def _ser_drop(self, c: dict, r: dict) -> dict:
        ignored = r["ignored_keyword"] is not None
        return {
            "id": r["id"], "name": r["name"],
            "current_minutes": int(r["watched_min"]), "required_minutes": int(r["required_min"]),
            "progress": r["progress"], "is_claimed": r["claimed"],
            "can_claim": r["complete"] and c["linked"],
            "is_ignored": ignored, "is_mineable": self.wanted_reward(r),
            "is_skipped": False,
            "ignored_reason": "keyword" if ignored else None,
            "ignored_keyword": r["ignored_keyword"], "ignored_precondition": None,
            "benefits": [{"name": r["name"], "type": r["btype"], "image_url": r["image"]}],
            "starts_at": c["starts_at"], "ends_at": c["ends_at"],
        }

    def _ser_campaign(self, c: dict) -> dict:
        drops = [self._ser_drop(c, r) for r in c["rewards"]]
        return {
            "id": c["id"], "name": c["name"], "game_name": c["game"],
            "game_box_art_url": c["game_image"],
            "campaign_url": c["info_url"] or INVENTORY_URL,
            "link_url": c["connect_url"] or INVENTORY_URL,
            "starts_at": c["starts_at"], "ends_at": c["ends_at"], "linked": c["linked"],
            "active": c["status"] == "active", "upcoming": c["status"] == "upcoming",
            "expired": c["status"] == "expired",
            "claimed_drops": sum(d["is_claimed"] for d in drops), "total_drops": len(drops),
            "ignored_drops": sum(d["is_ignored"] for d in drops), "skipped_drops": 0,
            "finished": bool(drops) and all(d["is_claimed"] for d in drops),
            # completed-but-unclaimed rewards (e.g. account not linked yet) keep the campaign visible
            "mining_finished": bool(drops) and all(d["is_claimed"] or d["is_ignored"] for d in drops),
            "drops": drops,
        }

    def inventory(self) -> list[dict]:
        return [self._ser_campaign(c) for c in self.campaigns]

    def wanted_tree(self) -> list[dict]:
        tree: list[dict] = []
        groups: dict[str, dict] = {}
        for cid in self.wanted:
            c = self.campaign(cid)
            if not c:
                continue
            g = groups.get(c["game"])
            if g is None:
                g = groups[c["game"]] = {"game_name": c["game"], "game_icon": c["game_image"], "campaigns": []}
                tree.append(g)
            drops = [{"name": r["name"], "benefits": [r["name"]]} for r in c["rewards"]
                     if self.wanted_reward(r)]
            g["campaigns"].append({"name": c["name"], "url": c["info_url"] or INVENTORY_URL, "drops": drops})
        return tree

    def current_drop(self) -> dict | None:
        cur = self.current
        camp = self.campaign(cur.get("campaign_id")) if cur else None
        if not camp:
            return None
        r = next((r for r in camp["rewards"] if self.wanted_reward(r)), None)
        if not r:
            return None
        watched = r["watched_min"]
        if self.watcher.connected and self.progress_at:
            watched += (time.time() - self.progress_at) / 60
        watched = min(r["required_min"], watched)
        return {
            "drop_id": r["id"], "drop_name": r["name"], "campaign_name": camp["name"],
            "campaign_id": camp["id"], "game_name": camp["game"],
            "current_minutes": int(watched), "required_minutes": int(r["required_min"]),
            "progress": (watched / r["required_min"]) if r["required_min"] else 0,
            "remaining_seconds": max(0, int((r["required_min"] - watched) * 60)),
        }

    def publish_progress(self) -> None:
        d = self.current_drop()
        if d:
            self.emit("drop_progress", d)
        elif self._sent.get("drop_progress_stop") != "1":
            self.emit("drop_progress_stop", {})
        self._sent["drop_progress_stop"] = "0" if d else "1"

    # ------------------------------------------------------------------ claiming
    async def _auto_claim(self) -> None:
        claimed_any = False
        for c in self.campaigns:
            if c["status"] == "upcoming":
                continue
            for r in c["rewards"]:
                if (r["complete"] and not r["ignored_keyword"]
                        and time.time() >= self.claim_cooldown.get(r["id"], 0)):
                    claimed_any |= await self.claim(c, r)
        if claimed_any:
            self.raw_progress = await self.client.get_progress()
            self.progress_at = time.time()
            self._merge()

    async def claim(self, c: dict, r: dict) -> bool:
        try:
            await self.client.claim(c["id"], r["id"], r.get("claim_id"))
        except AuthError:
            raise
        except KickError as e:
            body = (e.body or "").lower()
            if e.status == 400 or "invalid_claim" in body or "connect" in body:
                self.claim_cooldown[r["id"]] = time.time() + 1800
                if c["id"] not in self.link_warned:
                    self.link_warned.add(c["id"])
                    url = f" → {c['connect_url']}" if c["connect_url"] else ""
                    self.log("warning", f"Cannot claim \"{r['name']}\" ({c['game']}) – account not linked?{url}")
            else:
                self.claim_cooldown[r["id"]] = time.time() + 600
                self.log("warning", f"Claim \"{r['name']}\" failed: {e}")
            return False
        r["claimed"], r["complete"] = True, False
        n = sum(x["claimed"] for x in c["rewards"])
        self.log("info", _.t["status"]["claimed_drop"].format(
            drop=f"{c['game']} {r['name']} ({n}/{len(c['rewards'])})"))
        self.history.add(c["game"], c["name"], r["name"], [r["name"]], int(r["required_min"]))
        self.emit("attention_required", {"sound": False})
        self.emit("notification", {"title": "Kick Drops Miner", "message": f"{r['name']} ({c['game']})"})
        await self._notify(c, r)
        self._force_reselect = True
        return True

    async def _notify(self, c: dict, r: dict) -> None:
        s = self.settings
        text = f"🎁 Kick drop claimed: {r['name']}\n{c['game']} – {c['name']}"
        if s.telegram_bot_token and s.telegram_chat_id:
            ok, err = await telegram_send(s.telegram_bot_token, s.telegram_chat_id,
                                          text.replace("<", "&lt;").replace(">", "&gt;"))
            if not ok:
                self.log("warning", f"Telegram: {err}")
        if s.discord_webhook_url:
            ok, err = await discord_send(s.discord_webhook_url, text)
            if not ok:
                self.log("warning", f"Discord: {err}")

    # ------------------------------------------------------------------ channels
    async def _candidates(self, camp: dict, cache: dict) -> list[dict]:
        cat = camp["category_id"]
        if cat not in cache:
            try:
                cache[cat] = await self.client.get_livestreams(cat, limit=48)
            except KickError as e:
                self.log("warning", f"Livestreams {camp['game']}: {e}")
                cache[cat] = []
        allowed = set(camp["channels"])
        out = []
        for st in cache[cat]:
            if not isinstance(st, dict):
                continue
            sl = _slug_of(st)
            if not sl or (allowed and sl not in allowed):
                continue
            ch = st.get("channel") if isinstance(st.get("channel"), dict) else {}
            out.append({
                "slug": sl, "name": ch.get("username") or (ch.get("user") or {}).get("username") or sl,
                "kick_id": ch.get("id"), "viewers": int(st.get("viewer_count") or st.get("viewers") or 0),
                "title": st.get("title") or st.get("session_title") or "",
                "thumbnail": _thumb(st.get("thumbnail")), "game": camp["game"],
                "game_icon": camp["game_image"],
            })
        if allowed and not out:
            sem = asyncio.Semaphore(5)

            async def check(sl: str):
                async with sem:
                    try:
                        info = await self._resolve_channel(sl)
                    except KickError:
                        return None
                    if not info or info["category_id"] not in (None, cat):
                        return None
                    return {"slug": sl, "name": info["name"], "kick_id": info["channel_id"],
                            "viewers": info["viewers"], "title": info["title"],
                            "thumbnail": info["thumbnail"], "game": camp["game"],
                            "game_icon": camp["game_image"]}
            out = [x for x in await asyncio.gather(*(check(sl) for sl in camp["channels"][:30])) if x]
        out.sort(key=lambda x: -x["viewers"])
        return out

    def _is_bad(self, cid: str, slug: str) -> bool:
        until = self.bad.get(f"{cid}|{slug}")
        if until and until < time.time():
            self.bad.pop(f"{cid}|{slug}", None)
            return False
        return bool(until)

    def _usable(self, cid: str) -> list[dict]:
        return [x for x in self.cand_map.get(cid, []) if not self._is_bad(cid, x["slug"])]

    def _progress_metric(self, camp: dict) -> float:
        return max(camp["progress_units"], sum(r["watched_min"] for r in camp["rewards"]),
                   sum(1 for r in camp["rewards"] if r["claimed"]) * 1e6)

    def _check_stall(self) -> None:
        """Switch away from a channel where Kick does not credit watch time."""
        cur = self.current
        camp = self.campaign(cur.get("campaign_id")) if cur else None
        if not cur or not camp or self.manual_channel:
            self.stall = None
            return
        key, metric, now = f"{camp['id']}|{cur['slug']}", self._progress_metric(camp), time.time()
        st = self.stall
        if not st or st["key"] != key or metric > st["metric"]:
            self.stall = {"key": key, "metric": metric, "last": now, "healthy": 0.0}
            return
        if self.watcher.healthy:
            st["healthy"] += now - st["last"]
        st["last"] = now
        if st["healthy"] >= STALL_LIMIT:
            self.bad[key] = now + BAD_CHANNEL_TTL
            self.stall = None
            self.log("warning", f"No drop progress on {cur['name']} for {STALL_LIMIT // 60} min "
                                f"({camp['game']} – {camp['name']}), switching channel")
            self._force_reselect = True

    @staticmethod
    def _cid(x: dict) -> int:
        try:
            return int(x.get("kick_id"))
        except (TypeError, ValueError):
            return zlib.crc32(x["slug"].encode())

    def _publish_channels(self) -> None:
        merged: dict[str, dict] = {}
        for cid, lst in self.cand_map.items():
            c = self.campaign(cid)
            for x in lst:
                e = merged.setdefault(x["slug"], {**x, "acl": False})
                e["acl"] |= bool(c and not c["general"])
        cur = self.current["slug"] if self.current else None
        if cur and cur not in merged and self.current:
            merged[cur] = {"slug": cur, "name": self.current.get("name") or cur,
                           "kick_id": self.current.get("channel_id"), "viewers": self.current.get("viewers"),
                           "thumbnail": self.current.get("thumbnail"), "game": self.current.get("category"),
                           "game_icon": None, "acl": False}
        chans = {}
        for x in sorted(merged.values(), key=lambda x: -(x.get("viewers") or 0))[:60]:
            i = self._cid(x)
            chans[i] = {
                "id": i, "name": x["name"], "login": x["slug"], "url": f"https://kick.com/{x['slug']}",
                "game": x.get("game"), "game_id": None, "game_icon": x.get("game_icon"),
                "viewers": x.get("viewers"), "online": True,
                "drops_enabled": not any(k.endswith("|" + x["slug"]) and v > time.time()
                                         for k, v in self.bad.items()),
                "acl_based": x.get("acl", False), "watching": x["slug"] == cur,
                "thumbnail": x.get("thumbnail") or "",
            }
        self.channels = chans
        self.emit("channels_batch_update", {"channels": list(chans.values())}, dedupe=True)

    async def _resolve_channel(self, slug: str) -> dict | None:
        info = await self.client.get_channel(slug)
        ls = info.get("livestream") if isinstance(info, dict) else None
        if not ls or ls.get("is_live") is False:
            return None
        cat_id, cat_name = _stream_category(ls)
        user = info.get("user") if isinstance(info.get("user"), dict) else {}
        return {
            "slug": slug, "name": user.get("username") or slug, "channel_id": info.get("id"),
            "livestream_id": ls.get("id"),
            "vod_id": ls.get("vod_id") if isinstance(ls.get("vod_id"), str) else None,
            "category_id": cat_id, "category": cat_name, "title": ls.get("session_title") or "",
            "viewers": int(ls.get("viewer_count") or 0), "thumbnail": _thumb(ls.get("thumbnail")),
        }

    async def stop_watching(self) -> None:
        await self.watcher.stop()
        if self.current:
            self.current = None
            self.emit("channel_watching_clear", {})
            self._publish_channels()

    async def _refresh_candidates(self, wanted: list[dict]) -> None:
        cache: dict = {}
        cand_map: dict[str, list[dict]] = {}
        cats: list[str] = []
        for c in wanted:
            if c["category_id"] not in cats:
                if len(cats) >= MAX_CATEGORIES:
                    continue
                cats.append(c["category_id"])
            cand_map[c["id"]] = await self._candidates(c, cache)
        self.cand_map = cand_map
        self.channels_at = time.time()
        self._force_channels = False

    async def _select_and_watch(self) -> None:
        if self.manual_channel:
            info = await self._resolve_channel(self.manual_channel)
            if info:
                camp = next((c for c in self.campaigns if c["status"] == "active"
                             and c["category_id"] == info["category_id"]
                             and (c["general"] or info["slug"] in c["channels"])), None)
                await self._watch(info, camp)
                return
            self.log("info", _.t["status"]["goes_offline"].format(channel=self.manual_channel))
            self.manual_channel = None
            self._force_reselect = True
            self.emit("manual_mode_update", self.manual_info())

        wanted = [c for c in (self.campaign(cid) for cid in self.wanted) if c]
        if not wanted:
            if self.manual_campaign:
                self.manual_campaign = None
                self.emit("manual_mode_update", self.manual_info())
                self._merge()
                self._wake.set()
                return
            await self.stop_watching()
            self.cand_map = {}
            self._publish_channels()
            self.set_status(_.t["status"]["no_campaign"])
            return

        if self._force_channels or time.time() - self.channels_at > CHANNELS_REFRESH \
                or any(c["id"] not in self.cand_map for c in wanted[:1]):
            self.set_status(self.gs("gathering", "Gathering channels..."))
            await self._refresh_candidates(wanted)

        target = next((c for c in wanted if self._usable(c["id"])), None)
        if not target and self.manual_campaign:
            self.log("info", _.t["status"]["no_channel"])
            self.manual_campaign = None
            self.emit("manual_mode_update", self.manual_info())
            self._merge()
            self._force_reselect = True
            self._wake.set()
            return
        if not target:
            await self.stop_watching()
            self._publish_channels()
            self.set_status(_.t["status"]["no_channel"])
            return

        cands = self._usable(target["id"])
        same_cat = [c for c in wanted if c["category_id"] == target["category_id"]]
        covers = lambda slug, c: any(x["slug"] == slug for x in self._usable(c["id"]))
        cur = self.current["slug"] if self.current else None
        if cur and not self._force_reselect and covers(cur, target):
            pick = cur
        else:
            pick = max(cands, key=lambda x: (sum(covers(x["slug"], c) for c in same_cat), x["viewers"]))["slug"]
        self._force_reselect = False

        info = await self._resolve_channel(pick)
        if not info or (info["category_id"] and info["category_id"] != target["category_id"]):
            self.log("info", _.t["status"]["goes_offline"].format(channel=pick))
            self.cand_map[target["id"]] = [x for x in cands if x["slug"] != pick]
            self._force_reselect = True
            self._wake.set()
            return
        await self._watch(info, target)

    async def _watch(self, info: dict, camp: dict | None) -> None:
        prev = self.current["slug"] if self.current else None
        if prev != info["slug"]:
            self.set_status(self.gs("switching", "Switching the channel..."))
            self.current = {**info, "since": time.time()}
        else:
            self.current.update(info)
        self.current["campaign_id"] = camp["id"] if camp else None
        await self.watcher.start(info["slug"], info["channel_id"], info["livestream_id"], info["vod_id"])
        self._publish_channels()
        if prev != info["slug"]:
            ch_id = next((i for i, c in self.channels.items() if c["login"] == info["slug"]), None)
            if ch_id is not None:
                self.emit("channel_watching", {"id": ch_id})
            self.log("info", _.t["status"]["watching"].format(channel=info["name"]))
            if self.manual_channel:
                self.emit("manual_mode_update", self.manual_info())
        self.set_status(_.t["status"]["watching"].format(channel=info["name"]))

    # ------------------------------------------------------------------ initial state
    def initial_state(self, settings: dict) -> dict:
        return {
            "status": self.status,
            "channels": list(self.channels.values()),
            "campaigns": self.inventory(),
            "console": list(self.console),
            "settings": settings,
            "login": self.login_status(),
            "manual_mode": self.manual_info(),
            "current_drop": self.current_drop(),
            "wanted_items": self.wanted_tree(),
        }
