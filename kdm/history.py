"""Claimed drop history (API compatible with the TwitchDropsMiner history tab)."""
from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import datetime, timezone

from . import config


class DropHistory:
    MAX = 20000

    def __init__(self) -> None:
        data = config.load_json(config.HISTORY_FILE, [])
        # entries from the first version used {"t", "reward", ...}
        self.entries: list[dict] = []
        for e in data if isinstance(data, list) else []:
            if "claimed_at" not in e and "t" in e:
                e = {"claimed_at": datetime.fromtimestamp(e["t"], timezone.utc).isoformat(),
                     "game": e.get("game", ""), "campaign": e.get("campaign", ""),
                     "drop_name": e.get("reward", ""), "benefits": [e.get("reward", "")],
                     "required_minutes": e.get("required_minutes", 0)}
            self.entries.append(e)

    @property
    def total_count(self) -> int:
        return len(self.entries)

    def add(self, game: str, campaign: str, drop: str, benefits: list[str], minutes: int) -> None:
        self.entries.insert(0, {
            "claimed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "game": game, "campaign": campaign, "drop_name": drop,
            "benefits": benefits, "required_minutes": minutes,
        })
        del self.entries[self.MAX:]
        config.save_json(config.HISTORY_FILE, self.entries)

    def clear(self) -> None:
        self.entries = []
        config.save_json(config.HISTORY_FILE, self.entries)

    def get_entries(self, game: str | None = None, since: datetime | None = None,
                    limit: int | None = None) -> list[dict]:
        out = []
        for e in self.entries:
            if game and game.lower() not in e["game"].lower():
                continue
            if since:
                try:
                    if datetime.fromisoformat(e["claimed_at"]) < since:
                        continue
                except ValueError:
                    pass
            out.append(e)
            if limit and len(out) >= limit:
                break
        return out

    def to_csv(self, game=None, since=None) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["claimed_at", "game", "campaign", "drop", "rewards", "required_minutes"])
        for e in self.get_entries(game, since):
            w.writerow([e["claimed_at"], e["game"], e["campaign"], e["drop_name"],
                        ", ".join(e.get("benefits") or []), e.get("required_minutes", 0)])
        return buf.getvalue()

    def stats_by_game(self) -> dict[str, int]:
        return dict(Counter(e["game"] for e in self.entries).most_common())

    def stats_by_month(self) -> dict[str, int]:
        c = Counter(e["claimed_at"][:7] for e in self.entries)
        return dict(sorted(c.items()))
