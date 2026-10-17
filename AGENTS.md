# AGENTS.md: reelgrab

Handoff for agents working in this repo.

## Goal

Matrix **appservice** bot that watches for short-form video links (reels / shorts), downloads them with yt-dlp, and posts `m.video` into the room.

## Package

- Python: `reelgrab` (`python -m reelgrab`)
- Env: `REELGRAB_DATA`, `REELGRAB_DOCKER`
- Default appservice id / bot localpart: `reelgrab`
- Data dir (mautrix-style): `config.yaml` + `registration.yaml` auto-created

## Architecture (do not regress)

- Homeserver **pushes** events to registration `url` (`appservice.address`).
- Outbound CS API uses `as_token`.
- Modern Synapse rejects AS-user `/sync` — never set registration `url: null` and poll `/sync`.
- Encrypted rooms use mautrix's appservice Olm machine (MSC3202 / MSC2409 to-device in the push transaction). Same `url`, no `/sync`.

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

## Rules and what enforces them

A rule that nothing enforces is a wish. Before you push, run `.agent-checks/bin/agent-checks local` (the shared `agent-checks` repo on Helvio's own git server) and the local checks above.

| Rule | Enforced by |
|---|---|
| Every commit is Helvio Pedreschi <helvio88@gmail.com>, nobody else | git refuses to guess an identity on the box; `identity` check on every push |
| No em dashes, no marketing words | `voice` check on added lines, whole tree weekly |
| README and CHANGELOG move with every user-facing change; otherwise `Docs: skip <reason>` in a commit message | `docs` check |
| No built-in model ids; no house names in shipped code | `model-ids` and `private` checks |
| `:latest` is published only from the default branch, never by a version tag | `tests/test_workflow_rules.py` |
| The tag matches the version in `pyproject.toml` and `reelgrab/__init__.py` before images are published | `gate` job in `docker.yml` |
| Workflow files are valid | `actionlint` inside the `agent-checks` job |
| Tests and ruff pass | `CI` workflow on every branch push |
| Work on a branch. Never push to the default branch, never open a pull request unless asked | branch protection on the default branch; Helvio or Rook merges |
| When a project is going for distribution, offer Helvio to protect master | judgment (Rook offers it) |
