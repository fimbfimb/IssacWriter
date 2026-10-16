from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any, Dict, List

_CJK = re.compile(
    "[㐀-䶿一-鿿豈-﫿]|[\U00020000-\U0002ffff]"
)
_ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9'_-]*")

SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?\.\n—])\s*|(?<=[。！？!?])(?=[^\s])")


def split_sentences(text: str) -> List[str]:
    parts = re.split(SENTENCE_SPLIT, text)
    return [p.strip() for p in parts if p and p.strip()]


def tokenize(text: str, query: bool = False) -> List[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: List[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if token and token not in seen:
            tokens.append(token)
            seen.add(token)

    for match in _ASCII_TOKEN.findall(normalized):
        add(match)

    cjk_run: List[str] = []

    def flush() -> None:
        for char in cjk_run:
            add(char)
        for i in range(len(cjk_run) - 1):
            add("".join(cjk_run[i : i + 2]))
        cjk_run.clear()

    for char in normalized:
        if _CJK.fullmatch(char):
            cjk_run.append(char)
        else:
            flush()
    flush()

    if not query:
        return tokens
    preferred = [t for t in tokens if len(t) >= 2 or t.isdigit()]
    return preferred or tokens


class ChapterSearchIndex:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chapter_passages USING fts5(
                chapter_id UNINDEXED,
                sentence_id UNINDEXED,
                before_text UNINDEXED,
                sentence_text UNINDEXED,
                after_text UNINDEXED,
                tokens,
                tokenize='unicode61'
            )
            """
        )
        self._conn.commit()
        self._indexed_ids: set[int] = set()
        self._dirty_ids: set[int] = set()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _chapter_rows(chapter: Dict[str, Any]) -> List[tuple]:
        sentences = split_sentences(chapter.get("content", ""))
        rows: List[tuple] = []
        for index, sentence in enumerate(sentences):
            before = sentences[index - 1] if index > 0 else ""
            after = sentences[index + 1] if index + 1 < len(sentences) else ""
            center_tokens = tokenize(sentence)
            context_tokens = tokenize(before) + tokenize(after)
            tokens = " ".join(center_tokens * 3 + context_tokens)
            if tokens:
                rows.append((chapter["id"], index, before, sentence, after, tokens))
        return rows

    def reset(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM chapter_passages")
        self._indexed_ids.clear()
        self._dirty_ids.clear()

    def mark_dirty(self, chapter_id: int) -> None:
        self._dirty_ids.add(chapter_id)

    def _sync(self, chapters: List[Dict[str, Any]]) -> None:
        by_id = {chapter["id"]: chapter for chapter in chapters}
        current = set(by_id)
        stale = self._indexed_ids - current
        changed = {
            cid
            for cid in current
            if cid not in self._indexed_ids or cid in self._dirty_ids
        }
        if not stale and not changed:
            return
        with self._conn:
            for cid in stale | changed:
                self._conn.execute(
                    "DELETE FROM chapter_passages WHERE chapter_id = ?", (cid,)
                )
            for cid in changed:
                rows = self._chapter_rows(by_id[cid])
                if rows:
                    self._conn.executemany(
                        """
                        INSERT INTO chapter_passages(
                            chapter_id, sentence_id, before_text,
                            sentence_text, after_text, tokens
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
        self._indexed_ids = current
        self._dirty_ids.difference_update(changed | stale)

    @staticmethod
    def _fts_query(tokens: List[str]) -> str:
        quoted = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens]
        return " OR ".join(quoted)

    def search(
        self, chapters: List[Dict[str, Any]], query: str, max_hits: int = 8
    ) -> List[Dict[str, Any]]:
        if not query or not chapters:
            return []
        self._sync(chapters)
        query_tokens = tokenize(query, query=True)
        if not query_tokens:
            return []
        rows = self._conn.execute(
            """
            SELECT chapter_id, before_text, sentence_text, after_text,
                   bm25(chapter_passages) AS relevance
            FROM chapter_passages
            WHERE tokens MATCH ?
            ORDER BY relevance, chapter_id DESC, sentence_id
            LIMIT ?
            """,
            (self._fts_query(query_tokens), max_hits),
        ).fetchall()
        return [
            {
                "chapter_id": int(chapter_id),
                "before": before,
                "sentence": sentence,
                "after": after,
            }
            for chapter_id, before, sentence, after, _ in rows
        ]