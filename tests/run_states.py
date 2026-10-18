import json, os, time, urllib.request
B = "http://127.0.0.1:8099"
HERE = os.path.dirname(os.path.abspath(__file__))
C = {
  "token_ok": True, "speed": 1, "rust_req": 10000, "linked": {},
  "productive": ["rustguy", "solorust", "streamerb", "cs2pro"],
  "channels": {
    "rustguy":  {"live": True, "cat": "13", "viewers": 5000},
    "solorust": {"live": True, "cat": "13", "viewers": 100},
    "streamerb": {"live": True, "cat": "7", "viewers": 900},
    "cs2pro":   {"live": True, "cat": "7", "viewers": 300},
  }}
def save(): json.dump(C, open(os.path.join(HERE, "ctrl.json"), "w"))
def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, method=method or ("POST" if body is not None else "GET"),
                               headers={"Content-Type": "application/json", "X-TDM-Request": "1"})
    return json.load(urllib.request.urlopen(r))
def status(): return api("/api/status")
def watching():
    s = status()["status"]
    return s.split("Watching: ")[1] if s.startswith("Watching: ") else None
results = []
def expect(name, cond, timeout=15):
    t = time.time(); last = None
    while time.time() - t < timeout:
        last = (watching(), status()["status"])
        if cond(): results.append((name, True, f"{time.time()-t:.0f}s", last[1])); return
        time.sleep(0.5)
    results.append((name, False, "timeout", last[1]))

save()
api("/api/session/login", {"cookies": "good%7Ctoken"})
api("/api/settings", {"games_to_watch": ["Rust", "Counter-Strike 2"], "allow_unlinked_campaigns": False})

expect("1 priority game Rust → watches top Rust channel", lambda: watching() == "rustguy")
C["channels"]["rustguy"]["live"] = False; save()
expect("2 Rust channel offline → other Rust channel", lambda: watching() == "solorust")
C["channels"]["solorust"]["live"] = False; save()
expect("3 all Rust offline → next game CS2", lambda: watching() in ("streamerb", "cs2pro"))
C["channels"]["rustguy"]["live"] = True; save()
expect("4 Rust back online → returns to Rust", lambda: watching() == "rustguy")
C["channels"]["rustguy"]["cat"] = "7"; save()
expect("5 streamer switched game → leaves channel", lambda: watching() != "rustguy")
C["channels"]["rustguy"]["cat"] = "13"; C["channels"]["solorust"]["live"] = True; save()
expect("6 back to Rust (rustguy)", lambda: watching() == "rustguy", 20)
C["productive"] = ["solorust", "streamerb", "cs2pro"]; save()
expect("7 no progress on rustguy → switches to solorust", lambda: watching() == "solorust", 20)
time.sleep(4)
results.append(("7b stays on productive solorust", watching() == "solorust", "", status()["status"]))
C["productive"] = ["streamerb", "cs2pro"]; save()
expect("8 no Rust channel gives progress → CS2", lambda: watching() in ("streamerb", "cs2pro"), 25)
C["productive"] = ["rustguy", "solorust", "streamerb", "cs2pro"]; save()
expect("9 after blacklist TTL → back to Rust", lambda: watching() in ("rustguy", "solorust"), 30)
C["linked"] = {"01RUST": False}; save()
expect("10 Rust not linked (allow off) → CS2", lambda: watching() in ("streamerb", "cs2pro"))
api("/api/settings", {"allow_unlinked_campaigns": True})
expect("11 allow unlinked → back to Rust", lambda: watching() in ("rustguy", "solorust"))
C["linked"] = {}; save()
api("/api/settings", {"games_to_watch": ["Counter-Strike 2"]})
expect("12 Rust removed from list → CS2", lambda: watching() in ("streamerb", "cs2pro"))
api("/api/settings", {"games_to_watch": ["Rust", "Counter-Strike 2"]})
expect("13 Rust re-added as top → Rust", lambda: watching() in ("rustguy", "solorust"))
C["rust_req"] = 1; save()
expect("14 Rust drop complete → claimed, moves to CS2", lambda: watching() in ("streamerb", "cs2pro"))
hist = api("/api/history")["entries"]
results.append(("14b claim in history", any(e["drop_name"] == "Rust Hoodie" for e in hist), "", ""))
api("/api/channels/select", {"channel_id": [c for c in api("/api/channels")["channels"] if c["login"] == "cs2pro"][0]["id"]})
expect("15 manual channel cs2pro", lambda: watching() == "cs2pro" and status()["manual_mode"]["active"])
C["channels"]["cs2pro"]["live"] = False; save()
expect("16 manual channel offline → auto mode", lambda: not status()["manual_mode"]["active"] and watching() == "streamerb")
C["channels"]["cs2pro"]["live"] = True; save()
api("/api/settings", {"mining_hours": {"mode": "range", "start": "03:00", "end": "03:01"}})
expect("17 outside mining hours → stops", lambda: watching() is None and "hours" in status()["status"].lower())
api("/api/settings", {"mining_hours": {"mode": "always", "start": "03:00", "end": "03:01"}})
expect("18 mining hours always → resumes", lambda: watching() is not None)
C["channels"]["streamerb"]["live"] = C["channels"]["cs2pro"]["live"] = False; save()
expect("19 nothing live → waiting message", lambda: watching() is None and "No available channels" in status()["status"])
C["channels"]["cs2pro"]["live"] = True; save()
expect("20 channel appears → watches", lambda: watching() == "cs2pro")
C["token_ok"] = False; save()
api("/api/reload", {})
expect("21 token expired → login required", lambda: not api("/api/session")["logged_in"], 20)

w = max(len(r[0]) for r in results)
for n, ok, t, st in results:
    print(f"{'PASS' if ok else 'FAIL'}  {n.ljust(w)}  {t:>4}  {st}")
print(f"\n{sum(r[1] for r in results)}/{len(results)} passed")
