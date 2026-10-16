from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

_VALID_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

COLLECTIONS = ("character_states", "past_events", "future_requirements")


def normalize_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).casefold()


def valid_key(value: Any) -> bool:
    return bool(isinstance(value, str) and _VALID_KEY_RE.fullmatch(value.strip()))


def _copy_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(entry) for entry in entries]


class NarrativeState:
    def __init__(self) -> None:
        self.character_states: List[Dict[str, Any]] = []
        self.past_events: List[Dict[str, Any]] = []
        self.future_requirements: List[Dict[str, Any]] = []
        self._next_ids = {"character_states": 1, "past_events": 1, "future_requirements": 1}
        self._auto_key_counter = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "character_states": self.character_states,
            "past_events": self.past_events,
            "future_requirements": self.future_requirements,
        }

    def to_prompt_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def snapshot(self) -> "NarrativeState":
        clone = NarrativeState()
        clone.character_states = _copy_entries(self.character_states)
        clone.past_events = _copy_entries(self.past_events)
        clone.future_requirements = _copy_entries(self.future_requirements)
        clone._next_ids = dict(self._next_ids)
        clone._auto_key_counter = self._auto_key_counter
        return clone

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NarrativeState":
        state = cls()
        for bucket in COLLECTIONS:
            fallback = "occurred_events" if bucket == "past_events" else bucket
            entries = data.get(bucket, data.get(fallback)) or []
            clean = [e for e in entries if isinstance(e, dict)]
            setattr(state, bucket, clean)
            ids = [e["id"] for e in clean if isinstance(e.get("id"), int)]
            state._next_ids[bucket] = (max(ids) + 1) if ids else 1
        return state

    def commit_snapshot(self, candidate: "NarrativeState") -> None:
        self.character_states = _copy_entries(candidate.character_states)
        self.past_events = _copy_entries(candidate.past_events)
        self.future_requirements = _copy_entries(candidate.future_requirements)
        self._next_ids = dict(candidate._next_ids)
        self._auto_key_counter = candidate._auto_key_counter

    def _generated_key(self, prefix: str, counter: int) -> str:
        return f"{prefix}_{counter}"

    def apply_narrative_ops(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        errors: List[str] = []
        chars = _copy_entries(self.character_states)
        events = _copy_entries(self.past_events)
        reqs = _copy_entries(self.future_requirements)
        next_ids = dict(self._next_ids)
        auto_counter = self._auto_key_counter
        info: Dict[str, Any] = {
            "characters_upserted": [],
            "past_events_added": 0,
            "requirements_added": [],
            "requirements_resolved": [],
            "rejected": [],
        }

        def next_id(collection: str) -> int:
            value = next_ids[collection]
            next_ids[collection] = value + 1
            return value

        def gen_key(prefix: str) -> str:
            nonlocal auto_counter
            auto_counter += 1
            return self._generated_key(prefix, auto_counter)

        upserts = self._array_of_objects(args, "upsert_character_state", errors)
        for item in upserts:
            name = str(item.get("name") or "").strip()
            description = str(item.get("description") or "").strip()
            if not name or not description:
                errors.append("upsert_character_state entry needs name and description")
                info["rejected"].append({"kind": "character", "name": name})
                continue
            key = normalize_name(name)
            existing = next(
                (e for e in chars if normalize_name(e.get("name")) == key), None
            )
            if existing is not None:
                existing["name"] = name
                existing["description"] = description
                info["characters_upserted"].append(
                    {"name": name, "id": existing["id"], "updated": True}
                )
            else:
                new_id = next_id("character_states")
                chars.append({"id": new_id, "name": name, "description": description})
                info["characters_upserted"].append(
                    {"name": name, "id": new_id, "updated": False}
                )

        event_ops = self._array_of_objects(args, "add_past_event", errors)
        for item in event_ops:
            description = str(item.get("description") or "").strip()
            key = item.get("key")
            if not description:
                errors.append("add_past_event entry needs description")
                info["rejected"].append({"kind": "past_event", "key": key})
                continue
            if key is not None and not valid_key(key):
                errors.append(f"add_past_event key {key!r} must be lower snake_case")
                info["rejected"].append({"kind": "past_event", "key": key})
                continue
            if isinstance(key, str):
                key = key.strip()
            if not isinstance(key, str):
                key = gen_key("past_event")
            duplicate = next((e for e in events if e.get("key") == key), None)
            if duplicate is not None:
                duplicate["description"] = description
                continue
            events.append({"id": next_id("past_events"), "key": key, "description": description})
            info["past_events_added"] += 1

        additions = self._array_of_objects(args, "add_future_requirement", errors)
        for item in additions:
            description = str(item.get("description") or "").strip()
            key = item.get("key")
            if not description:
                errors.append("add_future_requirement entry needs description")
                info["rejected"].append({"kind": "requirement", "key": key})
                continue
            if key is not None and not valid_key(key):
                errors.append(f"add_future_requirement key {key!r} must be lower snake_case")
                info["rejected"].append({"kind": "requirement", "key": key})
                continue
            if isinstance(key, str):
                key = key.strip()
            if not isinstance(key, str):
                key = gen_key("req")
            if next((e for e in reqs if e.get("key") == key), None) is not None:
                errors.append(f"future requirement key {key!r} already exists")
                info["rejected"].append({"kind": "requirement", "key": key})
                continue
            reqs.append({"id": next_id("future_requirements"), "key": key, "description": description})
            info["requirements_added"].append({"key": key})

        resolves = self._str_array(args, "resolve_future_requirement", errors)
        for key in resolves:
            entry = next((e for e in reqs if e.get("key") == key), None)
            if entry is None:
                errors.append(f"resolve_future_requirement key {key!r} does not exist")
                info["rejected"].append({"kind": "resolve", "key": key})
                continue
            reqs.remove(entry)
            info["requirements_resolved"].append({"key": key})

        if errors:
            return info, errors

        self.character_states = chars
        self.past_events = events
        self.future_requirements = reqs
        self._next_ids = next_ids
        self._auto_key_counter = auto_counter
        return info, []

    @staticmethod
    def _array_of_objects(args: Dict[str, Any], field: str, errors: List[str]) -> List[Dict[str, Any]]:
        value = args.get(field)
        if value is None:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                errors.append(f"{field} was a serialized string; expected a native JSON array")
                return []
        if not isinstance(value, list):
            errors.append(f"{field} must be a JSON array")
            return []
        result = []
        for item in value:
            if isinstance(item, str):
                result.append({"description": item})
            elif isinstance(item, dict):
                result.append(item)
            else:
                errors.append(f"{field} entries must be objects")
        return result

    @staticmethod
    def _str_array(args: Dict[str, Any], field: str, errors: List[str]) -> List[str]:
        value = args.get(field)
        if value is None:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                errors.append(f"{field} was a serialized string; expected a native JSON array")
                return []
        if not isinstance(value, list):
            errors.append(f"{field} must be a JSON array of keys")
            return []
        result = []
        for item in value:
            if isinstance(item, str) and item.strip():
                result.append(item)
            elif isinstance(item, dict):
                key = item.get("key")
                if isinstance(key, str):
                    result.append(key)
            elif isinstance(item, int):
                result.append(str(item))
            else:
                errors.append(f"{field} entries must be key strings")
        return result

    def set_character(self, name: str, description: str) -> int:
        key = normalize_name(name)
        existing = next(
            (e for e in self.character_states if normalize_name(e.get("name")) == key), None
        )
        if existing is not None:
            existing["description"] = description
            return int(existing["id"])
        new_id = self._next_ids["character_states"]
        self._next_ids["character_states"] += 1
        self.character_states.append({"id": new_id, "name": name, "description": description})
        return new_id

    def remove_character(self, name: str) -> bool:
        key = normalize_name(name)
        existing = next(
            (e for e in self.character_states if normalize_name(e.get("name")) == key), None
        )
        if existing is None:
            return False
        self.character_states.remove(existing)
        return True