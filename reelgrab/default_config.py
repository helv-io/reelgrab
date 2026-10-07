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
        displayname: "🎞️ Reelgrab"
        # Avatar set on startup. Matrix profile avatars are mxc:// URIs hosted
        # by your homeserver (federated media). Values:
        #   default  — packaged reelgrab icon (uploaded when the file changes)
        #   <path>   — image file (relative to data dir, or absolute)
        #   empty    — leave avatar unchanged
        avatar: default

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
    # Listen prefix. The bot ignores a message unless it contains this exact
    # whole token or a supported media URL. Bare chat ("ping", "help", ...) is ignored.
    # Change the token here if you want a different command (!reel is the default).
    command_prefix: "!reel"
    # If non-empty, only these room IDs get auto-downloads / force commands.
    # Empty list = every room the bot has joined.
    # Admins can also manage this at runtime via DM: allow / deny / allow clear
    allowed_rooms: []
    # Reply to the triggering message when posting the video or an error.
    reply_to_original: true
    # Optional body text on successful m.video. Empty = a metadata caption
    # ("@uploader: title · source url"), or the filename when metadata is missing.
    # Matrix requires a body. No "Downloading…" / success notices are sent.
    success_caption: ""
    # On failure, react with ❌ and post one short line. The traceback stays in the log.
    notify_on_failure: true
    # Max concurrent downloads.
    max_concurrent: 2
    # Do not post the same URL again in the same room within this window (seconds).
    # A repeat in another room (or after the window) reuses the uploaded file.
    dedupe_ttl_seconds: 3600
    # Skip events whose origin_server_ts is older than this process start.
    # Stops a homeserver backlog replay after downtime from re-grabbing old links.
    # Set false to process those queued events.
    ignore_history: true
    # Join rooms when invited.
    join_on_invite: true
    # MXIDs allowed to use admin/DM config commands (status, allow, auto, …).
    # Example: ["@admin:example.com"]
    admin_users: []
    # Relative paths are resolved against the data directory.
    state_file: runtime_state.yaml
    # SQLite cache of canonical URL → uploaded mxc (shared across rooms, survives restart).
    media_cache_file: media_cache.sqlite

# Download (in-process yt-dlp; ffmpeg for convert / probe / thumbnail).
download:
    # Temp download directory (relative to data dir unless absolute).
    work_dir: downloads
    # Netscape cookies.txt for sites that need a logged-in session.
    # Relative to data dir.
    cookies_file: cookies.txt
    # yt-dlp format selector. Prefers H.264 (avc1) + AAC so compatible files
    # can be posted without a re-encode. The legacy value bv*+ba/b is treated
    # as this selector. Set any other string to use it verbatim.
    # Quote the value: a leading * is a YAML alias.
    format: "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*[vcodec^=h264]+ba[acodec^=mp4a]/bv*+ba/b"
    # Remux container after yt-dlp merge (before convert step).
    merge_output_format: mp4
    # Refuse a download longer than this many seconds (0 = no limit).
    max_duration_seconds: 600
    # Optional upload ceiling in bytes. 0 = ask the homeserver (m.upload.size)
    # and step quality down until the file fits, instead of failing the upload.
    max_upload_bytes: 0
    # Re-encode when the source is not already H.264 + AAC + yuv420p in MP4.
    convert:
        enabled: true
        # true = always re-encode; false = skip when already H.264/AAC/yuv420p MP4
        force: false
        video_codec: libx264
        audio_codec: aac
        audio_bitrate: 128k
        video_preset: veryfast
        video_crf: 26
        pixel_format: yuv420p
        profile: high
        level: "4.0"
        # Peak video bitrate during re-encode. Empty string disables the cap.
        max_bitrate: 2500k
        bufsize: 5000k
        # Scale down if larger (0 = no limit). Aspect ratio kept; even dims.
        max_width: 1280
        max_height: 1280
        movflags: "+faststart"
        # Extra ffmpeg args before the output path, e.g. ["-bf", "0"]
        extra_args: []
        timeout_seconds: 600

# URL detection. Defaults cover short-form hosts plus status/post links
# (X, Instagram posts, Threads, Bluesky, Reddit). Long-form YouTube watch
# pages are not included. !reel <url> can still fetch any other http(s) URL.
urls:
    # Regex fragments matched against URLs found in message bodies.
    url_patterns:
        # Instagram Reels
        - instagram\\.com/reel/
        - instagram\\.com/reels/
        - instagr\\.am/
        - l\\.instagram\\.com/
        # YouTube Shorts
        - youtube\\.com/shorts/
        - youtube\\.com/short/
        - m\\.youtube\\.com/shorts/
        # Facebook Reels
        - facebook\\.com/reel/
        - facebook\\.com/reels/
        - facebook\\.com/share/r/
        - fb\\.watch/
        - fb\\.com/reel/
        - fb\\.com/reels/
        # TikTok
        - tiktok\\.com/.*/video/
        - tiktok\\.com/t/
        - vm\\.tiktok\\.com/
        - vt\\.tiktok\\.com/
        # Twitter/X amplify_video CDN direct MP4
        - video\\.twimg\\.com/amplify_video/.+\\.mp4
        # Instagram posts and IGTV
        - instagram\\.com/p/
        - instagram\\.com/tv/
        # X / Twitter status permalinks
        - https?://(?:www\\.|mobile\\.)?(?:twitter\\.com|x\\.com)/[^/?#\\s]+/status/\\d+
        # Threads
        - https?://(?:www\\.)?threads\\.(?:net|com)/(?:@|t/)
        # Bluesky
        - https?://(?:www\\.)?bsky\\.app/profile/[^/?#\\s]+/post/
        # Reddit
        - https?://v\\.redd\\.it/
        - https?://(?:www\\.|old\\.|m\\.)?reddit\\.com/r/[^/?#\\s]+/comments/
        - https?://(?:www\\.)?redd\\.it/[A-Za-z0-9]+

# End-to-end encryption for encrypted rooms and encrypted DMs.
# Device keys are stored in the data directory (crypto.sqlite, crypto_pickle.key,
# mx-state.json). The existing data volume is enough; no extra mount is required.
# registration.yaml gains org.matrix.msc3202: true. The homeserver must enable
# MSC3202 transaction extensions and MSC2409 to-device messages, then restart.
# url stays set — the bot does not poll /sync.
# Startup logs in (m.login.application_service) to create the device, then
# uploads keys with both user_id and device_id. If that fails, unencrypted
# rooms keep working and sends do not carry a device id.
encryption:
    enabled: true

# Logging.
logging:
    # DEBUG, INFO, WARNING, ERROR
    level: INFO
"""
# fmt: on
