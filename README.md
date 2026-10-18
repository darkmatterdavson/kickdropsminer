# Kick Drops Miner

Self-hosted AFK miner for **Kick Drops** – [github.com/darkmatterdavson/kickdropsminer](https://github.com/darkmatterdavson/kickdropsminer).
Fork of [rangermix/TwitchDropsMiner](https://github.com/rangermix/TwitchDropsMiner). The web UI is taken 1:1 from [rangermix/TwitchDropsMiner](https://github.com/rangermix/TwitchDropsMiner) (MIT); the backend is rewritten for Kick.

## Features

- **Stream-less mining** – watches via Kick's viewer WebSocket, no video is downloaded
- **Automatic campaign discovery** and game priority (Games to Watch, drag & drop)
- **Automatic channel switching** when a stream goes offline or changes game
- **Wanted Drops Queue** works like TwitchDropsMiner: only games in Games to Watch are mined, in that order. ➕ Mine in Inventory appends the game to the end of the queue
- **Manual mode** – click a channel; falls back to auto mode when the channel goes offline
- **Auto-claim** of rewards + drop history (CSV export, stats)
- **Telegram & Discord** notifications
- **Ignored drop keywords**, **Mining Hours**, **proxy** (with verification)
- **Dashboard password** (Settings → Dashboard password)
- **20 UI languages**, dark / light mode
- **Diagnostics dump** to `data/diagnostics`

## Running

### Docker Compose (recommended)

```bash
docker compose up -d --build
```

Open <http://localhost:8080>.

### From source

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Requires Python 3.11+.

## Signing in

1. Log in on kick.com in your browser.
2. Open DevTools (F12) → Application → Cookies → `https://kick.com` and copy the `session_token` cookie value.
3. Paste it on the sign in screen.

A full `Cookie` header, a Cookie-Editor JSON export or `cookies.txt` also works. The token is stored in `data/session.json`. Do not log out on kick.com in that browser, or the token stops working.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | Web UI port |
| `DATA_DIR` | `/app/data` | Settings, session, history, dashboard password |
| `DEFAULT_LANGUAGE` | `English` | Default UI language (e.g. `Deutsch`, `Čeština`) |
| `PUBLIC_BASE_URL` | – | Exact public URL behind a reverse proxy (origin check + Secure cookie) |
| `LOG_LEVEL` | `INFO` | Log level |

Behind a reverse proxy, make sure WebSockets on `/socket.io/` are forwarded.

## How it works

| Step | Endpoint |
|---|---|
| Campaigns | `GET web.kick.com/api/v1/drops/campaigns` |
| Progress | `GET web.kick.com/api/v1/drops/progress` |
| Live streams in category | `GET web.kick.com/api/v1/livestreams?category_id=…` |
| Channel state | `GET kick.com/api/v2/channels/{slug}` |
| Watching | `websockets.kick.com/viewer/v1/token` → WS `tracking.user.watch.livestream` |
| Claim | `POST web.kick.com/api/v1/drops/claim` |

HTTP requests use `curl_cffi` with a Chrome TLS fingerprint (Cloudflare). Kick's API is undocumented and may change.

## Project layout

```
main.py           entrypoint (uvicorn)
kdm/kick.py       Kick HTTP client + cookie parsing
kdm/watcher.py    viewer WebSocket
kdm/miner.py      queue, channel selection, claiming, UI events
kdm/web.py        FastAPI REST + Socket.IO
kdm/auth.py       dashboard password (from TwitchDropsMiner)
web/, lang/       UI and translations (from TwitchDropsMiner)
```

## Disclaimer

Automated watching may violate Kick's terms of service. Use at your own risk. The session token grants full access to your account – do not expose the UI without a password.

## License & credits

- UI (`web/`, `lang/`) and the auth module come from [rangermix/TwitchDropsMiner](https://github.com/rangermix/TwitchDropsMiner), a fork of [DevilXD/TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner), MIT – see `LICENSE-TwitchDropsMiner`.
- Kick API research: [jamezrin/lurkloot](https://github.com/jamezrin/lurkloot), [Ab-code520/kick-miner-pro](https://github.com/Ab-code520/kick-miner-pro), [HyperBeats/KickDropsMiner](https://github.com/HyperBeats/KickDropsMiner).
