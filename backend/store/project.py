from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..nstagent.state import NarrativeState

SCHEMA_VERSION = 1


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def default_codex() -> Dict[str, Any]:
    return {"characters": [], "settings": [], "lore": []}


def default_run_settings() -> Dict[str, Any]:
    return {
        "mode": "auto",
        "temperature": 0.7,
        "dynamic_tokens": True,
        "hard_max_output_tokens": 32768,
        "max_turns": 50,
        "max_attempts_per_chapter": 2,
    }


class BookStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _book_dir(self, book_id: str) -> Path:
        path = self.root / book_id
        if not path.exists():
            raise FileNotFoundError(f"book {book_id} not found")
        return path

    def create_book(
        self,
        title: str,
        story_prompt: str,
        language: str,
        target_words: int,
        genre_notes: str = "",
        codex: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        book_id = uuid.uuid4().hex[:12]
        directory = self.root / book_id
        if directory.exists():
            raise ValueError("book id collision")
        now = _utc_now()
        book = {
            "schema": SCHEMA_VERSION,
            "id": book_id,
            "title": title or "Untitled Book",
            "story_prompt": story_prompt,
            "genre_notes": genre_notes,
            "language": language,
            "target_words": int(target_words),
            "codex": codex or default_codex(),
            "outline": None,
            "status": "empty",
            "planner_profile_id": None,
            "writer_profile_id": None,
            "run_settings": default_run_settings(),
            "created": now,
            "updated": now,
        }
        _write_json(directory / "book.json", book)
        return book

    def save_book(self, book: Dict[str, Any]) -> None:
        book["updated"] = _utc_now()
        _write_json(self.root / book["id"] / "book.json", book)

    def load_book(self, book_id: str) -> Dict[str, Any]:
        book = _read_json(self._book_dir(book_id) / "book.json")
        if book is None:
            raise FileNotFoundError(f"book {book_id} not found")
        return book

    def list_books(self) -> List[Dict[str, Any]]:
        books = []
        if not self.root.exists():
            return books
        for directory in sorted(self.root.iterdir()):
            meta = _read_json(directory / "book.json")
            if meta is None:
                continue
            total_words, chapters_written = self.book_progress(meta["id"])
            books.append(
                {
                    "id": meta["id"],
                    "title": meta["title"],
                    "language": meta["language"],
                    "target_words": meta["target_words"],
                    "status": meta["status"],
                    "created": meta["created"],
                    "updated": meta["updated"],
                    "chapters_written": chapters_written,
                    "words_written": total_words,
                }
            )
        books.sort(key=lambda b: b["updated"], reverse=True)
        return books

    def delete_book(self, book_id: str) -> None:
        directory = self._book_dir(book_id)
        shutil.rmtree(directory)

    def outline_exists(self, book_id: str) -> bool:
        return self.load_book(book_id).get("outline") is not None

    def save_chapter(self, book_id: str, chapter: Dict[str, Any]) -> None:
        ch_id = int(chapter["id"])
        path = self._book_dir(book_id) / "chapters" / f"ch{ch_id:04d}.json"
        existing = _read_json(path) or {}
        merged = {**existing, **chapter}
        _write_json(path, merged)

    def load_chapters(self, book_id: str) -> List[Dict[str, Any]]:
        directory = self._book_dir(book_id) / "chapters"
        if not directory.exists():
            return []
        chapters = []
        for path in sorted(directory.glob("ch*.json")):
            chapter = _read_json(path)
            if chapter is not None:
                chapters.append(chapter)
        return chapters

    def written_chapters(self, book_id: str) -> List[Dict[str, Any]]:
        chapters = [c for c in self.load_chapters(book_id) if c.get("content")]
        chapters.sort(key=lambda c: c["id"])
        return chapters

    def chapter_words(self, chapter: Dict[str, Any]) -> int:
        if not chapter.get("content"):
            return 0
        from ..nstagent.tools import count_words

        return count_words(chapter["content"])

    def book_progress(self, book_id: str) -> tuple[int, int]:
        written = self.written_chapters(book_id)
        words = sum(self.chapter_words(c) for c in written)
        return words, len(written)

    def save_state(self, book_id: str, state: NarrativeState) -> None:
        _write_json(self._book_dir(book_id) / "state" / "current.json", state.to_dict())

    def load_state(self, book_id: str) -> NarrativeState:
        data = _read_json(self._book_dir(book_id) / "state" / "current.json")
        return NarrativeState.from_dict(data or {})

    def save_state_snapshot(self, book_id: str, chapter_id: int, state: NarrativeState) -> None:
        path = self._book_dir(book_id) / "state" / "history" / f"ch{chapter_id:04d}.json"
        _write_json(path, state.to_dict())

    def append_metrics(self, book_id: str, record: Dict[str, Any]) -> None:
        path = self._book_dir(book_id) / "runs" / "metrics.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_metrics(self, book_id: str) -> List[Dict[str, Any]]:
        path = self._book_dir(book_id) / "runs" / "metrics.jsonl"
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def set_author_notes(self, book_id: str, notes: str) -> None:
        path = self._book_dir(book_id) / "author_notes.txt"
        if notes.strip():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(notes, encoding="utf-8")
        elif path.exists():
            path.unlink()

    def pop_author_notes(self, book_id: str) -> str:
        path = self._book_dir(book_id) / "author_notes.txt"
        if not path.exists():
            return ""
        notes = path.read_text(encoding="utf-8")
        path.unlink()
        return notes

    def peek_author_notes(self, book_id: str) -> str:
        path = self._book_dir(book_id) / "author_notes.txt"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")


def _utc_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")