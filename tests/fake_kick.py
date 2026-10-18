"""Controllable fake Kick for state tests. State in ctrl.json, re-read on every request."""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["DATA_DIR"] = os.path.join(HERE, "data")
from datetime import datetime, timedelta, timezone
from kdm import kick, watcher, miner as M
from kdm.kick import KickError
M.TICK = 1; M.PROGRESS_REFRESH = 1; M.CHANNELS_REFRESH = 2; M.STALL_LIMIT = 4; M.BAD_CHANNEL_TTL = 8

now = datetime.now(timezone.utc)
iso = lambda d: (now + timedelta(days=d)).isoformat()
CATS = {"13": "Rust", "7": "Counter-Strike 2"}
WATCHING = {"slug": None}
UNITS = {"01RUST": 0.0, "01CS2": 0.0}
LAST = {"t": time.time()}
CLAIMED = set()

def ctrl():
    return json.load(open(os.path.join(HERE, "ctrl.json")))

def tick_units(c):
    t = time.time(); dt = t - LAST["t"]; LAST["t"] = t
    s = WATCHING["slug"]
    if not s: return
    ch = c["channels"].get(s)
    if not ch or not ch["live"] or s not in c["productive"]: return
    for cid, cat in (("01RUST", "13"), ("01CS2", "7")):
        if ch["cat"] == cat:
            UNITS[cid] += dt * c.get("speed", 1)

def campaigns(c):
    return {"data": [
      {"id": "01RUST", "name": "Rust S3", "starts_at": iso(-3), "ends_at": iso(4), "category": {"id": 13, "name": "Rust"},
       "channels": [], "rewards": [{"id": "r1", "name": "Rust Hoodie", "required_units": c.get("rust_req", 10000)}]},
      {"id": "01CS2", "name": "CS2 Drops", "starts_at": iso(-1), "ends_at": iso(1), "category": {"id": 7, "name": "Counter-Strike 2"},
       "channels": [], "rewards": [{"id": "c1", "name": "CS2 Capsule", "required_units": 10000}]}]}

def progress(c):
    out = []
    for cid, rid, req in (("01RUST", "r1", c.get("rust_req", 10000)), ("01CS2", "c1", 10000)):
        out.append({"id": cid, "progress_units": UNITS[cid], "user_app_connected": c.get("linked", {}).get(cid, True),
                    "rewards": [{"id": rid, "progress": min(1, UNITS[cid] / req), "claimed": rid in CLAIMED, "required_units": req}]})
    return {"data": out}

async def req(self, method, url, *, auth=True, json_body=None, headers=None):
    c = ctrl(); tick_units(c)
    if url.endswith("/api/v1/user"):
        if not c.get("token_ok", True) or self.token != "good|token": raise kick.AuthError("expired", 401)
        return {"id": 1, "username": "davson"}
    if url.endswith("/drops/campaigns"): return campaigns(c)
    if url.endswith("/drops/progress"):
        if not c.get("token_ok", True): raise kick.AuthError("Unauthorized (401)", 401)
        return progress(c)
    if "/livestreams" in url:
        cat = url.split("category_id=")[1]
        ls = [{"id": i, "title": s, "viewer_count": ch["viewers"], "channel": {"id": 100 + i, "slug": s, "username": s}}
              for i, (s, ch) in enumerate(c["channels"].items()) if ch["live"] and ch["cat"] == cat]
        return {"data": {"livestreams": sorted(ls, key=lambda x: -x["viewer_count"])}}
    if "/api/v2/channels/" in url:
        s = url.rsplit("/", 1)[1]; ch = c["channels"].get(s)
        if not ch or not ch["live"]: return {"id": 5, "livestream": None}
        return {"id": 100 + list(c["channels"]).index(s), "user": {"username": s},
                "livestream": {"id": 555, "is_live": True, "viewer_count": ch["viewers"],
                               "categories": [{"id": int(ch["cat"]), "name": CATS[ch["cat"]]}]}}
    if url.endswith("/drops/claim"):
        CLAIMED.add(json_body["reward_id"]); return {"message": "OK"}
    if "settings/global" in url: return {}
    raise KickError(f"unmocked {url}", 404)
kick.KickClient.request = req

async def fake_run(self):
    self.connected = True
    while True:
        WATCHING["slug"] = self.slug
        self.last_event = time.time(); self.events_sent += 1
        await asyncio.sleep(0.5)
_orig_stop = watcher.ViewerWatcher.stop
async def stop(self):
    WATCHING["slug"] = None
    await _orig_stop(self)
watcher.ViewerWatcher._run = fake_run
watcher.ViewerWatcher.stop = stop

import uvicorn
uvicorn.run("kdm.web:socket_app", host="127.0.0.1", port=8099, log_level="warning")
