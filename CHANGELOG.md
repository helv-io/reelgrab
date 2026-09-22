# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-09-22

Listen rules and Twitter/X amplify_video direct MP4s. Config file layout, appservice push, and the `helvio/reelgrab` image name are unchanged.

### Added

- Explicit support for `video.twimg.com/amplify_video/.../*.mp4` (including `?tag=N` and `vid/avc1/{WxH}` or `vid/{WxH}`). Pasting the link downloads the MP4 over HTTP, then runs the usual ffmpeg convert / probe / thumbnail path.
- The amplify pattern is merged in at load time, so an existing `config.yaml` pattern list picks it up without a rewrite.

### Changed

- The bot only reacts when a message contains the exact token `!reel`, or a supported media URL. Bare chat (`ping`, `help`, `!grab`, `!ig`, ...) gets no reply.
- Commands are `!reel help`, `!reel ping`, `!reel allow`, `!reel <url>`, and so on. `!reel` must be a whole token (`!reelgrab` does not match).
- `!reel <supported url>` downloads for anyone in an allowed room, even when auto-download is off. Other http(s) URLs via `!reel` stay admin-only.
- Default `bot.command_prefix` in new configs is `!reel`. The token is fixed in code.

### Tests

- `ping` (including DMs) is ignored.
- `!reel` plus a URL grabs.
- A bare supported URL still grabs.
- The amplify_video example URL is accepted and takes the direct MP4 path.

[0.5.0]: https://github.com/helv-io/reelgrab/releases/tag/v0.5.0

## [0.4.3] — 2026-08-12

Elegance / reliability pass. No config, command, or appservice protocol changes.

### Fixed

- Clean up `job_*` temp directories when a download fails (no more leftover junk
  under `downloads/` after yt-dlp / convert errors).
- Keep strong references to background download tasks so they are not garbage
  collected mid-flight.

### Changed

- Clearer failure logging: expected `DownloadError` vs unexpected exceptions,
  with elapsed time and room/url context; failure notices unchanged.
- Force-grab / URL extraction only accept absolute `http://` / `https://` URLs
  (rejects `file:`, `ftp:`, credentialed URLs, etc.). yt-dlp still fetches the
  intended short-form links.
- Dockerfile installs the package via `pip install .` (still includes ffmpeg).
- Small command-handler DRY (`PUBLIC_COMMANDS` / `ADMIN_COMMANDS`).

### Tests

- Coverage for download cleanup-on-failure, notify on/off failure paths,
  force-grab URL scheme guards, dedupe TTL=0, and preferred/merge-fmt pick.

[0.4.3]: https://github.com/helv-io/reelgrab/releases/tag/v0.4.3

## [0.4.2] — 2026-08-12

First tagged release. Matrix **appservice** bot that watches rooms for short-form
video links, downloads them with **yt-dlp**, and posts `m.video` back into the room.

### Highlights

- **Appservice push model** (mautrix-style): homeserver pushes transactions to
  `appservice.address`; outbound Client-Server API uses `as_token`. No AS-user
  `/sync` polling.
- **Short-form URL patterns** (configurable under `urls`): Instagram Reels,
  YouTube Shorts only, Facebook Reels / `fb.watch`, TikTok short links.
- **In-process yt-dlp** download with optional Netscape `cookies.txt`.
- **ffmpeg re-encode** to mobile-friendly H.264 + AAC MP4 (`download.convert`;
  baseline / yuv420p / faststart, resolution capped).
- **Quiet success path**: on success, posts only the `m.video` (no progress /
  “Grabbed…” notices). On failure, optional `m.notice` with traceback when
  `notify_on_failure` is on.
- **Bot profile**: default avatar uploaded and set on startup
  (`appservice.bot.avatar`); default display name includes a film-frames emoji
  so `m.video` posts are easier to spot in Element.
- **Admin DM commands**: `help`, `ping`, `status`, allow-list, `auto`, `notify`,
  `caption`, `!grab`, and related runtime toggles.
- **Docker**: multi-arch image `helvio/reelgrab` (`linux/amd64`, `linux/arm64`),
  compose + mautrix-style `config.yaml` / `registration.yaml` in the data dir.

### Project hygiene

- MIT `LICENSE` (matches `pyproject.toml`).
- GitHub Actions CI (unittest + ruff) on PRs and `main`.
- Dependabot for pip and GitHub Actions.
- Docker workflow action bumps (`checkout@v7` and current Docker actions).

[0.4.2]: https://github.com/helv-io/reelgrab/releases/tag/v0.4.2
