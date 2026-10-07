# reelgrab

[![CI](https://github.com/helv-io/reelgrab/actions/workflows/ci.yml/badge.svg)](https://github.com/helv-io/reelgrab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

<p align="center">
  <img src="images/icon.jpg" alt="reelgrab icon" width="128" height="128">
</p>

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
  media_cache.sqlite       # URL → uploaded mxc (survives restarts)
  avatar_state.yaml        # last uploaded avatar hash (skips repeat uploads)
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
org.matrix.msc3202: true
namespaces:
  users:
    - regex: '^@reelgrab:example\.com$'
      exclusive: true
```

- `url` = `appservice.address` (e.g. `http://reelgrab:29399` on a shared Docker network).
- Synapse calls `PUT /_matrix/app/v1/transactions/{txnId}` with `Authorization: Bearer <hs_token>`.
- Outbound (send / upload / join / device keys) uses the Client-Server API with `as_token`.
- AS users cannot use Client-Server `/sync` on modern Synapse; set a real `url`. Encrypted rooms use MSC3202 fields inside that push, not `/sync`.
- Bot MXID: `@<appservice.bot.username>:<homeserver.domain>`
- After changing `registration.yaml`, restart the homeserver.

### Encrypted rooms

Upgrading from 0.6.x rewrites `registration.yaml` to add `org.matrix.msc3202: true`. Tokens and `url` stay. Copy the file onto the homeserver if it does not read the data directory directly, then restart the homeserver.

Synapse 1.141 or newer also needs:

```yaml
experimental_features:
  msc3202_transaction_extensions: true
  msc2409_to_device_messages_enabled: true
```

Device keys and Megolm sessions are files in the data directory (`crypto.sqlite`, `crypto_pickle.key`, `mx-state.json`). The `./data:/data` mount already covers them. There is no new volume and no new environment variable. `encryption.enabled: false` turns the Olm machine off; unencrypted rooms keep working either way.

An encrypted DM can send `!reel help`. The reply and the uploaded video are encrypted for that room. Reactions stay as normal `m.reaction` events.

`GET /health` is open (no `hs_token`). It returns 503 until the homeserver answers and 200 once the bot is ready. The image `HEALTHCHECK` calls `http://127.0.0.1:29399/health`. If the homeserver is down at start, the process keeps retrying with backoff instead of logging ready and idling.

## Bot avatar (profile picture)

Matrix does **not** ship avatar bytes inside the profile event. The profile stores an **`mxc://` media URI**; other homeservers fetch that media from yours over federation.

On startup, reelgrab (like the display name):

1. Uploads the configured image to **your** homeserver media repo (`POST /_matrix/media/v3/upload`) with the appservice token.
2. Sets the bot profile avatar to the returned `mxc://…` (`PUT /_matrix/client/v3/profile/…/avatar_url`).

```yaml
appservice:
  bot:
    displayname: Reelgrab
    # default  = packaged icon (reelgrab/assets/icon.jpg)
    # path     = custom file (relative to data dir, or absolute)
    # ""       = leave avatar unchanged
    avatar: default
```

No extra homeserver config is required beyond a working appservice registration: exclusive AS users can upload media and edit their own profile with `as_token`. After the first successful start, Element and other clients should show the icon (refresh/reopen the room if a client cached a blank avatar).

## Using the bot

The bot stays quiet unless a message contains the exact token `!reel` or a supported media URL. Bare chat such as `ping` gets no reply.

1. DM `@reelgrab:example.com` (an encrypted Element DM works once the homeserver MSC3202 flags above are on).
2. Send `!reel help` / `!reel status` (admin commands need your MXID in `bot.admin_users`).
3. Invite the bot to rooms that receive video links.
4. Optional: `!reel allow !roomid:example.com`.

**Bridged rooms:** If you use the bot in rooms bridged from other networks (e.g. Instagram, WhatsApp, Discord via [mau.dev](https://docs.mau.fi/) bridges), **relaying must be active** for that room. Without relay mode, messages from the remote side are not visible to the appservice bot in the same way, so links will not be picked up. Enable relay on the bridge for those rooms the same way you would for other bots that need to see bridged traffic.

### Admin DM commands

| Command | Effect |
|---------|--------|
| `!reel help` | Command list |
| `!reel ping` | pong |
| `!reel status` | runtime, cookies, yt-dlp version, upload limit, E2EE device |
| `!reel whoami` | your MXID |
| `!reel rooms` | joined room IDs |
| `!reel allow <room_id>` / `!reel allow clear` | allow-list |
| `!reel deny <room_id>` | remove from allow-list |
| `!reel auto on\|off` | auto-download |
| `!reel notify on\|off` | one-line failure notice |
| `!reel caption <text>` | caption override (`caption clear` = metadata) |
| `!reel room` | this room's auto / notify / caption |
| `!reel room auto on\|off\|default` | auto-download for this room only |
| `!reel room notify on\|off\|default` | failure line for this room only |
| `!reel room caption <text>` | caption for this room (`room caption clear` inherits) |
| `!reel <url>` | force one download (any http URL; admin) |

`bot.command_prefix` is the listen token (default `!reel`). It must be a whole word.

While a grab is in progress the bot reacts ⏳ on the source message, then ✅ or ❌. On success it posts the `m.video` with a filename and a caption (`@uploader: title · source url`, or `bot.success_caption` when that is set). On failure it posts one short line when `notify_on_failure` is on. The Python traceback stays in the log.

A link sent inside a thread is answered inside that thread. Edits, `m.notice` messages, and the quoted part of a reply are ignored. With `bot.ignore_history: true` (the default), events older than this process start are skipped, so a backlog replay after downtime does not re-grab old links.

The same URL is uploaded once. Later sends, including after a restart and in other rooms, reuse that `mxc://` from `media_cache.sqlite`. The same room will not post it again until `dedupe_ttl_seconds` has passed.

## Config reference

Every key is documented **in** `config.yaml`. Sections: `homeserver`, `appservice`, `bot`, `download`, `urls` (patterns), `logging`.

Relative paths resolve against the **data directory**.

## Supported links (defaults)

Defaults (YouTube `watch?v=` pages are not included; `!reel <url>` can still fetch any other http URL, subject to the duration limit):

| Site | Matched URL shapes |
|------|--------------------|
| Instagram | `/reel/`, `/reels/`, `/p/`, `/tv/`, `instagr.am`, `l.instagram.com` |
| YouTube | `/shorts/` only (not `watch?v=`) |
| Facebook | `/reel/`, `/reels/`, `/share/r/`, `fb.watch` |
| TikTok | `/@…/video/…`, `vm.tiktok.com`, `vt.tiktok.com`, `/t/` |
| Twitter/X | status permalinks (`x.com` / `twitter.com` `…/status/123`) and `video.twimg.com/amplify_video/…/*.mp4` |
| Threads | `threads.net` / `threads.com` `/@…/post/…` |
| Bluesky | `bsky.app/profile/…/post/…` |
| Reddit | `reddit.com/r/…/comments/…`, `v.redd.it`, `redd.it` |

Override or extend via `urls.url_patterns` in `config.yaml`.

## Cookies

Export Netscape `cookies.txt` into `data/cookies.txt` when a site requires a session.

## Download

Instagram, TikTok, Shorts, and the other hosts are downloaded **in-process** with **yt-dlp**. The default format selector prefers H.264 + AAC. An existing config that still says `format: bv*+ba/b` is treated as that selector; any other format string is used as written. `video.twimg.com` amplify_video links are already MP4 files, so those are fetched directly over HTTP (redirects must stay on `video.twimg.com`).

ffmpeg re-encodes only when the file is not already H.264 + AAC + yuv420p in MP4 (`download.convert.force: false` on new configs). A re-encode uses High profile, CRF 26, and a bitrate cap. At startup the bot reads the homeserver upload limit (`m.upload.size`) and, if the file is larger, steps quality down instead of failing the upload.

`download.max_duration_seconds` (default 600) refuses longer videos. `0` disables the guard. A post with no video gets a short failure line.

An existing `config.yaml` keeps the convert values it already has (`force: true`, baseline, CRF 23, and so on). New keys (`max_bitrate`, `max_duration_seconds`, `max_upload_bytes`) use the defaults above when they are absent.

`!reel status` prints the installed yt-dlp version. The Docker image bakes yt-dlp in at build time; the Docker workflow rebuilds the image every Monday so that version stays current.

No external download container is required.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

MIT — see [LICENSE](LICENSE).

## Layout

```text
reelgrab/
  __main__.py          # CLI
  config.py            # load / generate config + registration
  default_config.py    # documented default config.yaml
  appservice.py        # mautrix AppService HTTP (push transactions + /health)
  matrix_client.py     # as_token outbound CS API, Olm machine, event dispatch
  crypto_store.py      # sqlite Olm/Megolm store in the data directory
  matrix_content.py    # m.video content, captions, thread relations
  handlers.py          # pipeline
  commands.py          # DM admin commands
  downloader.py        # yt-dlp download + ffmpeg probe/thumbnail
  media_cache.py       # URL → mxc sqlite cache
  urls.py
  state.py
  assets/icon.jpg      # default bot avatar (also images/icon.jpg in repo)
images/icon.jpg
Dockerfile
compose.yaml
```
