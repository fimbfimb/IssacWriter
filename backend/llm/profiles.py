from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional


class ProfileStore:
    def __init__(self, path):
        from pathlib import Path

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self) -> List[Dict[str, Any]]:
        return [self.safe(p) for p in self._load().values()]

    def get(self, profile_id: str) -> Optional[Dict[str, Any]]:
        return self._load().get(profile_id)

    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        wanted = (name or "").strip().casefold()
        if not wanted:
            return None
        for profile in self._load().values():
            if (profile.get("name") or "").strip().casefold() == wanted:
                return profile
        return None

    def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        profiles = self._load()
        profile_id = uuid.uuid4().hex[:8]
        profile = {**data, "id": profile_id}
        profiles[profile_id] = profile
        self._save(profiles)
        return profile

    def update(self, profile_id: str, data: Dict[str, Any]) -> None:
        profiles = self._load()
        if profile_id not in profiles:
            raise KeyError(profile_id)
        merged = {**profiles[profile_id], **data, "id": profile_id}
        profiles[profile_id] = merged
        self._save(profiles)

    def delete(self, profile_id: str) -> None:
        profiles = self._load()
        profiles.pop(profile_id, None)
        self._save(profiles)

    @staticmethod
    def safe(profile: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = dict(profile)
        key = cleaned.get("api_key") or ""
        cleaned["api_key_set"] = bool(key)
        cleaned["api_key"] = "***" if key else ""
        return cleaned