"""Paths and persistent settings (keys compatible with the TwitchDropsMiner web UI)."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

__version__ = "1.3.1"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
WEB_DIR = BASE_DIR / "web"
SETTINGS_FILE = DATA_DIR / "settings.json"
SESSION_FILE = DATA_DIR / "session.json"
HISTORY_FILE = DATA_DIR / "history.json"
AUTH_FILE = DATA_DIR / "web_auth.json"

PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "English")

DATA_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()

DEFAULT_FILTERS = {
    "show_active": True, "link_status": "all", "show_upcoming": False, "show_expired": False,
    "show_finished": False, "game_name_search": [], "show_benefit_item": True,
    "show_benefit_badge": True, "show_benefit_emote": True, "show_benefit_other": True,
}
LINK_STATUSES = ("all", "linked", "not_linked")
DEFAULTS: dict[str, Any] = {
    "games_to_watch": [],
    "drop_name_blacklist": [],
    "dark_mode": True,
    "language": DEFAULT_LANGUAGE,
    "proxy": "",
    "connection_quality": 1,
    "minimum_refresh_interval_minutes": 15,
    "discord_webhook_url": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "inventory_filters": DEFAULT_FILTERS,
    "inventory_list_view": False,
    "mining_benefits": {"DIRECT_ENTITLEMENT": True, "BADGE": True, "EMOTE": True, "UNKNOWN": True},
    "mining_hours": {"mode": "always", "start": "08:00", "end": "22:00"},
    "allow_unlinked_campaigns": False,
}
# settings.json from the first (custom UI) version of this project
LEGACY_KEYS = {"priority_games": "games_to_watch", "ignored_reward_keywords": "drop_name_blacklist",
               "telegram_token": "telegram_bot_token", "allow_unlinked": "allow_unlinked_campaigns"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def normalize_hours(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("mining_hours")
    mode = value.get("mode", "always")
    start, end = str(value.get("start", "08:00")), str(value.get("end", "22:00"))
    if mode not in ("always", "range") or not TIME_RE.match(start) or not TIME_RE.match(end):
        raise ValueError("mining_hours")
    return {"mode": mode, "start": start, "end": end}


def normalize_keywords(values: Any) -> list[str]:
    seen, out = set(), []
    for v in values or []:
        v = str(v).strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


class Settings:
    def __init__(self) -> None:
        self.__dict__.update(copy.deepcopy(DEFAULTS))
        if SETTINGS_FILE.exists():
            try:
                raw = json.loads(SETTINGS_FILE.read_text("utf-8"))
                for old, new in LEGACY_KEYS.items():
                    if old in raw and new not in raw:
                        raw[new] = raw[old]
                for k in DEFAULTS:
                    if k in raw:
                        setattr(self, k, raw[k])
                self.inventory_filters = {**DEFAULT_FILTERS, **(self.inventory_filters or {})}
            except Exception:
                pass

    def as_dict(self) -> dict:
        return {k: copy.deepcopy(getattr(self, k)) for k in DEFAULTS}

    def save(self) -> None:
        save_json(SETTINGS_FILE, self.as_dict())


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.chmod(name, 0o600)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
