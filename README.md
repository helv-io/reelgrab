# reelgrab

Matrix **appservice bot** that grabs **short-form** social videos (reels / shorts) from links and posts the file back into the room.

Configuration follows the **mautrix / mau.dev** pattern:

| File | Where | Purpose |
|------|--------|---------|
| `config.yaml` | data dir | Fully documented settings (auto-created) |
| `registration.yaml` | data dir | Homeserver appservice registration (auto-created) |

Defaults use placeholders (`example.com`, `localhost`).

## Docker Hub image

Multi-arch images (`linux/amd64`, `linux/arm64`) are built by GitHub Actions and published to:

```text
docker.io/helvio/reelgrab
```

```bash
docker pull helvio/reelgrab:latest
```

## Docker (recommended)

```bash
git clone https://github.com/helv-io/reelgrab.git && cd reelgrab
# or: docker pull helvio/reelgrab:latest
mkdir -p data

# 1) First run writes /data/config.yaml and exits
docker compose run --rm reelgrab

# 2) Edit config — at minimum:
#      homeserver.address   e.g. http://synapse:8008
#      homeserver.domain    e.g. example.com
#      bot.admin_users      e.g. ["@you:example.com"]
$EDITOR data/config.yaml

# 3) Second run mints as_token/hs_token and writes registration.yaml
docker compose run --rm reelgrab

# 4) Point your homeserver at the registration file, then restart HS
#    Synapse example (path must be visible inside the Synapse container):
#      app_service_config_files:
#        - /bots/reelgrab.yaml
#    Copy or mount:
#      cp data/registration.yaml /path/on/host/bots/reelgrab.yaml

# 5) Join the bot to the homeserver's Docker network (edit compose.yaml), then:
docker compose up -d
docker compose logs -f
```

### Data directory layout

```
data/                      # bind-mounted to /data in the container
  config.yaml              # you edit this
  registration.yaml        # generated; give to Synapse
  runtime_state.yaml       # DM toggles (allow-list, auto, …)
  cookies.txt              # optional site cookies (Netscape format)
  downloads/               # temp media
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `REELGRAB_DATA` | `/data` in Docker | Data directory |
| `REELGRAB_DOCKER` | `1` in image | Prefer `/data` when set |

`restart: on-failure` so “generated config, please edit” (exit 0) does not restart-loop.

## Local (without Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export REELGRAB_DATA=./data
python -m reelgrab                 # creates data/config.yaml, exits
# edit data/config.yaml
python -m reelgrab                 # tokens + registration.yaml
# register with HS, then:
python -m reelgrab                 # runs
```

```text
python -m reelgrab -d /path/to/data
python -m reelgrab -c /path/to/config.yaml
python -m reelgrab --generate-registration
```

## Homeserver registration

Same model as **mautrix** bridges: the homeserver **pushes** events to the bot.

```yaml
id: reelgrab
url: http://reelgrab:29399   # appservice.address — HS must reach this
as_token: <secret>
hs_token: <secret>
sender_localpart: reelgrab
rate_limited: false
namespaces:
  users:
    - regex: '^@reelgrab:example\.com$'
      exclusive: true
```

- `url` = `appservice.address` (e.g. `http://reelgrab:29399` on a shared Docker network).
- Synapse calls `PUT /_matrix/app/v1/transactions/{txnId}` with `Authorization: Bearer <hs_token>`.
- Outbound (send / upload / join) uses Client-Server API with `as_token`.
- AS users cannot use Client-Server `/sync` on modern Synapse; set a real `url`.
- Bot MXID: `@<appservice.bot.username>:<homeserver.domain>`
- After changing `registration.yaml`, restart the homeserver.

## Using the bot

1. DM `@reelgrab:example.com`.
2. Send `help` / `status` (admin commands need your MXID in `bot.admin_users`).
3. Invite the bot to rooms that receive video links.
4. Optional: `allow !roomid:example.com`.

**Bridged rooms:** If you use the bot in rooms bridged from other networks (e.g. Instagram, WhatsApp, Discord via [mau.dev](https://docs.mau.fi/) bridges), **relaying must be active** for that room. Without relay mode, messages from the remote side are not visible to the appservice bot in the same way, so links will not be picked up. Enable relay on the bridge for those rooms the same way you would for other bots that need to see bridged traffic.

### Admin DM commands

| Command | Effect |
|---------|--------|
| `help` | Command list |
| `ping` | pong |
| `status` | runtime + cookies + allow-list |
| `whoami` | your MXID |
| `rooms` | joined room IDs |
| `allow <room_id>` / `allow clear` | allow-list |
| `deny <room_id>` | remove from allow-list |
| `auto on\|off` | auto-download |
| `notify on\|off` | failure notices |
| `caption <text>` | success caption |
| `!grab <url>` | force one download |

## Config reference

Every key is documented **in** `config.yaml`. Sections: `homeserver`, `appservice`, `bot`, `download`, `urls` (patterns), `logging`.

Relative paths resolve against the **data directory**.

## Supported links (defaults)

Short-form only (not full long-form pages):

| Site | Matched URL shapes |
|------|--------------------|
| Instagram | `/reel/`, `/reels/`, `instagr.am`, `l.instagram.com` |
| YouTube | `/shorts/` only (not `watch?v=`) |
| Facebook | `/reel/`, `/reels/`, `/share/r/`, `fb.watch` |
| TikTok | `/@…/video/…`, `vm.tiktok.com`, `vt.tiktok.com`, `/t/` |

Override or extend via `urls.url_patterns` in `config.yaml`.

## Cookies

Export Netscape `cookies.txt` into `data/cookies.txt` when a site requires a session.

## Download

Downloads run **in-process** with **yt-dlp**. After download, **ffmpeg** re-encodes to a mobile-friendly **H.264 + AAC MP4**. Override under `download.convert` in `config.yaml`.

```yaml
download:
  convert:
    enabled: true
    force: true          # false = skip when already H.264/AAC/yuv420p MP4
    video_codec: libx264
    audio_codec: aac
    audio_bitrate: 128k
    video_preset: veryfast
    video_crf: 23
    pixel_format: yuv420p
    profile: baseline
    level: "3.1"
    max_width: 1280
    max_height: 1280
    movflags: "+faststart"
    extra_args: []       # e.g. ["-bf", "0"]
    timeout_seconds: 600
```

No external download container is required.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Layout

```text
reelgrab/
  __main__.py          # CLI
  config.py            # load / generate config + registration
  default_config.py    # documented default config.yaml
  appservice.py        # HS → bot transaction HTTP (mautrix-style)
  matrix_client.py     # as_token outbound CS API + event dispatch
  handlers.py          # pipeline
  commands.py          # DM admin commands
  downloader.py        # yt-dlp download + ffmpeg probe/thumbnail
  urls.py
  state.py
Dockerfile
compose.yaml
```
