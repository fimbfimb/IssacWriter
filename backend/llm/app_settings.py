"""Global LLM settings — ``data/settings.json``.

Holds the ONE Ollama Cloud API key that is shared by every profile whose
base_url points at ollama.com/ollama.ai (no per-model key). The file is read
LIVE on every request: swapping the key in the LLM Profiles tab takes effect
for the very next call, even for long-running runners ( writer, translation,
refine, wizard ) that hold their profile dict in memory.

No caching, no in-memory copy — disk is always the source of truth.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from ..store.project import _read_json, _write_json

SETTINGS_FILE = "settings.json"
OLLAMA_CLOUD_KEY = "ollama_cloud_api_key"


def data_root() -> Path:
    return Path(
        os.environ.get("STORYFORGE_DATA")
        or Path(__file__).resolve().parents[2] / "data"
    )


def settings_path() -> Path:
    return data_root() / SETTINGS_FILE


def load_settings() -> Dict[str, Any]:
    data = _read_json(settings_path())
    return data if isinstance(data, dict) else {}


def get_ollama_cloud_key() -> str:
    """Fresh read of the shared Ollama Cloud key ("" = none set)."""
    return str(load_settings().get(OLLAMA_CLOUD_KEY) or "")


def set_ollama_cloud_key(key: str) -> None:
    settings = load_settings()
    settings[OLLAMA_CLOUD_KEY] = (key or "").strip()
    _write_json(settings_path(), settings)


def is_ollama_cloud_base(base_url: str) -> bool:
    base = (base_url or "").lower()
    return "ollama.com" in base or "ollama.ai" in base