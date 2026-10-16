"""Layout module: visual profiles, deterministic illustration positions, scene prompts.

data/<book>/layout.json holds:

- style_directive: one free-text line, goes into every composed image prompt
- words_per_page / images_every_n_pages: the "page" definition (EPUBs and
  readers have no fixed pages, so the app defines one: default 250 words)
- characters: [{name, visual}] — human-editable visual profiles per codex character
- illustrations: [{id, chapter_id, paragraph_index, anchor_word_offset, scene,
  location, setting, prompt, image_path, ...}] — image slots anchored to a
  (chapter, paragraph) pair, plus optional cover_image_path.

Positions are computed over accumulated word counts of the whole book, never
LLM-decided. Scene texts (scene/location/setting) come from a model that reads
about half a page of context around the anchor; the final image prompt is
composed deterministically from those fields + character visuals + style.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..llm.provider import LlmClient, LlmProfile, extract_json, strip_inline_thinking
from ..translation.ensemble import unload_ollama_profile
from .cleanup import cleanup_paragraphs

DEFAULT_WORDS_PER_PAGE = 250
DEFAULT_IMAGES_EVERY_N_PAGES = 5
CONTEXT_HALF_WINDOW_WORDS = 150  # ± ~half a page around the anchor
SIMILAR_SCENE_WORD_GAP = 500  # warn when two slots sit closer than ~2 pages

# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def layout_path(data_root: Path, book_id: str) -> Path:
    return Path(data_root) / str(book_id) / "layout.json"


def default_layout() -> Dict[str, Any]:
    return {
        "style_directive": "",
        "words_per_page": DEFAULT_WORDS_PER_PAGE,
        "images_every_n_pages": DEFAULT_IMAGES_EVERY_N_PAGES,
        "cover_image_path": "",
        "characters": [],
        "illustrations": [],
    }


def load_layout(data_root: Path, book_id: str) -> Dict[str, Any]:
    path = layout_path(data_root, book_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_layout()
    if not isinstance(data, dict):
        return default_layout()
    layout = default_layout()
    for key in (
        "style_directive",
        "words_per_page",
        "images_every_n_pages",
        "cover_image_path",
        "characters",
        "illustrations",
    ):
        if key in data and data[key] is not None:
            layout[key] = data[key]
    layout["illustrations"] = [entry for entry in layout["illustrations"] if isinstance(entry, dict)]
    layout["characters"] = [entry for entry in layout["characters"] if isinstance(entry, dict)]
    return layout


def save_layout(data_root: Path, book_id: str, layout: Dict[str, Any]) -> None:
    path = layout_path(data_root, book_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def next_illustration_id(layout: Dict[str, Any]) -> str:
    highest = 0
    for entry in layout.get("illustrations") or []:
        match = re.fullmatch(r"i(\d+)", str(entry.get("id") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"i{highest + 1:02d}"


# --------------------------------------------------------------------------- #
# deterministic position math
# --------------------------------------------------------------------------- #


def chapter_paragraphs(chapter: Dict[str, Any]) -> Tuple[List[str], List[int]]:
    """(cleaned paragraphs, per-paragraph word counts) for one chapter."""
    paragraphs = cleanup_paragraphs(chapter.get("content") or "")
    return paragraphs, [max(1, len(paragraph.split())) for paragraph in paragraphs]


def book_word_positions(chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per chapter: cleaned paragraphs, words, and the accumulated book offset.

    `start_offset` is the cumulative word count BEFORE this chapter's first
    paragraph — the whole book's text as one continuous word line.
    """
    rows: List[Dict[str, Any]] = []
    offset = 0
    for chapter in sorted(chapters, key=lambda c: int(c["id"])):
        paragraphs, widths = chapter_paragraphs(chapter)
        rows.append(
            {
                "chapter_id": int(chapter["id"]),
                "chapter_name": chapter.get("name") or "",
                "paragraphs": paragraphs,
                "widths": widths,
                "words": sum(widths),
                "start_offset": offset,
            }
        )
        offset += sum(widths)
    return rows


