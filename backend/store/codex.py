from __future__ import annotations

from typing import Any, Dict, List

from ..nstagent.state import normalize_name


def merge_extracted(
    codex: Dict[str, Any], extracted: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Merge LLM-extracted entities into the author's codex.

    Existing entries are never touched: matching happens against an entry's
    name and all its aliases, so a character the author already knows under a
    different spelling stays theirs. Only genuinely new names are appended.
    """
    result: Dict[str, Dict[str, Any]] = {}
    for kind in ("characters", "settings", "lore"):
        extracted_entries = (extracted or {}).get(kind) or []
        if not isinstance(extracted_entries, list):
            continue
        existing = list(codex.get(kind) or [])
        known_names = _known_names(existing)
        added = []
        for entry in extracted_entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            aliases = [
                str(a).strip()
                for a in ((entry or {}).get("aliases") or [])
                if str(a).strip()
            ]
            if normalize_name(name) in known_names or any(
                normalize_name(a) in known_names for a in aliases
            ):
                continue
            new_entry: Dict[str, Any] = {
                "name": name,
                "aliases": aliases,
                "description": str((entry or {}).get("description") or "").strip(),
            }
            role = str((entry or {}).get("role") or "").strip()
            if role and kind == "characters":
                new_entry["role"] = role
            existing.append(new_entry)
            known_names.update({normalize_name(name), *[normalize_name(a) for a in aliases]})
            added.append(name)
        codex[kind] = existing
        result[kind] = {"added": added}
    return result


def _known_names(entries: List[Dict[str, Any]]) -> set[str]:
    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if name:
            names.add(normalize_name(name))
        for alias in entry.get("aliases") or []:
            if alias:
                names.add(normalize_name(alias))
    return names