"""Shared export text cleanup — used by the EPUB, MD and TXT exports.

LLM prose uses em/en dashes as spoken pauses (" — " as a pause between clauses).
In a printed book they read heavy-handed, so the exports normalize them:

- " — " and " – " (dash surrounded by spaces) become ", ".
- " —" at a paragraph edge becomes ","; "— " at a paragraph start disappears.
- A line consisting only of dashes is dropped.
- Only space-/line-surrounded occurrences are touched: ranges like "5—7" or
  compound hyphenations survive untouched.

Also normalizes multiple blank lines and trailing whitespace; hanging "-"
at a paragraph end (a dehyphenation artifact) is removed.
"""

from __future__ import annotations

import re
from typing import List

_DASH = "\u2014\u2013"  # em dash + en dash
_SEPARATORS = " \t\u00a0"

# one optional leading space in front of the dash, so " — " collapses cleanly
_DASH_TOKEN = re.compile(r" ?[" + _DASH + "]")
_SPACE_RUN = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{3,}")
_END_HYPHEN = re.compile(r"-$")
_WRAP_JOIN = re.compile(r"[ \t]*\n[ \t]*")

# models leak markdown emphasis (**bold**, *italics*, __underline__) into
# prose — a printed book never carries asterisks, so they dissolve. Applied
# twice: "**a **b** c**" nests one pair inside another.
_EM_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_EM_BOLD_UNDER = re.compile(r"__(.+?)__", re.DOTALL)
# single asterisks: only when digit-UNflanked ("5*3"/"2 * x" is math, not prose)
_EM_ITALIC = re.compile(r"(?<!\d)\*([^*\n]+?)\*(?!\d)")
_EM_UNDER = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
_ORPHAN_MARKS = re.compile(r"\*\*")


def _strip_emphasis(text: str) -> str:
    for pattern in (_EM_BOLD, _EM_BOLD_UNDER, _EM_ITALIC, _EM_UNDER):
        for _ in range(3):  # nested pairs need a second pass
            text = pattern.sub(r"\1", text)
    return _ORPHAN_MARKS.sub("", text)


def _next_nonspace(text: str, index: int) -> str:
    index = max(0, index)
    while index < len(text) and text[index] in _SEPARATORS + "\n":
        index += 1
    return text[index] if index < len(text) else ""


def _replace_dash(match: "re.Match[str]") -> str:
    """Dash occurrence → "," / "" / unchanged (digit-flanked ranges survive)."""
    text = match.string
    start = match.start()
    consumed = match.group()
    has_prefix_space = consumed.startswith(" ")
    dash_i = start + (1 if has_prefix_space else 0)
    # digit before = char in front of the (already consumed) leading space
    before = text[start - 1] if start > 0 else "\n"
    # digit after = first non-whitespace behind the dash, so "5 — 7" survives
    after = _next_nonspace(text, dash_i + 1)
    if before.isdigit() and after.isdigit():
        return consumed  # "5 — 7" — distance/range, leave it alone
    if has_prefix_space:
        return ","  # "A —" or "A — B": the space after the dash stays in place
    if start == 0 or before == "\n":
        return ""  # "— B" at a paragraph start: the dash disappears
    if before in ".!?:;…":
        return ""  # ".— next" artifact → ". next"
    if before in _SEPARATORS:
        return ","  # non-space whitespace before the dash
    return consumed  # glued on both sides ("Salz—Wasser"): keep


def _dash_only_line(line: str) -> bool:
    return bool(line) and all(ch in _DASH + " \t\u00a0" for ch in line)


def cleanup_text(text: str) -> str:
    """Normalize markdown emphasis, dashes, blank lines and trailing whitespace
    in a manuscript."""
    text = (text or "").replace("\r\n", "\n")
    text = _strip_emphasis(text)
    text = _DASH_TOKEN.sub(_replace_dash, text)
    lines: List[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if _dash_only_line(stripped):
            continue  # a lone dash is not prose
        stripped = _SPACE_RUN.sub(" ", stripped)
        lines.append(stripped)
    text = "\n".join(lines)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def cleanup_paragraphs(text: str) -> List[str]:
    """Cleanup + paragraph split: hard-wrapped lines inside a paragraph join."""
    text = cleanup_text(text)
    paragraphs: List[str] = []
    for raw in re.split(r"\n\s*\n", text):
        segment = _WRAP_JOIN.sub(" ", raw).strip()
        segment = _END_HYPHEN.sub("", segment).strip()  # dangling "-" artifact
        if segment:
            paragraphs.append(segment)
    return paragraphs