def slot_for_target(
    rows: List[Dict[str, Any]], target_offset: int
) -> Optional[Dict[str, Any]]:
    """Locate (chapter_id, paragraph_index) that first crosses `target_offset`.

    Positions never sit on a chapter heading and not as the very first
    paragraph of a chapter; a single-paragraph chapter is skipped forward.
    """
    passed = 0
    for row in rows:
        chapter_words = row["words"]
        if passed + chapter_words < target_offset:
            passed += chapter_words
            continue
        # target falls inside this chapter
        local_target = max(0, target_offset - passed)
        acc = 0
        chosen: Optional[int] = None
        for index, width in enumerate(row["widths"]):
            acc += width
            chosen = index
            if acc >= max(1, local_target):
                break
        if chosen is None:
            passed += chapter_words
            continue
        # not the first paragraph when the chapter has more to offer
        if chosen == 0 and len(row["widths"]) > 1:
            chosen = 1
        return {
            "chapter_id": row["chapter_id"],
            "paragraph_index": chosen,
            "anchor_word_offset": row["start_offset"] + sum(row["widths"][: chosen + 1]),
        }
    return None


def anchor_valid_for(rows: List[Dict[str, Any]], entry: Dict[str, Any]) -> bool:
    """An illustration is anchored when its (chapter, paragraph) still exists."""
    raw_id = entry.get("chapter_id")
    if raw_id is None:
        return False
    row = next((r for r in rows if r["chapter_id"] == int(raw_id)), None)
    if row is None:
        return False
    index = entry.get("paragraph_index")
    return isinstance(index, int) and 0 <= index < len(row["paragraphs"])


