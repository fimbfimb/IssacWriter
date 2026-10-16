"""Authorial translation: draft, refine, check — worthwhile prose even from weak English.

Replaces the "candidates transcribe the English, judge picks the least bad
result" logic whenever the source text itself is weak. Per
uebersetzungssystem.md the pipeline per block is:

1. ENTWURF      one strong model renders the block in the target language with
                the source visible. It may re-structure sentences and replace
                images (meaning preserved); unclear source passages are resolved
                to the most plausible reading and reported under ANMERKUNGEN.
2. VERFEINERN   the strongest available model polishes the German (source still
                visible, deliberately simple prompt). Up to N rounds; the run
                stops early once two rounds barely differ (similarity threshold).
3. PRÜFEN       flags only, never an automatic rewrite:
                a) length watchdog — deterministic char-ratio bounds
                b) fidelity check — a separate model, JSON verdict

Findings (flags + annotations) land in review_list.md for a human decision.
Checkpoints are written per stage, so an interrupted run resumes mid-pipeline.

Not used here: parallel candidates, a judge, an analysis stage before the draft,
a blind editor loop. Evidence in uebersetzungssystem.md section 2.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..llm.provider import LlmClient, LlmError, LlmProfile, extract_json, strip_inline_thinking
from .ensemble import TranslationStopped, codex_glossary, style_context, unload_ollama_profile

DRAFT_TEMPERATURE = 0.7
DEFAULT_DRAFT_TEMPERATURE = DRAFT_TEMPERATURE
REFINE_TEMPERATURE = 0.2
CHECK_TEMPERATURE = 0.0
DEFAULT_REFINE_ROUNDS = 2
DEFAULT_SIMILARITY_STOP = 0.97
DEFAULT_RATIO_MIN = 0.85
DEFAULT_RATIO_MAX = 1.45
DEFAULT_MIN_WORDS = 250
MAX_NOTES = 12
_MAX_REVIEW_LINES_EMITTED = 8

_EMPTY_NOTE_VALUES = ("", "keine", "keine.", "none", "none.", "-", "—")

_ANNOTATION_MARKER = re.compile(r"^[ \t]*[#*>-]*[ \t]*ANMERKUNGEN\b", re.IGNORECASE)
_BULLET_PREFIX = re.compile(r"^[ \t]*(?:[-*••]|\d+[.)])[ \t]+")

# Models that reason in plain text (no think tags) sometimes announce the actual
# answer with a marker line before it; recognizing the common forms lets us cut
# the reasoning off deterministically.
_ANSWER_MARKER_LINE = re.compile(
    r"^[ \t]*(?:[#>*_\-]+[ \t]*)*"
    r"(?:final\s+answers?|final\s+text|final\s+version|final\s+translation"
    r"|finale?\s+fassung|finale?\s+antwort|finaler\s+text)"
    r"\s*[:：\-—]*[ \t]*$",
    re.IGNORECASE,
)

_PROTOCOL_MARKER_LINE = re.compile(
    r"^[ \t]*(?:[#>*_\-]+[ \t]*)?(?:TEXT|ANTWORT|ANSWER|DEUTSCHER\s+TEXT|GERMAN\s+TEXT)"
    r"\s*[:：]?[ \t]*$",
    re.IGNORECASE,
)
# Reasoning models sometimes think in plain English inside the content field.
# Function-word sets let us classify each sentence and cut the reasoning off
# right before the target-language answer. Tokens that exist in both languages
# ("die", "man", "was", "hat", "will", "so") are listed in both sets, so they
# cancel out.
_ENGLISH_MARKERS = frozenset(
    """the and is are were that this these those it its of to for with as by but or not
    i we you they he she his her my our should could would must can may might does did
    has had than then there here what which who whose why how if when while because
    very more most less least better best good fine keep drop consider considered
    option options version versions english sentence sentences meaning translation
    translated translate original reading maybe actually slightly acceptable awkward
    alternatives alternative approach restructure consideration heading tone rhythm
    word choice""".split()
)
_GERMAN_MARKERS = frozenset(
    """der das dem den des ein eine einen einem einer eines und ist war waren bleibt
    blieb wurde wurden werden sind sich nicht auch noch schon nur aber doch denn weil
    mit nach über unter am im zum zur von vom für auf aus bei beim ganz kaum wieder
    immer sagte kam ging stand wollte hätte wäre kapitel morgen abend wind hafen""".split()
)
# homographs: count for both languages so they cancel out
for _token in ("die", "man", "was", "hat", "will", "so", "an", "be", "es"):
    _ENGLISH_MARKERS = frozenset(_ENGLISH_MARKERS | {_token})
    _GERMAN_MARKERS = frozenset(_GERMAN_MARKERS | {_token})


def _sentence_language(sentence: str) -> str:
    tokens = re.findall(r"[A-Za-zÄÖÜäöüß]+", sentence)
    english = sum(1 for token in tokens if token.lower() in _ENGLISH_MARKERS)
    german = sum(1 for token in tokens if token.lower() in _GERMAN_MARKERS)
    if english == 0 and german == 0:
        return "neutral"
    if german > english:
        return "german"
    if english > german:
        return "english"
    return "neutral"


def _cut_reasoning_around_answer(head: str) -> str:
    """Pick the longer run of target-language sentences around English reasoning.

    Models that reason in plain text either put the prose first and the
    commentary after, or the reasoning first and the answer at the end. Both
    runs are measured; the longer one is the answer, the other is reasoning.
    """
    matches = list(re.finditer(r"[^.!?…]*[.!?…]+|[^.!?…]+$", head))
    sentences = [m.group() for m in matches]
    if len(sentences) < 2:
        return head
    languages = [_sentence_language(sentence) for sentence in sentences]
    if "english" not in languages:
        return head
    first_english = languages.index("english")
    last_english = len(languages) - 1 - languages[::-1].index("english")
    head_run = "".join(m.group() for m in matches[:first_english]).strip()
    tail_run = "".join(m.group() for m in matches[last_english + 1 :]).strip()
    candidates = [run for run in (head_run, tail_run) if run]
    if not candidates:
        return head
    best = max(candidates, key=len)
    if len(best) >= 120:
        half = len(best) // 2
        if similar_ratio(best[:half], best[-half:]) >= 0.97:
            best = best[half:].strip()
    # reasoning sometimes glues an English lead-in ("One more consideration:") to
    # the answer sentence; strip an English prefix up to the last colon.
    lead = best[:120]
    boundary = max(lead.rfind(":"), lead.rfind("—"), lead.rfind("–"))
    if boundary > 0 and _sentence_language(lead[:boundary]) == "english":
        best = best[boundary + 1 :].strip()
    return best


_TRAILER_META = re.compile(
    r"^(?:no\s+notes?(?:\s+needed)?\.?|keine\s+anmerkungen?\.?|keine\.?|anmerkungen:\s*keine\.?)\s*[?!]*\s*$",
    re.IGNORECASE,
)


def clean_model_prose(raw: str) -> Tuple[str, List[str], bool]:
    """Robust extraction of the actual prose from a model response.

    Defensive against models that reason inside the content field: think tags
    are stripped, a bare TEXT/ANSWER protocol line and "Final answer:"-style
    markers cut the reasoning off, English reasoning glued to a German answer
    is dropped at the sentence boundary, and an answer restated back-to-back
    collapses to one copy. Returns (text, notes, marker_found).
    """
    text = strip_inline_thinking(raw or "")
    lines = text.splitlines()
    protocol_cut: Optional[int] = None
    for i, line in enumerate(lines):
        if _PROTOCOL_MARKER_LINE.match(line):
            protocol_cut = i
    if protocol_cut is not None and any(line.strip() for line in lines[protocol_cut + 1 :]):
        text = "\n".join(lines[protocol_cut + 1 :])

    head, notes, found = parse_annotated_output(text)
    if not head:
        return head, notes, found

    head_lines = head.splitlines()
    marker_cut: Optional[int] = None
    for i, line in enumerate(head_lines):
        if _ANSWER_MARKER_LINE.match(line):
            marker_cut = i
    if marker_cut is not None and any(line.strip() for line in head_lines[marker_cut + 1 :]):
        head = "\n".join(head_lines[marker_cut + 1 :]).strip()

    head = _cut_reasoning_around_answer(head)

    if len(head) >= 120:
        half = len(head) // 2
        if similar_ratio(head[:half], head[-half:]) >= 0.97:
            head = head[half:].strip()

    # models sometimes end with a stray "No notes needed?" instead of a
    # proper ANMERKUNGEN section; such trailing meta lines are not prose
    chunks = [c for c in split_paragraphs(head) if not _TRAILER_META.match(c)]
    if chunks:
        head = "\n\n".join(chunks)

    head = re.sub(r"(?<=\.)[-–—]+\s*$", "", head).strip()

    return head, notes, found


def build_word_batches(
    chapters: List[Dict[str, Any]], min_words: int = 250, max_words: int = 500
) -> List[Dict[str, Any]]:
    """Group paragraphs into draft batches of ~min..max words (deterministic).

    Paragraphs accumulate until the batch reaches `min_words`; adding the next
    paragraph only continues while the total stays within `max_words`. A single
    paragraph longer than `max_words` keeps its own batch.
    """
    min_words = max(1, int(min_words))
    max_words = max(min_words, int(max_words))
    batches: List[Dict[str, Any]] = []
    for chapter in chapters:
        paragraphs = [
            line.rstrip()
            for line in (chapter.get("content") or "").split("\n")
            if line.strip()
        ]
        if not paragraphs:
            continue
        header = f"Chapter {int(chapter['id']) + 1}: {chapter.get('name') or 'Untitled'}"
        groups: List[List[str]] = []
        current: List[str] = []
        total = 0
        for paragraph in paragraphs:
            width = max(1, len(paragraph.split()))
            if current and total >= min_words and total + width > max_words:
                groups.append(current)
                current = []
                total = 0
            current.append(paragraph)
            total += width
        if current:
            groups.append(current)
        for chunk in groups:
            batches.append(
                {
                    "chapter_id": int(chapter["id"]),
                    "chapter_name": chapter.get("name") or "Untitled",
                    "header": header if chunk is groups[0] else None,
                    "paragraphs": chunk,
                    "words": sum(max(1, len(p.split())) for p in chunk),
                    "source_hash": hashlib.sha256(
                        "\n".join(chunk).encode("utf-8")
                    ).hexdigest()[:16],
                }
            )
    return batches


def split_paragraphs(text: str) -> List[str]:
    """Split a generated text on blank lines; collapse hard-wrap newlines."""
    segments: List[str] = []
    for raw in re.split(r"\n\s*\n", text or ""):
        segment = re.sub(r"[ \t]*\n[ \t]*", " ", raw)
        segment = re.sub(r"[ \t]{2,}", " ", segment).strip()
        if segment:
            segments.append(segment)
    return segments


def parse_annotated_output(raw: str) -> Tuple[str, List[str], bool]:
    """Separate generated prose from a trailing ANMERKUNGEN section.

    Returns (text, notes, marker_found). Without any marker line the whole
    response is prose. If the marker appears in the very first line the model
    violated the output protocol, so the whole response stays prose and the
    notes section is dropped rather than leaking into the manuscript.
    """
    text = raw or ""
    lines = text.splitlines()
    marker_index: Optional[int] = None
    for i, line in enumerate(lines):
        if _ANNOTATION_MARKER.match(line):
            marker_index = i
            break
    if marker_index is None:
        return text.strip(), [], False
    head = "\n".join(lines[:marker_index]).strip()
    if not head:
        return text.strip(), [], False
    notes: List[str] = []
    for line in lines[marker_index + 1 :]:
        if not line.strip():
            continue
        note = _BULLET_PREFIX.sub("", line.strip()).strip().rstrip(".,;")
        if note.strip().lower() in _EMPTY_NOTE_VALUES:
            continue
        notes.append(note)
        if len(notes) >= MAX_NOTES:
            break
    return head, notes, True


def similar_ratio(a: str, b: str) -> float:
    """Whitespace-insensitive text similarity in [0, 1] for the early-stop rule."""
    na = re.sub(r"\s+", " ", (a or "").strip())
    nb = re.sub(r"\s+", " ", (b or "").strip())
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def char_ratio(german: str, english: str) -> Optional[float]:
    """German/English character ratio for the length watchdog (None if unknown)."""
    de_len = len((german or "").strip())
    en_len = len((english or "").strip())
    if en_len <= 0:
        return None
    if de_len <= 0:
        return 0.0
    return de_len / en_len


def length_flag(ratio: Optional[float], ratio_min: float, ratio_max: float) -> Optional[str]:
    if ratio is None:
        return "length not measurable (empty source paragraph)"
    if ratio < ratio_min or ratio > ratio_max:
        return f"length ratio {ratio:.2f} outside [{ratio_min:.2f}, {ratio_max:.2f}]"
    return None


def draft_messages(
    source_block: str,
    language: str,
    book: Dict[str, Any],
    previous_paragraph: Optional[str],
    style_probe: Optional[str] = None,
) -> List[Dict[str, Any]]:
    system_parts = [
        f"You are a literary author and translator. Your goal is a text in {language} that reads",
        f"as if it had been conceived and written in {language} from the start.",
    ]
    user_parts: List[str] = []
    glossary = codex_glossary(book)
    if glossary:
        user_parts.append(glossary)
    style = style_context(book)
    if style:
        user_parts.append(style)
    probe = (style_probe or "").strip()
    if probe:
        user_parts.append(f"Style probe (target tone, do not copy content):\n{probe}")
    if previous_paragraph:
        user_parts.append(
            f"Previous paragraph in {language} (keep tone and terminology):\n{previous_paragraph}"
        )
    user_parts.append(f"English original:\n{source_block}")
    user_parts.append(
        "Rules:\n"
        "- Begin your output with a single line that contains exactly: TEXT\n"
        f"- This is a plain retelling in {language}, not a transcription: keep events, their order,\n"
        "  facts and names — retell them in simple, clear sentences, the way someone would tell this\n"
        f"  story aloud. Every sentence must stand on its own and make sense in {language}.\n"
        "- Prefer plain statements over imagery. An original metaphor is not sacred — reduce it to\n"
        f"  what it means (\"the morning belonged to the bora\" → \"the bora was blowing hard\"). Nobody\n"
        f"  in {language} says \"Wasser, das eine Treppe hinunterfällt\" — state plainly what happens.\n"
        "  Report conscious simplifications of striking images under ANMERKUNGEN.\n"
        "- Omit nothing of the plot, invent nothing, add no intensifiers (\"ganz\", \"sogar\",\n"
        "  \"plötzlich\"). Person and place names stay as they are.\n"
        "- Never mirror the English period: break a pinched construction into two or three sentences.\n"
        "  Participial chains (\"falling off …, through …, down …\") become finite verbs.\n"
        "- Impersonal \"they\" does not become \"Sie + verb\" unless specific people are meant: use \"man\"\n"
        "  or a passive (\"They buried\" → \"Man begrub …\").\n"
        "- Output exactly one paragraph per input paragraph, same order. Lines of the form \"Chapter N:\n"
        "  Title\" are chapter headings: render them as headings.\n"
        "- After the TEXT line, output ONLY the text. Then one line \"ANMERKUNGEN:\" with a short list of\n"
        "  conscious changes (or \"keine\"). No analysis, no explanations, no English reasoning, never\n"
        "  state the final text twice."
    )
    return [
        {"role": "system", "content": "\n".join(system_parts)},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def refine_messages(
    source_paragraph: str,
    previous_paragraph: Optional[str],
    current_text: str,
    language: str,
    book: Dict[str, Any],
) -> List[Dict[str, Any]]:
    user_parts: List[str] = []
    glossary = codex_glossary(book)
    if glossary:
        user_parts.append(glossary)
    if previous_paragraph:
        user_parts.append(f"Previous paragraph:\n{previous_paragraph}")
    user_parts.append(f"English original (for reference):\n{source_paragraph}")
    user_parts.append(f"Current version in {language}:\n{current_text}")
    user_parts.append(
        f"Aufgabe: Diese Fassung klingt noch übersetzt — sie soll wie eine eigene Erzählung in {language}\n"
        "klingen. Nimm den Inhalt und erzähle ihn in eigenen, verständlichen Sätzen neu:\n"
        "- Jeder Satz muss allein Sinn ergeben und sich anhören wie es ein Muttersprachler schreibt.\n"
        "- Unnatürliche Bilder verwerfst du grundsätzlich: sag klar und einfach, was passiert.\n"
        "- Auch tragfähige Bilder darfst du zu einer einfachen Aussage vereinfachen (\"der Morgen gehörte\n"
        "  der Bora\" → \"es wehte starker Wind\") — das ist erwünscht. Klar und simpel schlägt poetisch.\n"
        "- Handlung, Reihenfolge, Figuren und Fakten bleiben; nichts weglassen, nichts dazuerfinden, keine\n"
        "  Verstärker (\"ganz\", \"sogar\"). Unpersönliches \"they\" wird zu \"man …\" oder Passiv, nie zu\n"
        "  \"Sie begruben …\". Eigennamen unverändert.\n"
        "- Englisch gebaute Lange Sätze zerlegst du; Partizipialketten werden zu finiten Verben.\n"
        "- Beginne mit einer einzelnen Zeile, die genau enthält: TEXT\n"
        "- Danach NUR der neue Text (optional ANMERKUNGEN: für bewusste Änderungen). Keine Analyse, kein\n"
        "  englischer Text, keine Wiederholung."
    )
    return [
        {
            "role": "system",
            "content": "You are an experienced literary editor with a fine sense of language.",
        },
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def check_messages(source_paragraph: str, german: str, language: str) -> List[Dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": "You check content fidelity only, not style. Output ONLY JSON.",
        },
        {
            "role": "user",
            "content": (
                f"English original:\n{source_paragraph}\n\n"
                f"Text in {language}:\n{german}\n\n"
                f"The target is by design a plain, simplified retelling in {language}: literary images\n"
                "may have been flattened into simple statements — that is intended, not a loss.\n"
                "Check only real content: is an event, fact or plot detail missing? Was anything\n"
                "invented? Did the plot or the central statement actually change?\n"
                'JSON: {"ok": true/false, "verloren": [], "erfunden": [], "verschoben": []}'
            ),
        },
    ]


def parse_fidelity(raw: str) -> Dict[str, Any]:
    payload = extract_json(raw or "")
    if not isinstance(payload, dict):
        raise ValueError("no JSON object in fidelity verdict")

    def _items(key: str) -> List[str]:
        value = payload.get(key)
        if value is None:
            return []
        if isinstance(value, str):
            value = [value] if value.strip() else []
        if not isinstance(value, list):
            return [str(value)]
        return [str(item).strip() for item in value if str(item).strip()]

    return {
        "ok": bool(payload.get("ok", True)),
        "verloren": _items("verloren"),
        "erfunden": _items("erfunden"),
        "verschoben": _items("verschoben"),
    }


def fidelity_flag(verdict: Dict[str, Any]) -> Optional[str]:
    if verdict.get("error"):
        return None
    details = []
    for key in ("verloren", "erfunden", "verschoben"):
        items = verdict.get(key) or []
        if items:
            details.append(f"{key}: " + "; ".join(items))
    if not verdict.get("ok", True) and not details:
        details.append("verdict ok=false without details")
    return "; ".join(details) if details else None


class AuthorialWriter:
    """Collects per-batch stage results and rewrites outputs + report + review list."""

    def __init__(self, outdir: Path):
        self.outdir = outdir
        self.bilingual_path = outdir / "book_bilingual.txt"
        self.german_path = outdir / "book_german.txt"
        self.review_path = outdir / "review_list.md"
        self.manuscript_path = outdir / "manuscript.md"
        self.report_path = outdir / "authorial_report.json"
        self.finished: List[Dict[str, Any]] = []
        self._chapters: Dict[int, Dict[str, Any]] = {}

    def set_chapters(self, chapters: List[Dict[str, Any]]) -> None:
        self._chapters = {
            int(c["id"]): {
                "chapter_id": int(c["id"]),
                "chapter_name": c.get("name") or "Untitled",
                "words_total": int(c.get("words") or 0),
            }
            for c in chapters
        }

    def chapter_progress(self) -> List[Dict[str, Any]]:
        progress: Dict[int, Dict[str, Any]] = {}
        for chapter_id, info in self._chapters.items():
            progress[chapter_id] = {
                "chapter_id": chapter_id,
                "chapter_name": info["chapter_name"],
                "words_total": info.get("words_total") or 0,
                "translated_words": 0,
                "batches_total": 0,
                "batches_done": 0,
                "paragraphs": 0,
                "review_flags": 0,
                "started": False,
                "done": False,
            }
        for entry in self.finished:
            chapter_id = int(entry.get("chapter_id") or 0)
            row = progress.setdefault(
                chapter_id,
                {
                    "chapter_id": chapter_id,
                    "chapter_name": entry.get("chapter_name") or "Untitled",
                    "words_total": 0,
                    "translated_words": 0,
                    "batches_total": 0,
                    "batches_done": 0,
                    "paragraphs": 0,
                    "review_flags": 0,
                    "started": False,
                    "done": False,
                },
            )
            row["batches_total"] += 1
            row["started"] = True
            if entry.get("stage") in ("done", "refined"):
                row["words_translated"] = row.get("words_translated") or 0
                row["translated_words"] = row.get("translated_words") or 0
                for item in entry.get("items") or []:
                    row["translated_words"] += len((item.get("text") or "").split())
                    row["paragraphs"] += 1 if item.get("text") else 0
                if entry.get("stage") == "done":
                    row["batches_done"] += 1
            row["review_flags"] += len(
                [flag for flag in entry.get("review") or [] if flag.get("type") != "annotation"]
            )
            if row["batches_total"] > 0:
                row["done"] = row["batches_done"] == row["batches_total"]
        return [progress[key] for key in sorted(progress)]

    def set_entry(self, index: int, entry: Dict[str, Any]) -> None:
        if index == len(self.finished):
            self.finished.append(entry)
        else:
            self.finished[index] = entry

    def last_paragraph(self) -> Optional[str]:
        """Last accepted German paragraph from earlier batches (refined items only)."""
        for entry in reversed(self.finished):
            for item in reversed(entry.get("items") or []):
                text = (item.get("text") or "").strip()
                if text:
                    return text
        return None

    def source_for(self, batch: Dict[str, Any]) -> str:
        lines: List[str] = []
        if batch.get("header"):
            lines.append(batch["header"])
        lines.extend(batch["paragraphs"])
        return "\n".join(lines)

    def _german_parts(self, entry: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        """(paragraph lines, review list lines) for one batch entry."""
        lines: List[str] = []
        items = entry.get("items") or []
        texts = [item.get("text") or "" for item in items]
        if not any(t.strip() for t in texts):
            segments = entry.get("german_segments") or []
            texts = [seg for seg in segments if seg.strip()]
        if texts and any(t.strip() for t in texts):
            if entry.get("heading"):
                lines.append(entry["heading"])
            lines.extend(t for t in texts if t.strip())
        return lines, [r for r in (entry.get("review") or [])]

    def german_text(self) -> str:
        blocks: List[str] = []
        for entry in self.finished:
            lines, _ = self._german_parts(entry)
            if lines:
                blocks.append("\n\n".join(lines))
        return "\n\n".join(blocks).rstrip() + ("\n" if blocks else "")

    def bilingual_text(self) -> str:
        blocks: List[str] = []
        for entry in self.finished:
            source = (entry.get("source_block") or "").rstrip()
            lines, _ = self._german_parts(entry)
            if source:
                blocks.append(source)
            if lines:
                blocks.append("\n\n".join(lines))
            blocks.append("")
        return "\n".join(blocks).rstrip() + "\n"

    def review_entries(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for entry in self.finished:
            for flag in entry.get("review") or []:
                rows.append({**flag, "chapter_id": entry.get("chapter_id"), "batch_index": entry.get("index")})
        return rows

    def review_text(self, book_title: str) -> str:
        rows = self.review_entries()
        lines = [
            f"# Review list — {book_title}",
            "",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M')} · {len(rows)} entries "
            f"(flags require a human decision; the system suggests, it does not know).",
            "",
        ]
        if not rows:
            lines.append("No flags — nothing to review.")
            return "\n".join(lines) + "\n"
        current_chapter: Optional[int] = None
        for row in rows:
            chapter_id = row.get("chapter_id")
            if chapter_id != current_chapter:
                current_chapter = chapter_id
                lines.append(f"## Chapter {int(chapter_id) + 1 if chapter_id is not None else '?'}")
            kind = row.get("type") or "flag"
            label = "note" if kind == "annotation" else kind
            paragraph = row.get("paragraph")
            where = f" ¶{paragraph + 1}" if isinstance(paragraph, int) else ""
            lines.append(f"- [{label}{where}] {row.get('detail') or ''}".rstrip())
        return "\n".join(lines) + "\n"

    def manuscript_text(self) -> str:
        """Markdown edition: translated headings as `## …`, German prose per batch."""
        blocks: List[str] = []
        for entry in self.finished:
            lines, _ = self._german_parts(entry)
            if not lines:
                continue
            heading = entry.get("heading") or entry.get("chapter_name") or ""
            if heading:
                blocks.append(f"## {heading}")
            blocks.append("\n\n".join(lines))
        return "\n\n".join(blocks).rstrip() + ("\n" if blocks else "")

    def persist(self, meta: Dict[str, Any], book_title: str = "") -> None:
        self.bilingual_path.write_text(self.bilingual_text(), encoding="utf-8")
        self.german_path.write_text(self.german_text(), encoding="utf-8")
        self.review_path.write_text(self.review_text(book_title), encoding="utf-8")
        self.manuscript_path.write_text(self.manuscript_text(), encoding="utf-8")
        report = dict(meta)
        report["chapters"] = self.chapter_progress()
        report["batches"] = self.finished
        self.report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def load_report(path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


def _review_note(entry: Dict[str, Any], kind: str, paragraph: Optional[int], detail: str) -> None:
    entry.setdefault("review", []).append(
        {"type": kind, "paragraph": paragraph, "detail": detail}
    )


def _role_usage(usage: Any) -> Dict[str, Any]:
    return usage.to_dict() if usage else {}


def _first_line(text: str, limit: int = 140) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return (text or "").strip()[:limit]


def _make_unload(unload_after_call: bool) -> Callable[[Optional[Dict[str, Any]]], Awaitable[None]]:
    async def _unload(profile: Optional[Dict[str, Any]]) -> None:
        if profile and unload_after_call:
            await unload_ollama_profile(profile)

    return _unload


async def run_authorial_translation(
    *,
    chapters: List[Dict[str, Any]],
    language: str,
    draft_profile: Dict[str, Any],
    refiner_profile: Dict[str, Any],
    checker_profile: Optional[Dict[str, Any]],
    unload_after_call: bool,
    refine_rounds: int,
    similarity_stop: float,
    ratio_min: float,
    ratio_max: float,
    min_words: int,
    max_words: Optional[int],
    draft_temperature: float = DRAFT_TEMPERATURE,
    style_probe: str = "",
    outdir: Path,
    book: Dict[str, Any],
    resume: bool,
    emit: Callable[[Dict[str, Any]], Awaitable[None]],
    should_stop: Callable[[], bool],
) -> Dict[str, Any]:
    started = time.time()
    outdir.mkdir(parents=True, exist_ok=True)
    writer = AuthorialWriter(outdir)
    writer.set_chapters(
        [
            {
                "id": c["id"],
                "name": c.get("name") or "Untitled",
                "words": len((c.get("content") or "").split()),
            }
            for c in chapters
        ]
    )
    max_words = max(min_words, int(max_words or (min_words * 2)))
    batches = build_word_batches(chapters, min_words=min_words, max_words=max_words)
    refine_rounds = max(1, int(refine_rounds))
    similarity_stop = min(1.0, max(0.0, float(similarity_stop)))

    draft_name = str(draft_profile.get("name") or draft_profile.get("model") or "draft")
    refiner_name = str(refiner_profile.get("name") or refiner_profile.get("model") or "refiner")
    checker_name = (
        str(checker_profile.get("name") or checker_profile.get("model") or "checker")
        if checker_profile
        else None
    )

    meta: Dict[str, Any] = {
        "mode": "authorial",
        "language": language,
        "draft": {"name": draft_name, "model": draft_profile.get("model")},
        "refiner": {"name": refiner_name, "model": refiner_profile.get("model")},
        "checker": {"name": checker_name, "model": (checker_profile or {}).get("model")}
        if checker_profile
        else None,
        "settings": {
            "refine_rounds": refine_rounds,
            "similarity_stop": similarity_stop,
            "ratio_min": ratio_min,
            "ratio_max": ratio_max,
            "min_words": min_words,
            "max_words": max_words,
        },
        "batches_total": len(batches),
        "started_at": started,
        "totals": {"tokens": {}},
    }
    totals = meta["totals"]

    def _accumulate(name: str, usage: Dict[str, Any]) -> None:
        bucket = totals["tokens"].setdefault(
            name, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        )
        bucket["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        bucket["cost"] += float(usage.get("cost") or 0.0)

    if resume:
        saved = AuthorialWriter.load_report(writer.report_path)
        reuse = 0
        if saved and int(saved.get("batches_total") or 0) == len(batches):
            for entry in saved.get("batches") or []:
                index = int(entry.get("index", -1))
                if (
                    0 <= index < len(batches)
                    and entry.get("source_hash") == batches[index]["source_hash"]
                ):
                    entry["source_block"] = writer.source_for(batches[index])
                    writer.set_entry(index, entry)
                    reuse += 1
        if reuse:
            await emit(
                {
                    "type": "translation_batch",
                    "batch_index": reuse,
                    "batches_total": len(batches),
                    "note": f"resumed — {reuse} batches reused at their saved stage",
                }
            )
        if saved is None or not reuse:
            resume = False

    draft_client = LlmClient(LlmProfile(**draft_profile))
    refiner_client = LlmClient(LlmProfile(**refiner_profile))
    checker_client = LlmClient(LlmProfile(**checker_profile)) if checker_profile else None
    _unload = _make_unload(unload_after_call)
    try:
        for index, batch in enumerate(batches):
            if should_stop():
                writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                raise TranslationStopped()
            restored = writer.finished[index] if index < len(writer.finished) else None
            entry = restored
            stage = (entry or {}).get("stage")
            source_block = writer.source_for(batch)

            # ---- Stufe 1: Entwurf -------------------------------------------
            if stage not in ("drafted", "refined", "done"):
                try:
                    response = await draft_client.chat(
                        draft_messages(
                            source_block, language, book, writer.last_paragraph(), style_probe
                        ),
                        temperature=draft_temperature,
                    )
                    raw = (response.content or "").strip()
                    _accumulate(draft_name, _role_usage(response.usage))
                    await _unload(draft_profile)
                    if not raw:
                        raise LlmError("empty draft")
                    text, notes, _marker = clean_model_prose(raw)
                    if not text.strip():
                        raise LlmError("draft produced no prose before ANMERKUNGEN")
                except Exception as exc:
                    entry = {
                        "index": index,
                        "chapter_id": batch["chapter_id"],
                        "chapter_name": batch["chapter_name"],
                        "source_hash": batch["source_hash"],
                        "source_block": source_block,
                        "source_paragraphs": batch["paragraphs"],
                        "stage": "draft",
                        "draft_error": f"{draft_name}: {exc}",
                        "draft_text": "",
                        "draft_notes": [],
                        "heading": None,
                        "german_segments": [],
                        "items": [],
                        "review": [],
                        "errors": [f"draft: {exc}"],
                    }
                    writer.set_entry(index, entry)
                    await _unload(draft_profile)
                    writer.persist(meta, book.get("title") or "")
                    await emit(
                        {
                            "type": "translation_batch",
                            "batch_index": index + 1,
                            "batches_total": len(batches),
                            "chapter_id": batch["chapter_id"],
                            "stage": "draft",
                            "error": str(exc),
                        }
                    )
                    continue
                segments = split_paragraphs(text)
                heading = None
                expected = len(batch["paragraphs"]) + (1 if batch.get("header") else 0)
                if batch.get("header") and len(segments) == expected:
                    heading, segments = segments[0], segments[1:]
                entry = {
                    "index": index,
                    "chapter_id": batch["chapter_id"],
                    "chapter_name": batch["chapter_name"],
                    "source_hash": batch["source_hash"],
                    "source_block": source_block,
                    "source_paragraphs": batch["paragraphs"],
                    "stage": "drafted",
                    "draft_error": None,
                    "draft_text": text,
                    "draft_notes": notes,
                    "heading": heading,
                    "german_segments": segments,
                    "items": [],
                    "review": [
                        {"type": "annotation", "paragraph": None, "detail": note}
                        for note in notes
                    ],
                    "errors": [],
                }
                writer.set_entry(index, entry)
                writer.persist(meta, book.get("title") or "")
                stage = "drafted"
                await emit(
                    {
                        "type": "translation_batch",
                        "batch_index": index + 1,
                        "batches_total": len(batches),
                        "chapter_id": batch["chapter_id"],
                        "stage": "draft",
                        "notes": len(notes),
                    }
                )
                await emit(
                    {
                        "type": "translation_output",
                        "lines": [f"[{draft_name}] {_first_line(text)}"],
                    }
                )

            # ---- Stufe 2: Verfeinern ---------------------------------------
            if entry["stage"] == "drafted":
                source_paragraphs: List[str] = entry.get("source_paragraphs") or []
                segments: List[str] = (
                    entry.get("german_segments")
                    or split_paragraphs(entry.get("draft_text") or "")
                )
                aligned = len(segments) == len(source_paragraphs)
                if aligned:
                    pairs = list(zip(source_paragraphs, segments))
                else:
                    _review_note(
                        entry,
                        "structure",
                        None,
                        f"paragraph count deviates (source {len(source_paragraphs)}, "
                        f"draft {len(segments)}) — block refined as one text",
                    )
                    pairs = [(entry.get("source_block") or "", " ".join(segments))]
                items: List[Dict[str, Any]] = []
                previous = writer.last_paragraph()
                for p_index, (para_source, para_text) in enumerate(pairs):
                    if should_stop():
                        entry["items"] = items
                        entry["stage"] = "refined"
                        writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                        raise TranslationStopped()
                    item: Dict[str, Any] = {
                        "index": p_index,
                        "source": para_source,
                        "text": para_text,
                        "rounds": 0,
                        "notes": [],
                        "error": None,
                        "kept_draft": False,
                        "stopped_early": False,
                        "length_ratio": None,
                        "fidelity": None,
                    }
                    for round_no in range(1, refine_rounds + 1):
                        try:
                            response = await refiner_client.chat(
                                refine_messages(
                                    para_source, previous, item["text"], language, book
                                ),
                                temperature=REFINE_TEMPERATURE,
                            )
                            raw = (response.content or "").strip()
                            _accumulate(refiner_name, _role_usage(response.usage))
                            await _unload(refiner_profile)
                            if not raw:
                                raise LlmError("empty refinement")
                            refined, note_lines, _marker = clean_model_prose(raw)
                            if not refined.strip():
                                raise LlmError("refinement produced no prose")
                        except Exception as exc:
                            await _unload(refiner_profile)
                            item["error"] = str(exc)
                            item["kept_draft"] = True
                            _review_note(
                                entry,
                                "refiner",
                                p_index,
                                f"refinement failed at round {round_no} ({exc}) — draft kept",
                            )
                            break
                        ratio = similar_ratio(item["text"], refined)
                        item["text"] = refined
                        item["rounds"] = round_no
                        for note in note_lines:
                            item["notes"].append(note)
                            _review_note(entry, "annotation", p_index, note)
                        if ratio >= similarity_stop:
                            item["stopped_early"] = True
                            break
                    previous = item["text"]
                    items.append(item)
                    entry["items"] = items
                    writer.persist(meta, book.get("title") or "")
                entry["stage"] = "refined"
                writer.persist(meta, book.get("title") or "")
                await emit(
                    {
                        "type": "translation_batch",
                        "batch_index": index + 1,
                        "batches_total": len(batches),
                        "chapter_id": batch["chapter_id"],
                        "stage": "refine",
                        "paragraphs": len(items),
                    }
                )

            # ---- Stufe 3: Prüfen (nur flaggen) ------------------------------
            if entry["stage"] == "refined":
                for item in entry.get("items") or []:
                    en_text = item.get("source") or ""
                    de_text = item.get("text") or ""
                    ratio = char_ratio(de_text, en_text)
                    item["length_ratio"] = ratio
                    flag = length_flag(ratio, ratio_min, ratio_max)
                    if flag:
                        _review_note(entry, "length", item.get("index"), flag)
                    if checker_profile:
                        verdict: Dict[str, Any]
                        if should_stop():
                            writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                            raise TranslationStopped()
                        try:
                            response = await checker_client.chat(
                                check_messages(en_text, de_text, language),
                                temperature=CHECK_TEMPERATURE,
                            )
                            verdict = parse_fidelity(response.content or "")
                            _accumulate(checker_name or "checker", _role_usage(response.usage))
                            await _unload(checker_profile)
                        except Exception as exc:
                            await _unload(checker_profile)
                            verdict = {"error": f"{checker_name or 'checker'}: {exc}"}
                            _review_note(
                                entry,
                                "check",
                                item.get("index"),
                                f"fidelity check not evaluable: {exc}",
                            )
                        item["fidelity"] = verdict
                        flag = fidelity_flag(verdict)
                        if flag:
                            _review_note(entry, "fidelity", item.get("index"), flag)
                entry["stage"] = "done"
                writer.persist(meta, book.get("title") or "")
                flag_rows = [
                    row for row in entry.get("review") or [] if row.get("type") != "annotation"
                ]
                await emit(
                    {
                        "type": "translation_batch",
                        "batch_index": index + 1,
                        "batches_total": len(batches),
                        "chapter_id": batch["chapter_id"],
                        "stage": "check",
                        "flags": len(flag_rows),
                        "review_total": len(entry.get("review") or []),
                    }
                )
                if flag_rows:
                    lines = [
                        f"[Prüfung] {row.get('type')}: {row.get('detail')}"
                        for row in flag_rows[:_MAX_REVIEW_LINES_EMITTED]
                    ]
                    await emit({"type": "translation_output", "lines": lines})

        writer.persist(
            {
                **meta,
                "interrupted": False,
                "finished_at": time.time(),
                "duration_s": round(time.time() - started, 1),
            },
            book.get("title") or "",
        )
        gaps = [entry for entry in writer.finished if entry.get("stage") != "done"]
        review_rows = writer.review_entries()
        rounds_total = sum(
            int(item.get("rounds") or 0)
            for entry in writer.finished
            for item in entry.get("items") or []
        )
        return {
            "output_file": str(writer.bilingual_path),
            "german_file": str(writer.german_path),
            "review_file": str(writer.review_path),
            "report_file": str(writer.report_path),
            "batches_total": len(batches),
            "batches_done": len(writer.finished),
            "gaps": len(gaps),
            "totals": totals,
            "review_flags": len([row for row in review_rows if row.get("type") != "annotation"]),
            "annotations": len([row for row in review_rows if row.get("type") == "annotation"]),
            "refine_rounds_total": rounds_total,
        }
    finally:
        for client in (draft_client, refiner_client, checker_client):
            if client is not None:
                await client.aclose()