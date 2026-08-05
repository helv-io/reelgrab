"""Mutable runtime state (admin DM config) persisted separately from secrets."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("reelgrab.state")


@dataclass
class RuntimeState:
    """Overrides that admins can change via DM without editing config.yaml."""

    auto_download: bool | None = None
    allowed_rooms: list[str] | None = None
    notify_on_failure: bool | None = None
    success_caption: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


class StateStore:
    """Thread-safe YAML-backed runtime overrides."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.state = RuntimeState()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self.state = RuntimeState()
                return
            try:
                with self.path.open(encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                known = {f.name for f in fields(RuntimeState)}
                self.state = RuntimeState(**{k: v for k, v in raw.items() if k in known})
                log.info("loaded runtime state from %s", self.path)
            except Exception as exc:
                log.warning("failed to load state %s: %s", self.path, exc)
                self.state = RuntimeState()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                yaml.safe_dump(
                    self.state.to_dict(), f, default_flow_style=False, sort_keys=True
                )
            tmp.replace(self.path)

    def update(self, **kwargs: Any) -> RuntimeState:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.state, k):
                    setattr(self.state, k, v)
            self.save()
            return self.state
