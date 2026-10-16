"""Sequel preparation: extract Band-1 insights and create Band-2 books.

Feature 1 of geplannednderungenV1.md. Two steps:

1. `prepare_sequel` — one extraction LLM call (temperature 0, JSON only) over
   the ending of Band 1 (last chapters, char-budgeted) + the codex character
   list. Yields: ending summary, per-character alive/tot/unknown with textual
   proof, open threads (text analysis; the narrative state is NOT transferred,
   future_requirements may seed the threads), title + story prompt draft.
2. `create_sequel` — builds the Band-2 book via the normal create_book flow:
   settings codex copied verbatim, characters limited to the confirmed living
   ones (+ aliases; dead ones only as flashback candidates when asked), author
   notes carry ending + threads + idea (the writer reads them per chapter).
   Band 1 itself is never modified.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .llm.provider import LlmClient, LlmProfile, extract_json, strip_inline_thinking

EXTRACTION_CHAR_BUDGET = 12_000
EXTRACTION_TEMPERATURE = 0.0


def ending_source_text(chapters: List[Dict[str, Any]], budget: int = EXTRACTION_CHAR_BUDGET) -> str:
    """The ending: last chapters down to ~budget chars, cut at paragraph edges."""
    blocks: List[str] = []
    size = 0
    for chapter in sorted(chapters, key=lambda c: int(c["id"]), reverse=True):
        content = (chapter.get("content") or "").strip()
        if not content:
            continue
        remaining = budget - size
        if remaining <= 0:
            break
        header = f"[Kapitel {int(chapter['id']) + 1}: {chapter.get('name') or ''}]\n"
        if len(content) + len(header) > remaining:
            if remaining - len(header) >= 200:
                cut = content[-(remaining - len(header)) :]
                slice_at = cut.find("\n\n")
                if 0 <= slice_at < len(cut) - 120:  # keep a meaningful minimum
                    cut = cut[slice_at + 2 :]
                blocks.insert(0, header + cut)
            break  # no meaningful excerpt fits anymore either way
        blocks.insert(0, header + content)
        size += len(content) + len(header) + 2
    return "\n\n".join(blocks).strip()


def codex_characters(book: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in (book.get("codex") or {}).get("characters") or [] if (c.get("name") or "").strip()]


def empty_proposal(book: Dict[str, Any], codex_names: List[str], idea: str) -> Dict[str, Any]:
    return {
        "summary_ending": "",
        "open_threads": [],
        "characters": [
            {"name": name, "codex_id": "", "alive": None, "reason": ""}
            for name in codex_names
        ],
        "proposed_title": f"{book.get('title', 'Band 1')} — Band 2",
        "story_prompt": idea.strip(),
        "author_notes": (
            f"Band 1 endete so: (Extraktion fehlgeschlagen — bitte von Hand eintragen)\n\n"
            f"Offene Fäden: —\n\nIdee Band 2: {idea.strip()}"
        ),
    }


def _clean(text: Any, limit: int = 1200) -> str:
    return strip_inline_thinking(str(text or "")).strip()[:limit]


def extract_messages(source_text: str, codex: List[Dict[str, Any]], idea: str, title: str) -> List[Dict[str, Any]]:
    names = "\n".join(
        f"- {entry.get('name', '')}" + (f" (Rolle: {entry.get('role', '')})" if entry.get("role") else "")
        for entry in codex
    ) or "— (kein Codex)"
    return [
        {
            "role": "system",
            "content": "Du extrahierst aus einem Roman-Ende Fakten und antwortest NUR mit JSON.",
        },
        {
            "role": "user",
            "content": (
                f"Du bekommst das Ende eines Romans (letzte Kapitel, Titel: {title}) "
                "und sein Figurenverzeichnis.\n\n"
                "Figuren im Codex:\n" + names + "\n\n"
                "Idee des Autors für Band 2 (Handlungsgerüst Band 2, nicht Band 1):\n"
                + (idea.strip() or "—") + "\n\n"
                "Roman-Ende:\n---\n" + source_text + "\n---\n\n"
                "Erzeuge NUR JSON:\n"
                "{\n"
                '  "summary_ending": "5-8 Sätze, wie Band 1 endet",\n'
                '  "open_threads": ["offene Fäden, die Band 2 aufgreifen könnte"],\n'
                '  "characters": [{"name": "<Codexname>", "alive": true|false|null,\n'
                '                  "reason": "Beleg aus dem Text"}],\n'
                '  "proposed_title": "Titelvorschlag für Band 2",\n'
                '  "story_prompt": "Story-Prompt für Band 2: Band-1-Prämisse verdichtet + Auflösung von Band 1 + die Idee des Autors als neuer Hauptkonflikt"\n'
                "}\n\n"
                "Regeln: alive=null, wenn der Text es nicht hergibt. Nichts erfinden, das im Text "
                "nicht steht. Die Idee des Autors ist Handlungsgerüst Band 2, nicht Band 1."
            ),
        },
    ]


def normalize_payload(
    payload: Any, book: Dict[str, Any], codex: List[Dict[str, Any]], idea: str
) -> Dict[str, Any]:
    """Shape the extraction into the proposal contract; broken output → fallback."""
    if not isinstance(payload, dict):
        return empty_proposal(book, [c.get("name", "") for c in codex], idea)
    characters_raw = {}
    for row in payload.get("characters") or []:
        if isinstance(row, dict) and (str(row.get("name", "")).strip()):
            characters_raw.setdefault(str(row["name"]).strip(), row)
    characters: List[Dict[str, Any]] = []
    for entry in codex:
        name = (entry.get("name") or "").strip()
        row = characters_raw.get(name) or next(
            (
                r
                for key, r in characters_raw.items()
                if key.casefold() in ((entry.get("name") or "").casefold(),
                                      *(a.casefold() for a in (entry.get("aliases") or [])))
            ),
            None,
        )
        alive = row.get("alive") if row else None
        if isinstance(alive, str):
            lowered = alive.strip().lower()
            alive = True if lowered in ("true", "yes", "ja", "lebt") else (False if lowered in ("false", "no", "nein", "tot") else None)
        if not isinstance(alive, bool):
            alive = None
        characters.append(
            {
                "name": name,
                "codex_id": (entry.get("id") or ""),
                "alive": alive,
                "reason": (row.get("reason") if row else "") or "",
            }
        )
    threads = payload.get("open_threads")
    if not isinstance(threads, list):
        threads = []
    threads = [_clean(t, 240) for t in threads if _clean(t, 240)]
    title = _clean(payload.get("proposed_title") or f"{book.get('title', 'Band 1')} — Band 2", 140)
    story = _clean(payload.get("story_prompt") or idea, 4000)
    summary = _clean(payload.get("summary_ending") or "", 2400)
    notes = (
        f"Band 1 endete so: {summary or '(bitte ergänzen)'}\n\n"
        "Offene Fäden: "
        + (("; ".join(threads) if threads else "—"))
        + f"\n\nIdee Band 2: {idea.strip()}"
    )
    return {
        "summary_ending": summary,
        "open_threads": threads,
        "characters": characters,
        "proposed_title": title,
        "story_prompt": story,
        "author_notes": notes,
    }


async def prepare_sequel(
    profile: Dict[str, Any], book: Dict[str, Any], chapters: List[Dict[str, Any]], idea: str, unload: bool = True
) -> Dict[str, Any]:
    """LLM extraction → proposal. Extraction failure → a fully empty proposal."""
    codex = codex_characters(book)
    source_text = ending_source_text(chapters)
    try:
        client = LlmClient(LlmProfile(**profile))
        try:
            response = await client.chat(
                extract_messages(source_text, codex, idea, book.get("title") or ""),
                temperature=EXTRACTION_TEMPERATURE,
            )
            payload = extract_json(response.content or "")
            proposal = normalize_payload(payload, book, codex, idea)
        finally:
            await client.aclose()
    except Exception as exc:
        proposal = empty_proposal(book, [c.get("name", "") for c in codex], idea)
        proposal["extraction_error"] = f"Extraktion fehlgeschlagen ({exc}) — bitte von Hand ergänzen."
    if unload:
        from .translation.ensemble import unload_ollama_profile

        try:
            await unload_ollama_profile(profile)
        except Exception:
            pass
    return proposal


def create_sequel_book(
    store: Any,
    book: Dict[str, Any],
    proposal: Dict[str, Any],
    keep_character_names: List[str],
    idea: str,
    target_words: Optional[int] = None,
    include_dead_as_flashbacks: bool = False,
) -> Dict[str, Any]:
    keep_set = {n.strip().casefold() for n in keep_character_names if n.strip()}
    codex = book.get("codex") or {}
    old_characters = codex.get("characters") or []
    alive_rows = {
        str(row.get("name") or "").strip().casefold(): row
        for row in proposal.get("characters") or []
    }

    new_characters: List[Dict[str, Any]] = []
    kept_keys: set = set()
    for entry in old_characters:
        name = (entry.get("name") or "").strip()
        if not name or name.casefold() not in keep_set:
            continue
        new_characters.append({**entry})
        kept_keys.add(name.casefold())
    # names the user confirmed but the codex never tracked (e.g. added by hand)
    for keep_name in keep_character_names:
        name = keep_name.strip()
        if not name or name.casefold() in kept_keys:
            continue
        row = alive_rows.get(name.casefold()) or {}
        new_characters.append(
            {"name": name, "aliases": [], "description": str(row.get("reason") or ""), "role": "", "color": ""}
        )
    # dead-as-flashback handling: additionally copy the dead ones (marked)
    if include_dead_as_flashbacks:
        for entry in old_characters:
            name = (entry.get("name") or "").strip()
            row = alive_rows.get(name.casefold())
            if name and row and row.get("alive") is False and name.casefold() not in keep_set:
                note = dict(entry)
                note["description"] = (
                    ((entry.get("description") or "") + " [Band-1-Verstorben — nur als Flashback-Kandidat]").strip()
                )
                new_characters.append(note)

    summary = str(proposal.get("summary_ending") or "").strip()
    threads = [str(t).strip() for t in proposal.get("open_threads") or [] if str(t).strip()]
    notes_parts = [f"Band 1 endete so: {summary or '(bitte ergänzen)'}"]
    if threads:
        notes_parts.append("Offene Fäden:\n" + "\n".join(f"- {t}" for t in threads))
    notes_parts.append(f"Idee Band 2: {idea.strip() or '—'}")

    story_prompt = str(proposal.get("story_prompt") or "").strip() or idea.strip()
    title = str(proposal.get("proposed_title") or "").strip() or f"{book.get('title', 'Band 1')} — Band 2"
    words = int(target_words or book.get("target_words") or 10000)
    new_book = store.create_book(
        title=title,
        story_prompt=story_prompt,
        language=book.get("language") or "en",
        target_words=words,
        genre_notes=book.get("genre_notes") or "",
        codex={
            "characters": new_characters,
            "settings": list(codex.get("settings") or []),
            "lore": list(codex.get("lore") or []),
        },
    )
    store.set_author_notes(new_book["id"], "\n\n".join(notes_parts))
    return store.load_book(new_book["id"])