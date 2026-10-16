from __future__ import annotations

import math
from typing import List, Dict, Any, Optional

WORD_COUNT_TOLERANCE = 0.20
WORD_COUNT_TOLERANCE_PERCENT = 20

CHAPTER_ANCHORS = [
    (0, 0),
    (5000, 5),
    (10000, 10),
    (20000, 15),
    (50000, 25),
    (100000, 40),
    (500000, 80),
    (1000000, 120),
]

MAX_READ = 3
MAX_SEARCH = 5
MAX_CORRECT_PER_CHAPTER = 3
SEARCH_MAX_HITS = 8
MAX_TURNS_PER_CHAPTER = 50

SYSTEM_OUTLINE = {
    "en": "You are an experienced novelist who plans stories carefully.",
    "de": "Du bist ein erfahrener Romanautor, der Geschichten sorgfältig plant.",
}

SYSTEM_CHAPTER = {
    "en": (
        "You are an experienced novelist writing a long novel chapter by chapter. "
        "Use the provided tools to read written chapters, search for specified "
        "content, write the current chapter, fix inconsistencies, and maintain "
        "the structured narrative state."
    ),
    "de": (
        "Du bist ein erfahrener Romanautor, der einen langen Roman Kapitel für "
        "Kapitel schreibt. Nutze die bereitgestellten Werkzeuge, um geschriebene "
        "Kapitel zu lesen, bestimmte Inhalte zu suchen, das aktuelle Kapitel zu "
        "schreiben, Inkonsistenzen zu korrigieren und den strukturierten "
        "Erzählzustand zu pflegen."
    ),
}


def recommend_chapter_count(word_count: int) -> int:
    if word_count <= 0:
        return 0
    anchors = CHAPTER_ANCHORS
    if word_count >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        w0, c0 = anchors[i]
        w1, c1 = anchors[i + 1]
        if w0 <= word_count <= w1:
            if w1 == w0:
                return c0
            ratio = (word_count - w0) / (w1 - w0)
            return max(1, math.ceil(c0 + ratio * (c1 - c0)))
    return anchors[-1][1]


