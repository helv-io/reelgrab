"""
mautrix-style configuration: data dir, documented config.yaml, registration.yaml.

Creates missing files. Resolves relative paths against the data directory.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from reelgrab.default_config import DEFAULT_CONFIG_YAML

log_print = print


@dataclass
class HomeserverConfig:
    address: str = "http://localhost:8008"
    domain: str = "example.com"


@dataclass
class AppserviceBotConfig:
    username: str = "reelgrab"
    displayname: str = "Reelgrab"


@dataclass
class AppserviceConfig:
    id: str = "reelgrab"
    bot: AppserviceBotConfig = field(default_factory=AppserviceBotConfig)
    as_token: str = "generate"
    hs_token: str = "generate"
    rate_limited: bool = False
    hostname: str = "0.0.0.0"
    port: int = 29399
    address: str = "http://reelgrab:29399"


@dataclass
class BotConfig:
    auto_download: bool = True
    command_prefix: str = "!grab"
    allowed_rooms: list[str] = field(default_factory=list)
    reply_to_original: bool = True
    success_caption: str = "Grabbed with reelgrab"
    notify_on_failure: bool = True
    max_concurrent: int = 2
    dedupe_ttl_seconds: int = 3600
    ignore_history: bool = True
    join_on_invite: bool = True
    admin_users: list[str] = field(default_factory=list)
    state_file: str = "runtime_state.yaml"


@dataclass
class DownloadConfig:
    backend: str = "ytdlp"
    work_dir: str = "downloads"
    cookies_file: str = "cookies.txt"
    format: str = "bv*+ba/b"
    merge_output_format: str = "mp4"
    metube_url: str = "http://metube:8081"
    metube_poll_seconds: int = 5
    metube_timeout_seconds: int = 600
    metube_download_dir: str = "/downloads"


@dataclass
class UrlPatternsConfig:
    """URL match patterns (config key remains `instagram` for compatibility)."""

    url_patterns: list[str] = field(
        default_factory=lambda: [
            r"instagram\.com/reel/",
            r"instagram\.com/reels/",
            r"instagram\.com/p/",
            r"instagram\.com/tv/",
            r"instagr\.am/",
            r"l\.instagram\.com/",
        ]
    )


# Back-compat name used in YAML section
InstagramConfig = UrlPatternsConfig


@dataclass
class LoggingConfig:
    level: str = "INFO"


@dataclass
class AppConfig:
    """Loaded configuration + resolved paths."""

    homeserver: HomeserverConfig = field(default_factory=HomeserverConfig)
    appservice: AppserviceConfig = field(default_factory=AppserviceConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    instagram: UrlPatternsConfig = field(default_factory=UrlPatternsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    data_dir: Path = field(default_factory=lambda: Path("data"))
    config_path: Path = field(default_factory=lambda: Path("data/config.yaml"))
    registration_path: Path = field(
        default_factory=lambda: Path("data/registration.yaml")
    )

    @property
    def user_id(self) -> str:
        return f"@{self.appservice.bot.username}:{self.homeserver.domain}"

    @property
    def as_token(self) -> str:
        return self.appservice.as_token

    @property
    def hs_token(self) -> str:
        return self.appservice.hs_token

    @property
    def url_patterns(self) -> list[str]:
        return list(self.instagram.url_patterns)

    def resolve_path(self, p: str | Path) -> Path:
        path = Path(p)
        if path.is_absolute():
            return path
        return (self.data_dir / path).resolve()

    @property
    def state_file_path(self) -> Path:
        return self.resolve_path(self.bot.state_file)

    @property
    def work_dir_path(self) -> Path:
        return self.resolve_path(self.download.work_dir)

    @property
    def cookies_file_path(self) -> Path:
        return self.resolve_path(self.download.cookies_file)


def default_data_dir() -> Path:
    env = os.environ.get("REELGRAB_DATA", "").strip()
    if env:
        return Path(env)
    if Path("/data").is_dir() or os.environ.get("REELGRAB_DOCKER") == "1":
        return Path("/data")
    return Path("data")


def resolve_paths(
    config_file: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> tuple[Path, Path, Path]:
    """Returns (data_dir, config_path, registration_path)."""
    if config_file:
        cfg = Path(config_file).expanduser().resolve()
        ddir = cfg.parent
        return ddir, cfg, ddir / "registration.yaml"

    ddir = (
        Path(data_dir).expanduser().resolve()
        if data_dir
        else default_data_dir().resolve()
    )
    return ddir, ddir / "config.yaml", ddir / "registration.yaml"


def _merge_dataclass(dc_cls: type, data: dict[str, Any] | None) -> Any:
    if not data:
        return dc_cls()
    known = {f.name for f in fields(dc_cls)}
    kwargs: dict[str, Any] = {}
    for k, v in data.items():
        if k not in known:
            continue
        kwargs[k] = v
    if dc_cls is AppserviceConfig and "bot" in kwargs and isinstance(kwargs["bot"], dict):
        kwargs["bot"] = _merge_dataclass(AppserviceBotConfig, kwargs["bot"])
    return dc_cls(**kwargs)


def _random_token() -> str:
    return secrets.token_hex(32)


def _is_placeholder_token(value: str) -> bool:
    v = (value or "").strip().lower()
    return v in ("", "generate", "changeme", "replace_me", "null", "none")


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def parse_config_dict(raw: dict[str, Any]) -> AppConfig:
    appservice_raw = raw.get("appservice") or {}
    return AppConfig(
        homeserver=_merge_dataclass(HomeserverConfig, raw.get("homeserver")),
        appservice=_merge_dataclass(AppserviceConfig, appservice_raw),
        bot=_merge_dataclass(BotConfig, raw.get("bot")),
        download=_merge_dataclass(DownloadConfig, raw.get("download")),
        instagram=_merge_dataclass(UrlPatternsConfig, raw.get("instagram")),
        logging=_merge_dataclass(LoggingConfig, raw.get("logging")),
    )


def load_yaml_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_tokens_in_config_file(config_path: Path, as_token: str, hs_token: str) -> None:
    """Replace as_token / hs_token in config text, preserving comments when possible."""
    text = config_path.read_text(encoding="utf-8")

    def repl_token(key: str, value: str, src: str) -> str:
        pattern = rf"^([ \t]*{re.escape(key)}:[ \t]*).*$"
        new_src, n = re.subn(
            pattern,
            rf'\g<1>"{value}"',
            src,
            count=1,
            flags=re.MULTILINE,
        )
        return new_src if n else src

    updated = repl_token("as_token", as_token, text)
    updated = repl_token("hs_token", hs_token, updated)
    if updated == text:
        raw = load_yaml_file(config_path)
        raw.setdefault("appservice", {})
        raw["appservice"]["as_token"] = as_token
        raw["appservice"]["hs_token"] = hs_token
        with config_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)
        return
    config_path.write_text(updated, encoding="utf-8")


def build_registration(cfg: AppConfig) -> dict[str, Any]:
    """Build homeserver appservice registration; ``url`` is ``appservice.address``."""
    domain = cfg.homeserver.domain
    username = cfg.appservice.bot.username
    domain_re = re.escape(domain)
    user_re = re.escape(username)
    as_url = (cfg.appservice.address or "").strip() or None
    return {
        "id": cfg.appservice.id,
        "url": as_url,
        "as_token": cfg.appservice.as_token,
        "hs_token": cfg.appservice.hs_token,
        "sender_localpart": username,
        "rate_limited": bool(cfg.appservice.rate_limited),
        "namespaces": {
            "users": [
                {
                    "regex": f"^@{user_re}:{domain_re}$",
                    "exclusive": True,
                }
            ],
            "aliases": [],
            "rooms": [],
        },
    }


def write_registration(path: Path, cfg: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    reg = build_registration(cfg)
    header = (
        "# Generated by reelgrab — do not edit by hand unless you know why.\n"
        "# Add this file to your homeserver app_service_config_files, then restart the HS.\n"
        f"# Bot MXID: @{cfg.appservice.bot.username}:{cfg.homeserver.domain}\n"
    )
    body = yaml.safe_dump(reg, default_flow_style=False, sort_keys=False)
    path.write_text(header + body, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def config_looks_unedited(cfg: AppConfig) -> list[str]:
    problems: list[str] = []
    if cfg.homeserver.domain in ("example.com", "localhost", ""):
        problems.append(
            f"homeserver.domain is still {cfg.homeserver.domain!r} — set your server_name"
        )
    if "example.com" in cfg.homeserver.address:
        problems.append("homeserver.address still points at example.com")
    if _is_placeholder_token(cfg.appservice.as_token) or _is_placeholder_token(
        cfg.appservice.hs_token
    ):
        problems.append("appservice tokens not generated yet")
    return problems


@dataclass
class BootstrapResult:
    config: AppConfig
    created_config: bool = False
    created_registration: bool = False
    updated_tokens: bool = False
    exit_after: bool = False
    messages: list[str] = field(default_factory=list)


def bootstrap(
    config_file: str | Path | None = None,
    data_dir: str | Path | None = None,
    *,
    generate_registration: bool = False,
    allow_example_domain: bool = False,
) -> BootstrapResult:
    """
    mautrix-style bootstrap:

    1. If config.yaml missing → write documented default, exit_after=True
    2. If tokens are generate → mint tokens, rewrite config, write registration
    3. If registration missing or force → write registration.yaml
    4. If still placeholder domain → exit_after=True with instructions
    """
    ddir, config_path, reg_path = resolve_paths(config_file, data_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    messages: list[str] = []
    created_config = False
    created_registration = False
    updated_tokens = False

    if not config_path.is_file():
        write_default_config(config_path)
        messages.append(f"Wrote {config_path}")
        messages.append(
            "Edit that file (at least homeserver.address, homeserver.domain, "
            "bot.admin_users), then start again."
        )
        cfg = parse_config_dict({})
        cfg.data_dir = ddir
        cfg.config_path = config_path
        cfg.registration_path = reg_path
        return BootstrapResult(
            config=cfg,
            created_config=True,
            exit_after=True,
            messages=messages,
        )

    raw = load_yaml_file(config_path)
    cfg = parse_config_dict(raw)
    cfg.data_dir = ddir
    cfg.config_path = config_path
    cfg.registration_path = reg_path

    if _is_placeholder_token(cfg.appservice.as_token) or _is_placeholder_token(
        cfg.appservice.hs_token
    ):
        as_tok = (
            cfg.appservice.as_token
            if not _is_placeholder_token(cfg.appservice.as_token)
            else _random_token()
        )
        hs_tok = (
            cfg.appservice.hs_token
            if not _is_placeholder_token(cfg.appservice.hs_token)
            else _random_token()
        )
        cfg.appservice.as_token = as_tok
        cfg.appservice.hs_token = hs_tok
        save_tokens_in_config_file(config_path, as_tok, hs_tok)
        updated_tokens = True
        messages.append(f"Generated appservice tokens in {config_path}")

    need_reg = generate_registration or not reg_path.is_file()
    if not need_reg and reg_path.is_file():
        try:
            existing = load_yaml_file(reg_path)
            desired = build_registration(cfg)
            keys = ("url", "as_token", "hs_token", "sender_localpart", "id")
            if any(existing.get(k) != desired.get(k) for k in keys):
                need_reg = True
                messages.append(
                    "registration.yaml out of date vs config — regenerating"
                )
            else:
                want_re = desired["namespaces"]["users"][0]["regex"]
                got_re = (
                    (existing.get("namespaces") or {})
                    .get("users", [{}])[0]
                    .get("regex")
                )
                if got_re != want_re:
                    need_reg = True
                    messages.append(
                        "registration.yaml namespace mismatch — regenerating"
                    )
        except Exception as exc:
            need_reg = True
            messages.append(f"Could not read registration.yaml ({exc}); rewriting")

    if need_reg:
        write_registration(reg_path, cfg)
        created_registration = True
        messages.append(f"Wrote {reg_path}")
        messages.append(
            "Install registration on your homeserver, then restart the homeserver:\n"
            "  app_service_config_files:\n"
            f"    - {reg_path}\n"
            f"  url (homeserver → bot): {cfg.appservice.address}\n"
            "(Path must be readable by the homeserver process.)"
        )

    problems = [p for p in config_looks_unedited(cfg) if "tokens" not in p]
    if problems and not allow_example_domain:
        for p in problems:
            messages.append(f"Config incomplete: {p}")
        messages.append(f"Edit {config_path} and restart.")
        return BootstrapResult(
            config=cfg,
            created_config=created_config,
            created_registration=created_registration,
            updated_tokens=updated_tokens,
            exit_after=True,
            messages=messages,
        )

    cfg.work_dir_path.mkdir(parents=True, exist_ok=True)

    return BootstrapResult(
        config=cfg,
        created_config=created_config,
        created_registration=created_registration,
        updated_tokens=updated_tokens,
        exit_after=False,
        messages=messages,
    )


def load_config(
    path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
) -> AppConfig:
    """Load config after bootstrap. Exits 0 if operator must edit config first."""
    result = bootstrap(config_file=path, data_dir=data_dir)
    for msg in result.messages:
        log_print(msg, file=sys.stderr)
    if result.exit_after:
        sys.exit(0)
    return result.config
