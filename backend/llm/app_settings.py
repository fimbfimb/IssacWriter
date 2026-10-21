"""User-scoped global LLM settings.

Each user has their own settings file (``data/users/<uid>/settings.json``)
holding the ONE Ollama Cloud API key that governs every profile of THAT
user whose base_url points at ollama.com/ollama.ai. The web server pins the
current user (from the X-User-Id header) into a contextvar for every
request — background jobs (writer, translation, refine, wizard) inherit the
owner's identity at task-creation time, so their calls keep resolving the
owner's key. Every profile may still carry its own key, which is the
fallback whenever the user's global key is empty.

Legacy direct library use without a pinned user (scripts, tests) resolves
the old app-wide ``data/settings.json``. The file is read LIVE on every
request: swapping a key takes effect for the very next call. No caching.
"""

from __future__ import annotations

import contextvars
import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..store.project import _read_json, _write_json

SETTINGS_FILE = "settings.json"
OLLAMA_CLOUD_KEY = "ollama_cloud_api_key"

_pinned_user: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_settings_user", default=None
)


def set_request_user(user_id: Optional[str]) -> None:
    """Pin the settings scope for the current request/task context."""
    _pinned_user.set(user_id or None)


def pinned_user() -> Optional[str]:
    return _pinned_user.get()


def data_root() -> Path:
    return Path(
        os.environ.get("STORYFORGE_DATA")
        or Path(__file__).resolve().parents[2] / "data"
    )


def settings_path() -> Path:
    """Per-user settings file when a user is pinned, else the legacy file."""
    uid = _pinned_user.get()
    if uid:
        return data_root() / "users" / str(uid) / SETTINGS_FILE
    return data_root() / SETTINGS_FILE


def load_settings() -> Dict[str, Any]:
    data = _read_json(settings_path())
    return data if isinstance(data, dict) else {}


def get_ollama_cloud_key() -> str:
    """Fresh read of the user's Ollama Cloud key ("" = none set)."""
    return str(load_settings().get(OLLAMA_CLOUD_KEY) or "")


def set_ollama_cloud_key(key: str) -> None:
    settings = load_settings()
    settings[OLLAMA_CLOUD_KEY] = (key or "").strip()
    _write_json(settings_path(), settings)


def is_ollama_cloud_base(base_url: str) -> bool:
    base = (base_url or "").lower()
    return "ollama.com" in base or "ollama.ai" in base