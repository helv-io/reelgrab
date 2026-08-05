# reelgrab

Matrix **appservice bot** that grabs short-form / social videos from links (Instagram Reels and other yt-dlp sites) and posts the file back into the room.

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

### CI secrets / variables

Configure under the repo **Settings → Secrets and variables → Actions**:

| Name | Type | Purpose |
|------|------|---------|
| `DOCKERHUB_USERNAME` | **Variable** (or Secret) | Docker Hub username (`helvio`) |
| `DOCKERHUB_TOKEN` | **Secret** | Docker Hub [access token](https://hub.docker.com/settings/security) |

Workflow: [`.github/workflows/docker.yml`](.github/workflows/docker.yml)

- Push to `main` → build + push tags `latest`, `sha-…`
- Tag `v1.2.3` → also push semver tags `1.2.3`, `1.2`, `1`
- Pull requests → multi-arch build only (no push)

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
| `backend ytdlp\|metube` | download backend |
| `notify on\|off` | failure notices |
| `caption <text>` | success caption |
| `!grab <url>` / `!ig <url>` | force one download |

## Config reference

Every key is documented **in** `config.yaml`. Sections: `homeserver`, `appservice`, `bot`, `download`, `instagram` (URL patterns), `logging`.

Relative paths resolve against the **data directory**.

## Cookies

Export Netscape `cookies.txt` into `data/cookies.txt` when sites require a session (common for Instagram).

## MeTube backend

`download.backend: metube` + internal `download.metube_url`. Mount MeTube downloads at `download.metube_download_dir`.

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
  downloader.py        # yt-dlp / MeTube (strategy)
  urls.py
  state.py
Dockerfile
compose.yaml
```
