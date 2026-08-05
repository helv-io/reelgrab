# AGENTS.md — reelgrab

Handoff for agents working in this repo.

## Goal

Matrix **appservice** bot that watches for short-form / social video links (Instagram and other yt-dlp sites), downloads them, and posts `m.video` into the room. Useful with mautrix-whatsapp portal rooms.

## Package

- Python: `reelgrab` (`python -m reelgrab`)
- Env: `REELGRAB_DATA`, `REELGRAB_DOCKER`
- Default appservice id / bot localpart: `reelgrab`
- Data dir (mautrix-style): `config.yaml` + `registration.yaml` auto-created

## Architecture (do not regress)

- Homeserver **pushes** events to registration `url` (`appservice.address`).
- Outbound CS API uses `as_token`.
- Modern Synapse rejects AS-user `/sync` — never set registration `url: null` and poll `/sync`.

## Layout

```
reelgrab/          # application package
tests/
Dockerfile
compose.yaml       # ./data:/data
data/              # gitignored runtime
AGENTS.md
README.md
```

## Local checks

```bash
python -m unittest discover -s tests -v
# or: pytest tests/ -v
```
