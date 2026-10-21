from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def _utc_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UserStore:
    """Intranet identity registry — no credentials.

    Users pick their name on the login screen; the id is then sent as the
    ``X-User-Id`` header. This store exists purely to partition data (books,
    LLM profiles) per person, not to protect anything.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"users": {}, "default_id": None}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"users": {}, "default_id": None}
        if not isinstance(data, dict):
            return {"users": {}, "default_id": None}
        data.setdefault("users", {})
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> List[Dict[str, Any]]:
        rows = list(self._load()["users"].values())
        rows.sort(key=lambda u: (u.get("name") or "").casefold())
        return rows

    def get(self, user_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not user_id:
            return None
        return self._load()["users"].get(str(user_id))

    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        wanted = (name or "").strip().casefold()
        if not wanted:
            return None
        for user in self._load()["users"].values():
            if (user.get("name") or "").strip().casefold() == wanted:
                return user
        return None

    def default_id(self) -> Optional[str]:
        return self._load().get("default_id")

    def create(self, name: str) -> Dict[str, Any]:
        clean = (name or "").strip()
        if not clean:
            raise ValueError("user name must not be empty")
        data = self._load()
        for user in data["users"].values():
            if (user.get("name") or "").casefold() == clean.casefold():
                raise ValueError(f"user '{user.get('name')}' already exists")
        user = {"id": uuid.uuid4().hex[:10], "name": clean, "created": _utc_now()}
        data["users"][user["id"]] = user
        if not data.get("default_id"):
            data["default_id"] = user["id"]
        self._save(data)
        return user

    def ensure_default(self) -> Dict[str, Any]:
        """Return (creating if needed) the fallback user for header-less/tomorrow-legacy requests."""
        data = self._load()
        default_id = data.get("default_id")
        if default_id and default_id in data["users"]:
            return data["users"][default_id]
        if data["users"]:
            first = sorted(data["users"].values(), key=lambda u: u.get("created") or "")[0]
            data["default_id"] = first["id"]
            self._save(data)
            return first
        return self.create("Default")