def compute_positions(chapters: List[Dict[str, Any]], layout: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute slot targets, keep existing records anchored, add/retire slots.

    Returns the updated layout dict. Never deletes data: over-numerous or
    content-carrying records stay with `retired: true`; drifting anchors get
    `anchor_valid: false` (the UI badge "Position unklar").
    """
    try:
        words_per_page = max(50, int(layout.get("words_per_page") or DEFAULT_WORDS_PER_PAGE))
    except (TypeError, ValueError):
        words_per_page = DEFAULT_WORDS_PER_PAGE
    try:
        every_n = max(1, int(layout.get("images_every_n_pages") or DEFAULT_IMAGES_EVERY_N_PAGES))
    except (TypeError, ValueError):
        every_n = DEFAULT_IMAGES_EVERY_N_PAGES
    spacing = max(1, words_per_page * every_n)

    rows = book_word_positions(chapters)
    total_words = sum(row["words"] for row in rows)

    kept: List[Dict[str, Any]] = []
    for entry in [e for e in layout.get("illustrations") or [] if e.get("id")]:
        copy = dict(entry)
        copy["anchor_valid"] = anchor_valid_for(rows, copy)
        kept.append(copy)

    used_targets: List[int] = []
    for offset in range(spacing, total_words + 1, spacing):
        slot = slot_for_target(rows, offset)
        if slot is None:
            continue
        occupied = next(
            (
                e
                for e in kept
                if not e.get("retired")
                and e.get("chapter_id") == slot["chapter_id"]
                and e.get("paragraph_index") == slot["paragraph_index"]
            ),
            None,
        )
        if occupied is not None:
            used_targets.append(offset)
            continue
        # an empty, not-yet-described record near the target snaps onto the slot
        snapped = next(
            (
                e
                for e in kept
                if not e.get("retired")
                and not (e.get("scene") or "").strip()
                and not e.get("image_path")
                and e.get("chapter_id") == slot["chapter_id"]
                and abs(int(e.get("anchor_word_offset") or 0) - slot["anchor_word_offset"])
                <= max(200, spacing // 2)
            ),
            None,
        )
        if snapped is not None:
            snapped["paragraph_index"] = slot["paragraph_index"]
            snapped["anchor_word_offset"] = slot["anchor_word_offset"]
            snapped["chapter_id"] = slot["chapter_id"]
            snapped["anchor_valid"] = True
            snapped.pop("retired", None)
            used_targets.append(offset)
            continue
        kept.append({**slot, "id": next_illustration_id({"illustrations": kept})})
        used_targets.append(offset)

    # retire records that no longer fit the slot grid and carry no work yet
    for entry in kept:
        if entry.get("retired") or entry.get("scene") or entry.get("image_path"):
            continue
        offset = int(entry.get("anchor_word_offset") or 0)
        near_any = any(abs(t - offset) <= spacing // 2 for t in used_targets)
        if not near_any:
            entry["retired"] = True

    # flag near-duplicates (same scene guessed at two close slots)
    active = [
        (int(e.get("anchor_word_offset") or 0), e)
        for e in kept
        if not e.get("retired") and e.get("chapter_id") is not None
    ]
    active.sort(key=lambda pair: pair[0])
    for a, b in zip(active, active[1:]):
        if b[0] - a[0] < words_per_page * 2:
            a[1]["near_duplicate"] = True
            b[1]["near_duplicate"] = True

    layout["illustrations"] = kept
    layout["book_stats"] = {
        "total_words": total_words,
        "spacing_words": spacing,
        "per_chapter": [
            {
                "chapter_id": row["chapter_id"],
                "chapter_name": row["chapter_name"],
                "words": row["words"],
                "start_offset": row["start_offset"],
                "paragraphs": len(row["paragraphs"]),
            }
            for row in rows
        ],
    }
    return layout


# --------------------------------------------------------------------------- #
# visual profiles (extraction)
# --------------------------------------------------------------------------- #


def alias_pattern(name: str) -> Optional["re.Pattern[str]"]:
    name = (name or "").strip()
    if not name:
        return None
    try:
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    except re.error:
        return None


def passage_chunks_for(
    chapter: Dict[str, Any], patterns: List["re.Pattern[str]"], window_chars: int = 1800
) -> List[str]:
    """Chapter excerpts around occurrences of a character's name/alias."""
    paragraphs = cleanup_paragraphs(chapter.get("content") or "")
    full = "\n\n".join(paragraphs)
    hits: List[int] = []
    for pattern in patterns:
        for match in pattern.finditer(full):
            hits.append(match.start())
    if not hits:
        return []
    collected: List[Tuple[int, int]] = []
    for hit in sorted(hits):
        span = (max(0, hit - window_chars // 2), min(len(full), hit + window_chars // 2))
        if collected and span[0] <= collected[-1][1] + 200:
            collected[-1] = (collected[-1][0], max(collected[-1][1], span[1]))
        else:
            collected.append(span)
    return [full[begin:end].strip() for begin, end in collected[:6]]


def visual_extract_messages(candidates: List[Dict[str, Any]], source_chunks: List[str]) -> List[Dict[str, Any]]:
    listing = "\n".join(
        f"- {entry['name']}" + (f" (auch: {', '.join(entry['aliases'])})" if entry.get("aliases") else "")
        for entry in candidates
    )
    return [
        {
            "role": "system",
            "content": "You extract visual character descriptions from novel prose and answer ONLY with JSON.",
        },
        {
            "role": "user",
            "content": (
                "Roman-Passagen (Quelle) und die Figurenliste aus dem Codex.\n\n"
                "Figuren:\n" + listing + "\n\n"
                "Passagen:\n---\n" + "\n---\n".join(source_chunks) + "\n---\n\n"
                "Erzeuge für JEDERE Figur der Liste ein visuelles Profil aus dem Text, als Nacherzählung "
                "(kein Bild-Prompt, keine Stichwortliste, kein erfundenes Detail): Aussehen, Alter, "
                "Kleidung, Besonderheiten, typische Haltung — nur was die Passagen hergeben.\n\n"
                'JSON: {"characters": [{"name": "<Codexname>", "visual": "<3-6 S\u00e4tze>"}, ...]}\n'
                "Fehlt eine Figur, \u00fcbergib sie mit leerem visual-String."
            ),
        },
    ]


def parse_visuals(raw: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Payload → per-candidate visual strings (fallback: empty string)."""
    visuals: Dict[str, str] = {}
    try:
        payload = extract_json(raw or "")
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for row in payload.get("characters") or []:
            if isinstance(row, dict) and row.get("name") and row.get("visual"):
                visuals[str(row["name"]).strip()] = strip_inline_thinking(str(row["visual"])).strip()
    results: List[Dict[str, Any]] = []
    for entry in candidates:
        visual = visuals.get(entry["name"], "")
        if not visual:
            # model may have answered with an alias
            for key, value in visuals.items():
                if key.lower() in [a.lower() for a in [entry["name"], *entry.get("aliases", [])]]:
                    visual = value
                    break
        results.append({"name": entry["name"], "visual": visual[:1200]})
    return results


# --------------------------------------------------------------------------- #
# scene descriptions (LLM) + prompt composition
# --------------------------------------------------------------------------- #


def context_window(
    rows: List[Dict[str, Any]], chapter_id: int, paragraph_index: int, half_window: int = CONTEXT_HALF_WINDOW_WORDS
) -> Optional[str]:
    row = next((r for r in rows if r["chapter_id"] == int(chapter_id)), None)
    if row is None:
        return None
    paragraphs = row["paragraphs"]
    if not 0 <= int(paragraph_index) < len(paragraphs):
        return None
    words_before = sum(row["widths"][:paragraph_index])
    begin = paragraph_index
    back_words = 0
    while begin > 0 and back_words < half_window:
        begin -= 1
        back_words += row["widths"][begin]
    end = paragraph_index
    fwd_words = 0
    while end + 1 < len(paragraphs) and fwd_words < half_window:
        end += 1
        fwd_words += row["widths"][end]
    return "\n\n".join(paragraphs[begin : end + 1])


def character_visuals_in(
    text: str, book: Dict[str, Any], layout: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Codex characters whose name/alias appears in `text`, with their visual."""
    codex = book.get("codex") or {}
    visual_by_name = {(c.get("name") or "").strip(): (c.get("visual") or "").strip() for c in layout.get("characters") or []}
    found: List[Dict[str, Any]] = []
    for entry in codex.get("characters") or []:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        patterns = [alias_pattern(name)] + [alias_pattern(a) for a in entry.get("aliases") or []]
        if any(p and p.search(text) for p in patterns if p):
            visual = visual_by_name.get(name) or ""
            if visual:
                found.append({"name": name, "visual": visual})
    return found


def compose_prompt(
    illustration: Dict[str, Any], style_directive: str, characters: List[Dict[str, Any]]
) -> str:
    parts: List[str] = [(illustration.get("scene") or "").strip() or "Szene offen"]
    location = (illustration.get("location") or "").strip()
    if location:
        parts.append(f"am Ort: {location}")
    setting = (illustration.get("setting") or "").strip()
    if setting:
        parts.append(setting)
    if characters:
        visuals = "; ".join(f"{c['name']}: {c['visual']}" for c in characters[:4])
        parts.append(f"Personen: {visuals}")
    style = (style_directive or "").strip()
    if style:
        parts.append(f"Stil: {style}")
    return " · ".join(parts)


def describe_messages(
    context_text: str,
    layout: Dict[str, Any],
    language: str,
) -> List[Dict[str, Any]]:
    style_directive = (layout.get("style_directive") or "").strip()
    return [
        {
            "role": "system",
            "content": "You describe novel scenes for illustrations and answer ONLY with JSON.",
        },
        {
            "role": "user",
            "content": (
                f"Roman-Passagen (Sprache: {language}) rund um eine Illustrations-Position:\n"
                "---\n" + context_text + "\n---\n\n"
                "Liefere drei getrennte Felder für eine Illustration an dieser Stelle:\n"
                "- scene: was GESCHIEHT an dieser Stelle (1 Satz, handlungsbasiert, keine Interpretation)\n"
                "- location: der Ort, so der Text ihn hergibt (1-2 Sätze, konkret)\n"
                "- setting: Tageszeit/Kalenderzeit/Stimmung/Wetter/Licht (1 Satz)\n\n"
                "Nichts erfinden, das die Passagen nicht hergeben. Stilvorgabe des Autors "
                "(nur fürs Setting, nicht fürs Bild-Prompt-Format): "
                + (style_directive or "—")
                + "\n\n"
                'JSON: {"scene": "...", "location": "...", "setting": "..."}'
            ),
        },
    ]


def parse_describe(raw: str) -> Dict[str, str]:
    payload = extract_json(raw or "")
    if not isinstance(payload, dict):
        raise ValueError("no JSON object in scene description")
    return {
        "scene": strip_inline_thinking(str(payload.get("scene") or "")).strip()[:400],
        "location": strip_inline_thinking(str(payload.get("location") or "")).strip()[:400],
        "setting": strip_inline_thinking(str(payload.get("setting") or "")).strip()[:400],
    }


async def call_llm(
    profile: Dict[str, Any], messages: List[Dict[str, Any]], temperature: float, unload: bool = True
):
    """One sequential LLM call, Ollama unloaded afterwards (conventions)."""
    client = LlmClient(LlmProfile(**profile))
    try:
        return await client.chat(messages, temperature=temperature)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
        if unload:
            try:
                await unload_ollama_profile(profile)
            except Exception:
                pass