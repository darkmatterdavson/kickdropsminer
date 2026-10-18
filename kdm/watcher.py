"""Low-bandwidth Kick 'watching' through the viewer WebSocket (no video)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import websockets

from .kick import USER_AGENT, AuthError, KickClient

log = logging.getLogger("kdm.watcher")

HANDSHAKE_EVERY = 15
PING_EVERY = 30


def _num(v):
    try:
        n = int(v)
        return n if str(n) == str(v) else v
    except (TypeError, ValueError):
        return v


class ViewerWatcher:
    def __init__(self, client: KickClient, logfn: Callable[[str, str], None]):
        self.client = client
        self.logfn = logfn
        self.slug: str | None = None
        self.channel_id = None
        self.livestream_id = None
        self.vod_id: str | None = None
        self.interval = 120
        self.connected = False
        self.last_event = 0.0
        self.events_sent = 0
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._ws = None

    @property
    def healthy(self) -> bool:
        return self.connected and time.time() - self.last_event < 2 * self.interval + 30

    async def start(self, slug: str, channel_id, livestream_id, vod_id: str | None = None) -> None:
        if self.slug == slug and self._task and not self._task.done():
            self.update(livestream_id, vod_id)
            return
        await self.stop()
        self.slug, self.channel_id = slug, channel_id
        self.livestream_id, self.vod_id = livestream_id, vod_id
        self.error = None
        self.events_sent = 0
        self._task = asyncio.create_task(self._run(), name=f"watch-{slug}")

    def update(self, livestream_id, vod_id: str | None = None) -> None:
        if livestream_id and livestream_id != self.livestream_id:
            self.livestream_id = livestream_id
            self.vod_id = vod_id
            asyncio.create_task(self._send_watch())  # new broadcast → track right away

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.connected = False
        self.slug = None
        self._ws = None

    async def _send(self, payload: dict) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))

    async def _send_watch(self) -> None:
        if not self.livestream_id or self._ws is None:
            return
        msg = {
            "name": "tracking.user.watch.livestream",
            "channel_id": _num(self.channel_id),
            "livestream_id": _num(self.livestream_id),
        }
        if self.vod_id:
            msg["vod_id"] = self.vod_id
        try:
            await self._send({"type": "user_event", "data": {"message": msg}})
            self.last_event = time.time()
            self.events_sent += 1
            if self.events_sent == 1:
                self.logfn("info", f"Watching active: {self.slug} (watch event every {self.interval}s)")
        except Exception as e:
            log.debug("watch send failed: %s", e)

    async def _run(self) -> None:
        backoff = 5
        while True:
            try:
                self.interval = await self.client.get_watch_interval()
                token = await self.client.get_viewer_token()
                url = f"wss://websockets.kick.com/viewer/v1/connect?token={token}"
                async with websockets.connect(
                    url,
                    additional_headers={"Origin": "https://kick.com"},
                    user_agent_header=USER_AGENT,
                    ping_interval=None,
                    open_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    self.error = None
                    backoff = 5
                    handshake = {"type": "channel_handshake",
                                 "data": {"message": {"channelId": _num(self.channel_id)}}}
                    await self._send(handshake)
                    await self._send({"type": "ping"})
                    await self._send_watch()
                    t_hs = t_ping = t_watch = time.time()
                    while True:
                        try:
                            await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            pass
                        now = time.time()
                        if now - t_hs >= HANDSHAKE_EVERY:
                            await self._send(handshake)
                            t_hs = now
                        if now - t_ping >= PING_EVERY:
                            await self._send({"type": "ping"})
                            t_ping = now
                        if now - t_watch >= self.interval:
                            await self._send_watch()
                            t_watch = now
            except asyncio.CancelledError:
                raise
            except AuthError as e:
                self.error = str(e)
                self.logfn("error", f"Viewer token: {e}")
                backoff = 60
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.logfn("warning", f"Connection to {self.slug} dropped ({self.error}), retrying in {backoff}s")
            finally:
                self.connected = False
                self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
