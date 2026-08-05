"""Documented default config.yaml text (mautrix-style). Written when missing."""

from __future__ import annotations

# fmt: off
DEFAULT_CONFIG_YAML = """\
# reelgrab configuration
#
# Like mautrix bridges: keep this file (and registration.yaml) in the data
# directory. On first start the bot creates this file with safe placeholders.
# Edit it, restart, and the bot will mint tokens + registration.yaml.
#
# Data directory resolution (first match wins):
#   1. -c / --config path's parent (if you pass a config file)
#   2. $REELGRAB_DATA
#   3. /data  (Docker default)
#   4. ./data (local dev)

# Homeserver details.
homeserver:
    # Address the bot uses to reach the Client-Server API.
    # On the same Docker network as Synapse this is often http://synapse:8008
    # (or your homeserver container name / internal URL).
    address: http://localhost:8008
    # Server name (domain part of MXIDs), e.g. example.com
    domain: example.com

# Application service identity and tokens.
# Changing id, bot.username, or tokens requires regenerating registration.yaml
# and reloading the file on the homeserver.
appservice:
    # Unique appservice id (must be unique among all appservices on the HS).
    id: reelgrab

    # Bot user on the homeserver. MXID becomes @<username>:<homeserver.domain>
    bot:
        # Localpart only (no @, no domain).
        username: reelgrab
        # Display name set on startup. Empty = leave unchanged after first set.
        displayname: Reelgrab

    # AS <-> HS shared secrets. Leave as "generate" on first real start;
    # the bot will replace them with random values and write registration.yaml.
    # Do not put these in git.
    as_token: generate
    hs_token: generate

    # Whether the homeserver should rate-limit the appservice token.
    # Bridges usually set this false.
    rate_limited: false

    # Bind address for the appservice HTTP server.
    hostname: 0.0.0.0
    port: 29399
    # URL the homeserver uses to reach this process (registration ``url``).
    # Example on a shared Docker network: http://reelgrab:29399
    address: http://reelgrab:29399

# Bot behaviour.
bot:
    # Automatically download when a matching URL appears in a watched room.
    auto_download: true
    # Command to force a download, e.g. "!grab https://..."
    # Legacy alias "!ig ..." is also accepted.
    command_prefix: "!grab"
    # If non-empty, only these room IDs get auto-downloads / force commands.
    # Empty list = every room the bot has joined.
    # Admins can also manage this at runtime via DM: allow / deny / allow clear
    allowed_rooms: []
    # Reply to the triggering message when posting the video or an error.
    reply_to_original: true
    # Caption on successful m.video. Empty string = use filename.
    success_caption: Grabbed with reelgrab
    # Send an m.notice when download/upload fails.
    notify_on_failure: true
    # Max concurrent downloads.
    max_concurrent: 2
    # Do not re-download the same URL in the same room within this window (seconds).
    dedupe_ttl_seconds: 3600
    # After connect, skip historical messages (only handle new events).
    ignore_history: true
    # Join rooms when invited.
    join_on_invite: true
    # MXIDs allowed to use admin/DM config commands (status, allow, auto, …).
    # Example: ["@admin:example.com"]
    admin_users: []
    # Relative paths are resolved against the data directory.
    state_file: runtime_state.yaml

# Download backend.
download:
    # "ytdlp" (recommended) or "metube"
    backend: ytdlp
    # Temp download directory (relative to data dir unless absolute).
    work_dir: downloads
    # Netscape cookies.txt for sites that need a session (often Instagram).
    # Relative to data dir.
    cookies_file: cookies.txt
    # yt-dlp format selector.
    format: bv*+ba/b
    # Remux/merge container when possible.
    merge_output_format: mp4
    # MeTube base URL (only if backend: metube). Use an internal URL if MeTube
    # is reverse-proxied behind auth — do not use a public Authelia URL.
    metube_url: http://metube:8081
    metube_poll_seconds: 5
    metube_timeout_seconds: 600
    # Directory where MeTube writes finished files (mount the MeTube volume here).
    metube_download_dir: /downloads

# URL detection (Instagram short-form by default; extend patterns for other sites).
instagram:
    # Regex fragments matched against URLs found in message bodies.
    # Name is historical; patterns can cover any host you want reelgrab to grab.
    url_patterns:
        - instagram\\.com/reel/
        - instagram\\.com/reels/
        - instagram\\.com/p/
        - instagram\\.com/tv/
        - instagr\\.am/
        - l\\.instagram\\.com/

# Logging.
logging:
    # DEBUG, INFO, WARNING, ERROR
    level: INFO
"""
# fmt: on
