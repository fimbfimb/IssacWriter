from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..llm.provider import LlmClient, LlmError, LlmProfile
from ..llm.profiles import ProfileStore
from ..llm.provider import extract_json
from ..nstagent.prompts import CHAPTER_ANCHORS, MAX_CORRECT_PER_CHAPTER, MAX_READ, MAX_SEARCH, MAX_TURNS_PER_CHAPTER, SEARCH_MAX_HITS
from ..nstagent.runner import RunnerRegistry
from ..stats.mentions import build_stats, export_markdown, export_txt
from ..store.codex import merge_extracted
from ..store.project import BookStore

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("STORYFORGE_DATA") or ROOT / "data")
FRONTEND_DIST = Path(os.environ.get("STORYFORGE_FRONTEND") or ROOT / "frontend" / "dist")


class ProfileIn(BaseModel):
    name: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    max_output_tokens: int = 32768
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    thinking: str = "default"
    extra_params: Optional[Dict[str, Any]] = None


class BookIn(BaseModel):
    title: str
    story_prompt: str
    language: str = "en"
    target_words: int = 10000
    genre_notes: str = ""


class CodexEntry(BaseModel):
    name: str = ""
    aliases: List[str] = Field(default_factory=list)
    description: str = ""
    role: str = ""
    color: str = ""


class CodexIn(BaseModel):
    characters: List[CodexEntry] = Field(default_factory=list)
    settings: List[CodexEntry] = Field(default_factory=list)
    lore: List[CodexEntry] = Field(default_factory=list)


class RunConfigIn(BaseModel):
    planner_profile_id: Optional[str] = None
    writer_profile_id: Optional[str] = None


class ReviewIn(BaseModel):
    action: str = "approve"
    content: str = ""
    note: str = ""


class PatchBook(BaseModel):
    title: Optional[str] = None
    story_prompt: Optional[str] = None
    genre_notes: Optional[str] = None
    language: Optional[str] = None
    target_words: Optional[int] = None
    run_settings: Optional[Dict[str, Any]] = None
    planner_profile_id: Optional[str] = None
    writer_profile_id: Optional[str] = None