def codex_context(codex: Dict[str, Any]) -> str:
    characters = codex.get("characters") or []
    settings = codex.get("settings") or []
    lore = codex.get("lore") or []
    lines: List[str] = []
    if characters:
        lines.append("## Characters to use")
        for item in characters:
            aliases = ", ".join(item.get("aliases") or [])
            alias_note = f" (aliases: {aliases})" if aliases else ""
            role = item.get("role") or ""
            role_note = f" — {role}" if role else ""
            lines.append(f"- {item.get('name')}{alias_note}{role_note}: {item.get('description', '')}")
    if settings:
        lines.append("## Settings and locations to use")
        for item in settings:
            aliases = ", ".join(item.get("aliases") or [])
            alias_note = f" (aliases: {aliases})" if aliases else ""
            lines.append(f"- {item.get('name')}{alias_note}: {item.get('description', '')}")
    if lore:
        lines.append("## World lore and rules")
        for item in lore:
            lines.append(f"- {item.get('name')}: {item.get('description', '')}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def codex_context_note(lang: str) -> str:
    if lang == "de":
        return (
            "# Autoren-Bibel (verbindliche Vorgaben)\n"
            "Die unten angeführten Figuren, Schauplätze und Weltfakten wurden vom Autor festgelegt. "
            "Nutze sie für die Planung, gib den Figuren Namen und Rollen wie angegeben und füge bei "
            "Bedarf eigene Nebenfiguren hinzu.\n\n"
        )
    return (
        "# Author's Codex (binding)\n"
        "The characters, settings, and world facts below were fixed by the author. Use them in "
        "planning, give the characters the stated names and roles, and add your own supporting "
        "characters where the plot needs them.\n\n"
    )


def outline_premise_prompt(
    lang: str, prompt: str, word_count: int, codex: str = ""
) -> str:
    codex_block = "" if not codex else (
        codex_context_note(lang) + codex + "\n"
    )
    if lang == "de":
        return (
            f"Story-Anforderung: {prompt}\n\n"
            f"{codex_block}"
            f"Der fertige Roman umfasst etwa {word_count} Wörter. "
            "Schreibe eine übergeordnete Prämisse von etwa 300 Wörtern. "
            "Decke Kernkonflikt, Protagonist(en), Schauplatz, zentrales Thema und den "
            "gesamten Erzbogen vom Anfang bis zum Ende ab. Zerlege sie noch nicht in "
            "Kapitel.\n\n"
            "Gib NUR den Text der Prämisse aus, ohne Überschriften oder zusätzliche Kommentare."
        )
    return (
        f"Story prompt: {prompt}\n\n"
        f"{codex_block}"
        f"The final novel will be about {word_count} words. "
        "Write a high-level premise of approximately 300 words. "
        "Cover the core conflict, protagonist, setting, central theme, "
        "and the overall narrative arc from beginning to end. "
        "Do not break it into chapters yet.\n\n"
        "Output ONLY the premise text, no headings or extra commentary."
    )


def outline_detail_prompt(
    lang: str,
    prompt: str,
    premise: str,
    target_words: int,
    codex: str = "",
) -> str:
    codex_block = "" if not codex else codex + "\n"
    if lang == "de":
        return (
            f"Story-Anforderung: {prompt}\n\n"
            f"{codex_block}"
            f"Prämisse:\n{premise}\n\n"
            f"Erweitere die Prämisse zu einer detaillierten Synopsis von etwa {target_words} Wörtern. "
            "Geh die wichtigsten Handlungsstationen der Reihe nach durch, stelle die Hauptfiguren "
            "und ihre Motivationen vor, beschreibe zentrale Schauplätze und lege die großen "
            "Wendepunkte und die Auflösung dar. Zerlege sie noch nicht in Kapitel.\n\n"
            "Gib NUR den Text der Synopsis aus."
        )
    return (
        f"Story prompt: {prompt}\n\n"
        f"{codex_block}"
        f"High-level premise:\n{premise}\n\n"
        f"Expand the premise into a detailed synopsis of approximately "
        f"{target_words} words. The synopsis should walk through the major "
        "story beats in order, introduce the principal characters and "
        "their motivations, describe key locations, and lay out the major "
        "turning points and resolution. Do not break it into chapters yet.\n\n"
        "Output ONLY the synopsis text."
    )


def outline_acts_prompt(
    lang: str, prompt: str, premise: str, detail: str,
    word_count: int, codex: str = "",
) -> str:
    codex_block = "" if not codex else codex + "\n"
    if lang == "de":
        return (
            f"Story-Anforderung: {prompt}\n\n"
            f"{codex_block}"
            f"Prämisse:\n{premise}\n\n"
            f"Detaillierte Synopsis:\n{detail}\n\n"
            f"Dies ist ein Roman von etwa {word_count} Wörtern. Teile die Geschichte in eine "
            "kleine Anzahl von Akten oder Bänden (typischerweise 3–6). Gib einem jeden Akt einen "
            "Titel und eine Beschreibung von zwei bis vier Sätzen, die zusammenfasst, was darin "
            "geschieht. Die Akte müssen gemeinsam die gesamte Synopsis lückenlos abdecken.\n\n"
            "Gib die Akt-Gliederung als lesbaren Text aus, jede Akt klar nummeriert und betitelt, "
            "danach die Beschreibung."
        )
    return (
        f"Story prompt: {prompt}\n\n"
        f"{codex_block}"
        f"Premise:\n{premise}\n\n"
        f"Detailed synopsis:\n{detail}\n\n"
        f"This is a long novel of about {word_count} words. "
        "Divide the story into a small number of acts or volumes "
        "(typically 3-6). For each act, give a title and a 2-4 sentence "
        "description summarizing what happens within that act. The acts "
        "must collectively cover the entire detailed synopsis without "
        "gaps.\n\n"
        "Output the act-level outline as readable text. Each act should be "
        "clearly numbered and titled, followed by its description."
    )


def outline_chapters_prompt(
    lang: str, prompt: str, context: str, word_count: int,
    rec: int, per_chapter: int, codex: str = "",
) -> str:
    codex_block = "" if not codex else codex + "\n"
    if lang == "de":
        rec_line = (
            f"Empfohlene Kapitelzahl: etwa {rec} Kapitel (durchschnittliches Ziel pro Kapitel "
            f"~{per_chapter} Wörter). Du darfst leicht abweichen, wenn die Handlung es erfordert, "
            "bleibe aber nahe an dieser Zahl. Das word_count pro Kapitel bestimmst du."
            if rec
            else "Die Geschichte ist zu kurz für mehrere Kapitel. Gib eine einzelnes Kapitel mit dem "
            "gesamten Inhalt zurück."
        )
        return (
            f"Story-Anforderung: {prompt}\n\n"
            f"{codex_block}"
            f"Bisherige Gliederung:\n{context}\n\n"
            f"Das Gesamtsoll für den Roman beträgt {word_count} Wörter. {rec_line}\n\n"
            "Gib für jedes Kapitel an:\n"
            "  - id: Ganzzahl, beginnend bei 0, aufsteigend.\n"
            "  - name: kurzer Kapiteltitel.\n"
            "  - description: Beschreibung der Schlüsselereignisse, Figurenmomente und "
            "Handlungsschritte des Kapitels.\n"
            "  - word_count: Zielwortzahl des Kapitels. Die Summe aller word_count-Werte muss "
            f"innerhalb von ±{WORD_COUNT_TOLERANCE_PERCENT}% des Gesamtziels liegen. Wichtige "
            "Kapitel dürfen mehr, Übergangskapitel weniger bekommen.\n\n"
            "Gib die Kapitel-Gliederung als JSON-Array aus:\n"
            "[\n"
            '  {"id": 0, "name": "...", "description": "...", "word_count": 1000},\n'
            '  {"id": 1, "name": "...", "description": "...", "word_count": 1000},\n'
            "  ...\n"
            "]\n\n"
            "Stelle sicher, dass jede Beschreibung die wesentlichen Inhalte des Kapitels abdeckt."
        )
    rec_line = (
        f"Recommended number of chapters: approximately {rec} chapters "
        f"(average target ~{per_chapter} words per chapter). "
        "You may adjust slightly if the plot requires it, but stay close. "
        "The word_count for each chapter is up to you."
        if rec
        else "The story is too short to break into chapters. "
        "Return a single chapter containing the entire story."
    )
    return (
        f"Story prompt: {prompt}\n\n"
        f"{codex_block}"
        f"Prior outline:\n{context}\n\n"
        f"The total target word count for the entire novel is {word_count}. "
        f"{rec_line}\n\n"
        "For each chapter, provide:\n"
        "  - id: an integer, starting from 0 and incrementing.\n"
        "  - name: a short chapter title.\n"
        "  - description: an outline description summarizing "
        "the key events, character moments, and plot progression in this chapter.\n"
        "  - word_count: the target word count for this chapter. The sum of all "
        "chapter word_count values must stay within "
        f"±{WORD_COUNT_TOLERANCE_PERCENT}% of the total target. "
        "You may assign more words to important chapters and fewer to transitional ones.\n\n"
        "Output the chapter-level outline as a JSON array:\n"
        "[\n"
        '  {"id": 0, "name": "...", "description": "...", "word_count": 1000},\n'
        '  {"id": 1, "name": "...", "description": "...", "word_count": 1000},\n'
        "  ...\n"
        "]\n\n"
        "Ensure each description covers the essential content of the chapter."
    )


def chapter_prompt(
    lang: str,
    outline_json: str,
    state_json: str,
    ch_id: int,
    ch_name: str,
    ch_desc: str,
    ch_target: int,
    completed: int,
    story_contract: str,
    codex: str = "",
    author_notes: str = "",
    visible_outline: Optional[str] = None,
) -> str:
    outline_text = visible_outline if visible_outline else outline_json
    codex_block = "" if not codex else (
        "# Author's Codex\n"
        "The author fixed the following characters, settings, and world facts. Honor names, "
        "aliases, and facts; never contradict them.\n\n" + codex + "\n"
        if lang == "en" else
        "# Autoren-Bibel\n"
        "Der Autor hat die folgenden Figuren, Schauplätze und Weltfakten festgelegt. Halte Namen, "
        "Aliasse und Fakten ein; widersprich ihnen nie.\n\n" + codex + "\n"
    )
    notes_block = "" if not author_notes else (
        "# Author Notes for this Chapter\n"
        f"{author_notes}\n\n"
    )
    if lang == "de":
        return (
            f"Du schreibst Kapitel {ch_id} eines langen Romans.\n\n"
            f"# Ursprüngliche Story-Anforderung\n{story_contract}\n\n"
            f"{codex_block}"
            f"{notes_block}"
            f"# Vollständige eingefrorene Gliederung\n{outline_text}\n\n"
            f"# Aktueller Erzählzustand\n{state_json}\n\n"
            f"# Abgeschlossene Kapitel\nDie ersten {completed} Kapitel sind fertig.\n\n"
            f"# Aktuelle Aufgabe\n"
            f"Kapitel-ID: {ch_id}\n"
            f"Kapitelname: {ch_name}\n"
            f"Beschreibung: {ch_desc}\n"
            f"Zielwortzahl dieses Kapitels: {ch_target}. Halte dich strikt an ±"
            f"{WORD_COUNT_TOLERANCE_PERCENT}% dieser Zielwortzahl.\n\n"
            "Ausführungsreihenfolge:\n"
            "1. Nutze read, um ein bestimmtes Kapitel erneut zu lesen, oder search, um frühere "
            "Fakten zu finden, falls nötig.\n"
            "2. Nutze write, um das vollständige aktuelle Kapitel zu schreiben.\n"
            "3. Falls nötig, nutze correct, um Konsistenzfehler im aktuellen oder einem früheren "
            "Kapitel präzise zu beheben.\n"
            "4. Sobald der Text endgültig ist, mache genau einen update-Aufruf, der "
            "Charakterzustands-Upserts, neu etablierte abgeschlossene Ereignisse, neue "
            "zukünftige Anforderungen und die aufgelösten Schlüssel zukünftiger Anforderungen "
            "enthält.\n"
            "5. Wenn alles abgeschlossen ist, mache keinen Toolaufruf und gib nur DONE aus; "
            "jeder andere Text beendet das Kapitel nicht.\n\n"
            "Schreib- und Zustandsanforderungen:\n"
            "- Die Gliederung ist ein Plan, kein Prosatext; entwickle sie zu Szenen mit Dialog, "
            "sinnlichen Details und inneren Erleben.\n"
            "- In upsert_character_state liefere {name, description} mit dem vollständigen "
            "aktuellen Aufenthaltsort, Ziel, Beziehungen, Wissen, Besitz sowie körperlichem oder "
            "emotionalem Zustand der Figur; ein existierender Name wird ersetzt, nicht angehängt. "
            "In add_past_event zeichne ein abgeschlossenes Ereignis nur auf, wenn der Sachverhalt "
            "nicht bereits in der eingefrorenen Gliederung steht und spätere Handlung "
            "beeinflussen kann; gib ihm einen stabilen snake_case-Schlüssel. Füge zu "
            "add_future_requirement nur konkrete spätere Verpflichtungen hinzu, ebenfalls mit "
            "stabilen Schlüsseln, und übergebe die Schlüssel der in diesem Kapitel erfüllten "
            "Anforderungen an resolve_future_requirement. Jedes Feld muss ein echtes "
            "JSON-Array sein; serialisiere oder zitiere ein Array niemals als Zeichenkette. "
            "Füge alle vier Arrays hinzu und nutze [], wenn eines keine Änderungen hat.\n"
            "- Beachte die eigenen Bedingungen, Limits und Schemas jedes Tools."
        )
    return (
        f"You are writing Chapter {ch_id} of a long novel.\n\n"
        f"# Original Story Prompt\n{story_contract}\n\n"
        f"{codex_block}"
        f"{notes_block}"
        f"# Full Frozen Outline\n{outline_text}\n\n"
        f"# Current Narrative State\n{state_json}\n\n"
        f"# Completed Chapters\n"
        f"The first {completed} chapters have been completed.\n\n"
        f"# Current Task\n"
        f"Chapter ID: {ch_id}\n"
        f"Chapter Name: {ch_name}\n"
        f"Description: {ch_desc}\n"
        f"Target word count for this chapter: {ch_target}. "
        f"Keep the chapter strictly within ±{WORD_COUNT_TOLERANCE_PERCENT}% "
        "of this target word count.\n\n"
        "Execution order:\n"
        "1. Use read to revisit a specific chapter, or search to locate prior "
        "facts, as needed.\n"
        "2. Use write to write the complete current chapter.\n"
        "3. If necessary, use correct to precisely fix consistency errors in "
        "the current or a prior chapter.\n"
        "4. Once the prose is final, make exactly one update call containing "
        "character-state upserts, newly established completed events, "
        "new future "
        "requirements, and resolved future-requirement keys.\n"
        "5. When everything is complete, make no tool call and output only "
        "DONE; any other text does not finish the chapter.\n\n"
        "Writing and state requirements:\n"
        "- The outline is a plan, not prose; expand it into scenes with dialogue, "
        "sensory detail, and internal experience.\n"
        "- In upsert_character_state, provide {name, description} with each "
        "character's complete current location, goal, relationships, "
        "knowledge, possessions, and physical or emotional condition; an "
        "existing name is replaced rather than appended. In add_past_event, "
        "record a completed event only when the fact is not already explicit "
        "in the frozen outline and may affect later plot; give it a stable "
        "snake_case key. Add only concrete later obligations to "
        "add_future_requirement, also with stable keys, and pass the keys of "
        "requirements fulfilled in this chapter to "
        "resolve_future_requirement. Every field must "
        "be a native JSON array; "
        "never serialize or quote an array as a string. Include all four "
        "arrays and use [] when one has no changes.\n"
        "- Follow each tool's own conditions, limits, and schema."
    )