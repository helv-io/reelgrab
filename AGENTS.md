# AGENTS.md — reelgrab

Read this file first when working in this repo. Handoff for agents continuing the project.

## Communication rules (user)

1. **Always perform the best work you can.**
2. **Reply to the user in ASD-EST100.**
3. Prefer clear, complete engineering work over half-measures.

## Project goal

**reelgrab**: Matrix appservice bot that:

1. Hooks into the user’s Synapse / Matrix homeserver.
2. Watches for short-form / social video links (Instagram-first; yt-dlp multi-site capable).
3. Especially useful for WhatsApp → mautrix-whatsapp portal rooms.
4. Downloads via yt-dlp (or MeTube) and posts `m.video` back into the room.

User wants media in Matrix without opening Instagram.

**No production deploy until user says go.**

## Package

- Python package: `reelgrab` (`python -m reelgrab`)
- Env: `REELGRAB_DATA`, `REELGRAB_DOCKER`
- Appservice id / bot localpart default: `reelgrab`
- Data dir mautrix-style: `config.yaml` + `registration.yaml` auto-created

## This host (helv.io) — when deploying

| Item | Value |
|------|--------|
| HS internal | `http://synapse:8008` |
| Domain | `helv.io` |
| Bots mount | `/docker/synapse/bots` → install `reelgrab.yaml` |
| Network | matrix stack / `web` |
| Admin | `@helvio:helv.io` (confirm) |
| MeTube | `http://metube:8081` |
| Synapse max upload | 1G |

## Layout

```
reelgrab/                 # application package
tests/
Dockerfile
compose.yaml              # ./data:/data
data/                     # gitignored runtime
AGENTS.md
README.md
```

## Session status

- [x] Design + implementation
- [x] Appservice + DM commands
- [x] mautrix-style config/registration
- [x] Rename to **reelgrab**
- [ ] Deploy on this host
- [ ] Cookies if needed
- [ ] Live WA IG link test

## First actions for next agent

1. Project lives at `/root/reelgrab` (rename from `instamatrix` is done).
2. `docker compose run --rm reelgrab` → edit config for host → registration → Synapse.
3. DM `@reelgrab:helv.io` → `status`.
4. Keep replies in **ASD-EST100**.