class PatchChapter(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None


class StatePatch(BaseModel):
    collection: str
    key_or_name: str
    description: str = ""
    delete: bool = False


def create_app() -> FastAPI:
    app = FastAPI(title="IsaacWriter", version="0.1.0")
    store = BookStore(DATA_ROOT)
    profiles = ProfileStore(DATA_ROOT / "profiles.json")
    runners = RunnerRegistry(store)

    def get_book_or_404(book_id: str) -> Dict[str, Any]:
        try:
            return store.load_book(book_id)
        except FileNotFoundError:
            raise HTTPException(404, f"book {book_id} not found")

    def profile_or_404(profile_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if profile_id is None:
            return None
        profile = profiles.get(profile_id)
        if profile is None:
            raise HTTPException(404, f"profile {profile_id} not found")
        return profile

    @app.get("/api/health")
    def health():
        return {"ok": True, "app": "IsaacWriter"}

    @app.get("/api/settings")
    def settings():
        return {
            "anchors": CHAPTER_ANCHORS,
            "budgets": {
                "read": MAX_READ,
                "search": MAX_SEARCH,
                "search_hits": SEARCH_MAX_HITS,
                "correct": MAX_CORRECT_PER_CHAPTER,
                "max_turns": MAX_TURNS_PER_CHAPTER,
            },
            "data_root": str(DATA_ROOT),
        }

    @app.get("/api/profiles")
    def list_profiles():
        return {"profiles": profiles.list()}

    @app.post("/api/profiles")
    def create_profile(payload: ProfileIn):
        profile = profiles.create(payload.model_dump())
        return {"profile": profiles.safe(profile)}

    @app.patch("/api/profiles/{profile_id}")
    def update_profile(profile_id: str, payload: ProfileIn):
        existing = profile_or_404(profile_id) or {}
        data = payload.model_dump()
        if not data.get("api_key"):
            data["api_key"] = existing.get("api_key", "")
        if data.get("extra_params") is None:
            data["extra_params"] = existing.get("extra_params", {})
        profiles.update(profile_id, data)
        return {"profile": profiles.safe(profiles.get(profile_id))}

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile(profile_id: str):
        profile_or_404(profile_id)
        profiles.delete(profile_id)
        return {"ok": True}

    @app.get("/api/profiles/{profile_id}/models")
    async def profile_models(profile_id: str):
        profile = profile_or_404(profile_id)
        client = LlmClient(LlmProfile(**profile))
        try:
            models = await client.list_models()
        except LlmError as exc:
            raise HTTPException(502, f"connection failed: {exc}")
        return {"models": models}

    @app.get("/api/books")
    def list_books():
        return {"books": store.list_books()}

    @app.post("/api/books")
    def create_book(payload: BookIn):
        book = store.create_book(
            payload.title,
            payload.story_prompt,
            payload.language,
            payload.target_words,
            payload.genre_notes,
        )
        return {"book": book}

    @app.get("/api/books/{book_id}")
    def get_book(book_id: str):
        book = get_book_or_404(book_id)
        words, chapters = store.book_progress(book_id)
        book = dict(book)
        book["words_written"] = words
        book["chapters_written"] = chapters
        book["runner"] = runners.status(book_id)
        book["author_notes"] = store.peek_author_notes(book_id)
        return {"book": book}

    @app.patch("/api/books/{book_id}")
    def patch_book(book_id: str, payload: PatchBook):
        book = get_book_or_404(book_id)
        data = payload.model_dump(exclude_none=True)
        runner = runners.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, "runner busy; stop or wait before editing the book")
        for key, value in data.items():
            book[key] = value
        outline = book.get("outline")
        if outline is not None and data.get("target_words") and not outline.get("frozen"):
            from ..nstagent.planner import normalize_outline_word_targets

            outline["chapters"] = normalize_outline_word_targets(
                outline["chapters"], int(data["target_words"])
            )
            outline["target_words"] = int(data["target_words"])
        store.save_book(book)
        return {"book": book}

    @app.delete("/api/books/{book_id}")
    def delete_book(book_id: str):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, "runner busy; stop the run first")
        store.delete_book(book_id)
        runner._task = None
        runner.status = "stopped"
        return {"ok": True}

    @app.put("/api/books/{book_id}/codex")
    def replace_codex(book_id: str, payload: CodexIn):
        book = get_book_or_404(book_id)
        book["codex"] = payload.model_dump()
        store.save_book(book)
        return {"codex": book["codex"]}

    @app.post("/api/books/{book_id}/codex/extract")
    async def extract_codex(book_id: str, payload: RunConfigIn):
        book = get_book_or_404(book_id)
        outline = book.get("outline") or {}
        if not outline.get("premise"):
            raise HTTPException(400, "no outline to extract from")
        profile_id = (
            payload.planner_profile_id
            or book.get("planner_profile_id")
        )
        profile = profile_or_404(profile_id)
        if profile is None:
            raise HTTPException(400, "planner_profile_id required for extraction")

        parts: List[str] = [f"Premise:\n{outline.get('premise', '')}"]
        if outline.get("synopsis"):
            parts.append(f"Detailed synopsis:\n{outline['synopsis']}")
        if outline.get("acts"):
            parts.append(f"Acts:\n{outline['acts']}")
        chapters = outline.get("chapters") or []
        if chapters:
            compact = [
                f"{ch.get('name', '')}: {ch.get('description', '')}" for ch in chapters
            ]
            parts.append("Chapters:\n" + "\n".join(compact))
        codex = book.get("codex") or {}
        existing_characters = ", ".join(c.get("name", "") for c in codex.get("characters") or []) or "none yet"
        existing_settings = ", ".join(c.get("name", "") for c in codex.get("settings") or []) or "none yet"

        messages = [
            {
                "role": "system",
                "content": "You extract story entities from a novel outline and respond ONLY with JSON.",
            },
            {
                "role": "user",
                "content": (
                    "Outline:\n\n" + "\n\n".join(parts) + "\n\n"
                    f"Already authored codex entries (characters: {existing_characters}; settings: {existing_settings}) — "
                    "do not repeat them.\n\n"
                    "Extract every named character that drives the plot (4-12, main and memorable supporting cast) "
                    "and every named location or setting that matters (2-8). Respond ONLY as JSON:\n"
                    "{\n"
                    '  "characters": [{"name": "Full Name", "aliases": ["short spellings used in the outline"], '
                    '"role": "protagonist|antagonist|supporting|...", "description": "1-2 sentences: who they are and what they want"}],\n'
                    '  "settings": [{"name": ...,"aliases": [...], "description": "1 sentence"}]\n'
                    "}\n"
                    "Rules: only entities actually present in the outline; aliases must include spellings the outline "
                    "itself uses; write descriptions in the same language as the outline; keep names stable across chapters."
                ),
            },
        ]
        client = LlmClient(LlmProfile(**profile))
        try:
            response = await client.chat(messages, temperature=0.3)
            payload_json = extract_json(response.content)
        except (LlmError, ValueError) as exc:
            raise HTTPException(502, f"extraction failed: {exc}")
        if not isinstance(payload_json, dict):
            raise HTTPException(502, "extraction produced no JSON object")
        report = merge_extracted(book["codex"], payload_json)
        store.save_book(book)
        added = {kind: len(values["added"]) for kind, values in report.items()}
        return {"codex": book["codex"], "added": added, "added_names": report}

    @app.post("/api/books/{book_id}/outline/generate")
    async def generate_outline(book_id: str, payload: RunConfigIn):
        book = get_book_or_404(book_id)
        runner = runners.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, f"runner busy in status {runner.status}")
        planner = profile_or_404(payload.planner_profile_id)
        await runner.start(
            "outline",
            {
                "planner_profile": planner,
            },
        )
        return {"status": runner._meta()}

    @app.post("/api/books/{book_id}/outline/regenerate-stage")
    async def regenerate_stage(book_id: str, payload: Dict[str, Any]):
        book = get_book_or_404(book_id)
        stage = payload.get("stage")
        if stage not in ("premise", "synopsis", "acts", "chapters"):
            raise HTTPException(400, "stage must be premise|synopsis|acts|chapters")
        outline = book.get("outline")
        if not outline:
            raise HTTPException(400, "no outline yet")
        runner = runners.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, f"runner busy in status {runner.status}")
        if stage == "premise":
            outline = {"frozen": False}
            book["outline"] = outline
        elif stage == "synopsis":
            outline["synopsis"] = None
            outline["acts"] = None
            outline.pop("chapters", None)
        elif stage == "acts":
            outline["acts"] = None
            outline.pop("chapters", None)
        else:
            outline.pop("chapters", None)
        store.save_book(book)
        planner = profile_or_404(payload.get("planner_profile_id") or book.get("planner_profile_id"))
        await runner.start("outline", {"planner_profile": planner})
        return {"status": runner._meta()}

    @app.post("/api/books/{book_id}/outline/freeze")
    def freeze_outline(book_id: str):
        book = get_book_or_404(book_id)
        outline = book.get("outline")
        if not outline or not outline.get("chapters"):
            raise HTTPException(400, "no chapters to freeze")
        outline["frozen"] = True
        book["status"] = "ready"
        store.save_book(book)
        return {"outline": outline}

    @app.patch("/api/books/{book_id}/outline")
    def patch_outline(book_id: str, payload: Dict[str, Any]):
        book = get_book_or_404(book_id)
        outline = book.get("outline")
        if not outline:
            raise HTTPException(400, "no outline yet")
        for key in ("premise", "synopsis", "acts"):
            if key in payload:
                outline[key] = payload[key]
        if "chapters" in payload:
            outline["chapters"] = payload["chapters"]
        if "frozen" in payload:
            outline["frozen"] = bool(payload["frozen"])
        store.save_book(book)
        return {"outline": outline}

    @app.get("/api/books/{book_id}/chapters")
    def list_chapters(book_id: str):
        get_book_or_404(book_id)
        return {"chapters": store.written_chapters(book_id)}

    @app.get("/api/books/{book_id}/chapters/{chapter_id}")
    def get_chapter(book_id: str, chapter_id: int):
        get_book_or_404(book_id)
        for chapter in store.load_chapters(book_id):
            if chapter["id"] == chapter_id:
                return {"chapter": chapter}
        raise HTTPException(404, f"chapter {chapter_id} not found")

    @app.patch("/api/books/{book_id}/chapters/{chapter_id}")
    def patch_chapter(book_id: str, chapter_id: int, payload: PatchChapter):
        get_book_or_404(book_id)
        chapters = store.load_chapters(book_id)
        chapter = next((c for c in chapters if c["id"] == chapter_id), None)
        if chapter is None:
            raise HTTPException(404, f"chapter {chapter_id} not found")
        from ..nstagent.tools import count_words

        if payload.name is not None:
            chapter["name"] = payload.name
        if payload.content is not None:
            chapter["content"] = payload.content
            chapter["written_words"] = count_words(payload.content)
            chapter["status"] = "accepted"
        store.save_chapter(book_id, chapter)
        return {"chapter": chapter}

    @app.get("/api/books/{book_id}/state")
    def get_state(book_id: str):
        get_book_or_404(book_id)
        state = store.load_state(book_id)
        return {"state": state.to_dict()}

    @app.patch("/api/books/{book_id}/state")
    def patch_state(book_id: str, payload: StatePatch):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, "runner busy; pause first to edit state")
        state = store.load_state(book_id)
        if payload.collection == "character_states":
            if payload.delete:
                state.remove_character(payload.key_or_name)
            else:
                state.set_character(payload.key_or_name, payload.description)
        elif payload.collection == "past_events":
            events = [e for e in state.past_events if e.get("key") != payload.key_or_name]
            if payload.delete:
                state.past_events = events
            else:
                found = next(
                    (e for e in state.past_events if e.get("key") == payload.key_or_name), None
                )
                if found:
                    found["description"] = payload.description
        elif payload.collection == "future_requirements":
            found = next(
                (e for e in state.future_requirements if e.get("key") == payload.key_or_name),
                None,
            )
            if payload.delete and found:
                state.future_requirements.remove(found)
            elif found:
                found["description"] = payload.description
        else:
            raise HTTPException(400, "invalid collection")
        store.save_state(book_id, state)
        return {"state": state.to_dict()}

    @app.post("/api/books/{book_id}/run/start")
    async def run_start(book_id: str, payload: RunConfigIn):
        book = get_book_or_404(book_id)
        runner = runners.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, f"runner busy in status {runner.status}")
        writer_profile = profile_or_404(payload.writer_profile_id or book.get("writer_profile_id"))
        if writer_profile is None:
            raise HTTPException(400, "writer_profile_id required")
        book["writer_profile_id"] = writer_profile["id"]
        if payload.planner_profile_id:
            book["planner_profile_id"] = payload.planner_profile_id
        book["status"] = "writing"
        store.save_book(book)
        await runner.start("story", {"writer_profile": dict(writer_profile)})
        return {"status": runner._meta()}

    @app.post("/api/books/{book_id}/run/pause")
    async def run_pause(book_id: str):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        runner.request_pause()
        return {"status": runner._meta()}

    @app.post("/api/books/{book_id}/run/resume")
    async def run_resume(book_id: str):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        runner.request_resume()
        return {"status": runner._meta()}

    @app.post("/api/books/{book_id}/run/stop")
    async def run_stop(book_id: str):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        runner.request_stop()
        if runner._task is not None and not runner._task.done():
            runner.request_resume()
        return {"status": runner._meta()}

    @app.post("/api/books/{book_id}/run/abort")
    async def run_abort(book_id: str):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        message = runner.request_abort()
        return {"status": runner._meta(), "message": message}

    @app.post("/api/books/{book_id}/run/review")
    async def run_review(book_id: str, payload: ReviewIn):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        try:
            runner.submit_review_nowait(payload.model_dump())
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"status": runner._meta()}

    @app.get("/api/books/{book_id}/run/status")
    def run_status(book_id: str):
        get_book_or_404(book_id)
        return {"status": runners.get(book_id)._meta()}

    @app.put("/api/books/{book_id}/notes")
    def put_notes(book_id: str, payload: Dict[str, str]):
        get_book_or_404(book_id)
        store.set_author_notes(book_id, payload.get("notes", ""))
        return {"author_notes": store.peek_author_notes(book_id)}

    @app.get("/api/books/{book_id}/events")
    async def events(book_id: str):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        queue = await runner.subscribe()

        async def generator():
            try:
                yield ":" * 120 + "\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("type") in ("runner_stopped", "runner_error", "outline_done"):
                        continue
            except asyncio.CancelledError:
                pass
            finally:
                runner.unsubscribe(queue)

        return StreamingResponse(
            generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/books/{book_id}/stats")
    def stats(book_id: str):
        get_book_or_404(book_id)
        return build_stats(store, book_id)

    @app.get("/api/books/{book_id}/export")
    def export(book_id: str, format: str = "md"):
        get_book_or_404(book_id)
        if format == "md":
            return JSONResponse({"content": export_markdown(store, book_id)})
        if format == "txt":
            return JSONResponse({"content": export_txt(store, book_id)})
        if format == "markdown_file":
            return StreamingResponse(
                iter([export_markdown(store, book_id)]), media_type="text/markdown",
                headers={"Content-Disposition": f"attachment; filename={book_id}.md"},
            )
        if format == "csv":
            rows = ["chapter_id,name,words,target,attempts,turns,prompt_tokens,completion_tokens,cost"]
            for record in store.load_metrics(book_id):
                usage = record.get("usage") or {}
                name = ""
                for chapter in store.load_chapters(book_id):
                    if chapter["id"] == record.get("chapter_id"):
                        name = (chapter.get("name") or "").replace(",", ";")
                rows.append(
                    ",".join(
                        str(x)
                        for x in (
                            record.get("chapter_id"),
                            name,
                            record.get("word_count"),
                            record.get("target"),
                            record.get("attempts"),
                            "",
                            usage.get("prompt_tokens"),
                            usage.get("completion_tokens"),
                            usage.get("cost"),
                        )
                    )
                )
            return StreamingResponse(
                iter(["\n".join(rows) + "\n"]), media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={book_id}_stats.csv"},
            )
        raise HTTPException(400, "format must be md|txt|markdown_file|csv")

    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="static")

    return app


app = create_app()