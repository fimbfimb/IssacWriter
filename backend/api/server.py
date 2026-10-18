from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..export.layout import (
    alias_pattern,
    anchor_valid_for,
    book_word_positions,
    call_llm,
    character_visuals_in,
    compose_prompt,
    compute_positions,
    context_window,
    describe_messages,
    load_layout,
    parse_describe,
    parse_visuals,
    passage_chunks_for,
    save_layout,
    visual_extract_messages,
)
from ..export.epub import build_epub, slugify_filename
from ..llm.provider import LlmClient, LlmError, LlmProfile
from ..llm.profiles import ProfileStore
from ..llm.provider import extract_json
from ..nstagent.prompts import CHAPTER_ANCHORS, MAX_CORRECT_PER_CHAPTER, MAX_READ, MAX_SEARCH, MAX_TURNS_PER_CHAPTER, SEARCH_MAX_HITS
from ..nstagent.runner import RunnerRegistry
from ..refine import engine as refine_engine
from ..refine.runner import RefineRegistry
from ..refine import outline_engine
from ..refine.outline_runner import OutlineRefineRegistry
from ..sequel import create_sequel_book, prepare_sequel
from ..stats.mentions import build_stats, export_markdown, export_txt
from ..store.codex import merge_extracted
from ..store.project import BookStore
from ..translation.authorial import AuthorialWriter
from ..translation.review import (
    ReviewError,
    add_read_marks as review_add_read_marks,
    apply_decision,
    build_review_response,
    collect_rows,
    current_report as review_current_report,
    fix_paragraph as review_fix_paragraph,
    previous_german_text,
    retranslate_batch,
)
from ..translation.review import _restore_writer as restore_writer
from ..translation.runner import TranslationRegistry, _slugify as slugify_language

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
    language: Optional[str] = None
    target_words: Optional[int] = None
    run_settings: Optional[Dict[str, Any]] = None
    translation_settings: Optional[Dict[str, Any]] = None
    refine_settings: Optional[Dict[str, Any]] = None
    outline_refine_settings: Optional[Dict[str, Any]] = None
    refined_descriptions_source: Optional[bool] = None
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


class TranslateIn(BaseModel):
    target_language: str = "German"
    mode: str = "tool"
    profile_id: Optional[str] = None
    candidate_profile_ids: List[str] = Field(default_factory=list)
    judge_profile_id: Optional[str] = None
    unload_after_call: bool = True
    chapter_ids: Optional[List[int]] = None
    single_translate: bool = False
    test_num: int = 0
    batch_size: int = 10
    resume: bool = False
    use_refined: bool = False
    # authorial mode
    draft_profile_id: Optional[str] = None
    refiner_profile_id: Optional[str] = None
    checker_profile_id: Optional[str] = None
    style_probe: str = ""
    draft_temperature: float = 0.7
    refine_rounds: int = 2
    similarity_stop: float = 0.97
    ratio_min: float = 0.85
    ratio_max: float = 1.45
    batch_words: int = 0
    structured: bool = True
    check_mode: str = "essential"


class RefineIn(BaseModel):
    language: str = "English"
    draft_profile_id: Optional[str] = None
    refiner_profile_id: Optional[str] = None
    checker_profile_id: Optional[str] = None
    unload_after_call: bool = True
    chapter_ids: Optional[List[int]] = None
    resume: bool = False
    batch_words: int = 0
    refine_rounds: int = 2
    similarity_stop: float = 0.97
    ratio_min: float = 0.75
    ratio_max: float = 1.5
    draft_temperature: float = 0.7
    style_probe: str = ""
    structured: bool = True
    check_mode: str = "essential"


class RefineSelectIn(BaseModel):
    chapter_id: int
    version: int


class OutlineRefineIn(BaseModel):
    profile_id: str
    chapter_ids: Optional[List[int]] = None
    resume: bool = False
    refine_rounds: int = 1
    similarity_stop: float = 0.95
    context_prev: int = 3
    language: str = "English"
    unload_after_call: bool = True
    structured: bool = True
    style_probe: str = ""


class OutlineRefineSelectIn(BaseModel):
    chapter_id: int
    version: int


class SequelPrepareIn(BaseModel):
    idea: str
    planner_profile_id: Optional[str] = None
    unload_after_call: bool = True


class SequelCreateIn(BaseModel):
    proposal: Dict[str, Any] = Field(default_factory=dict)
    keep_character_names: List[str] = Field(default_factory=list)
    idea: str = ""
    target_words: Optional[int] = None
    include_dead_as_flashbacks: bool = False


class ReviewDecideIn(BaseModel):
    batch_index: int
    chapter_id: Optional[int] = None
    type: str
    paragraph: Optional[int] = None
    detail: str
    action: str
    text: Optional[str] = None


class LayoutIn(BaseModel):
    style_directive: Optional[str] = None
    words_per_page: Optional[int] = None
    images_every_n_pages: Optional[int] = None
    cover_image_path: Optional[str] = None
    characters: Optional[List[Dict[str, Any]]] = None
    illustrations: Optional[List[Dict[str, Any]]] = None


def create_app() -> FastAPI:
    app = FastAPI(title="IsaacWriter", version="0.1.0")
    store = BookStore(DATA_ROOT)
    profiles = ProfileStore(DATA_ROOT / "profiles.json")
    runners = RunnerRegistry(store)
    translations = TranslationRegistry(store)
    refines = RefineRegistry(store)
    outline_refines = OutlineRefineRegistry(store)

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
        book["translation"] = translations.get(book_id)._meta()
        book["refine"] = refines.get(book_id)._meta()
        book["author_notes"] = store.peek_author_notes(book_id)
        return {"book": book}

    @app.patch("/api/books/{book_id}")
    def patch_book(book_id: str, payload: PatchBook):
        book = get_book_or_404(book_id)
        data = payload.model_dump(exclude_none=True)
        translation_settings = data.pop("translation_settings", None)
        refine_settings = data.pop("refine_settings", None)
        outline_refine_settings = data.pop("outline_refine_settings", None)
        refined_descriptions_source = data.pop("refined_descriptions_source", None)
        saved = False
        if data:
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
            saved = True
        if translation_settings is not None:
            # translator settings are editable while a translation runs
            book["translation_settings"] = translation_settings
            saved = True
        if refine_settings is not None:
            # refine settings are editable while a refinement runs
            book["refine_settings"] = refine_settings
            saved = True
        if outline_refine_settings is not None:
            book["outline_refine_settings"] = outline_refine_settings
            saved = True
        if refined_descriptions_source is not None:
            # writer source toggle — takes effect at the next writing run
            book["refined_descriptions_source"] = bool(refined_descriptions_source)
            saved = True
        if saved:
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

    @app.post("/api/books/{book_id}/translate")
    async def translate_start(book_id: str, payload: TranslateIn):
        book = get_book_or_404(book_id)
        mode = (payload.mode or "tool").strip().lower()
        config: Dict[str, Any] = {
            "target_language": payload.target_language,
            "mode": mode,
            "chapter_ids": payload.chapter_ids,
            "single_translate": payload.single_translate,
            "test_num": payload.test_num,
            "batch_size": payload.batch_size,
            "resume": payload.resume,
            "use_refined": payload.use_refined,
        }
        if mode == "tool":
            profile = profile_or_404(payload.profile_id or book.get("writer_profile_id"))
            if profile is None:
                raise HTTPException(400, "profile_id required (no default profile connected yet)")
            config["profile"] = dict(profile)
        elif mode == "ensemble":
            candidates = []
            seen: List[str] = []
            for raw_id in payload.candidate_profile_ids:
                if not raw_id or raw_id in seen:
                    continue
                seen.append(raw_id)
                candidates.append(dict(profile_or_404(raw_id)))
            if not candidates:
                raise HTTPException(400, "ensemble mode needs at least one candidate profile")
            if len(candidates) > 6:
                raise HTTPException(400, "maximum 6 candidate profiles")
            config["candidates"] = candidates
            config["judge"] = dict(profile_or_404(payload.judge_profile_id)) if payload.judge_profile_id else None
            config["unload_after_call"] = payload.unload_after_call
        elif mode == "authorial":
            draft = dict(profile_or_404(payload.draft_profile_id)) if payload.draft_profile_id else None
            refiner = dict(profile_or_404(payload.refiner_profile_id)) if payload.refiner_profile_id else None
            if draft is None:
                raise HTTPException(400, "draft_profile_id required (authorial mode)")
            if refiner is None:
                raise HTTPException(400, "refiner_profile_id required (authorial mode)")
            checker = dict(profile_or_404(payload.checker_profile_id)) if payload.checker_profile_id else None
            config["draft"] = draft
            config["refiner"] = refiner
            config["checker"] = checker
            config["unload_after_call"] = payload.unload_after_call
            config["style_probe"] = payload.style_probe
            config["draft_temperature"] = payload.draft_temperature
            config["refine_rounds"] = payload.refine_rounds
            config["similarity_stop"] = payload.similarity_stop
            config["ratio_min"] = payload.ratio_min
            config["ratio_max"] = payload.ratio_max
            config["batch_words"] = payload.batch_words
            config["structured"] = payload.structured
            config["check_mode"] = payload.check_mode if payload.check_mode in ("essential", "strict") else "essential"
        else:
            raise HTTPException(400, "mode must be 'tool', 'ensemble' or 'authorial'")
        await translations.get(book_id).start(config)
        return {"status": translations.get(book_id)._meta()}

    @app.get("/api/books/{book_id}/translate/progress")
    def translate_progress(book_id: str):
        get_book_or_404(book_id)
        status = translations.get(book_id)._meta()
        language = status.get("language") or "German"
        base = (DATA_ROOT / str(book_id) / "translations").resolve()
        report: Dict[str, Any] | None = None
        for name in ("authorial_report.json", "ensemble_report.json"):
            path = base / slugify_language(language) / name
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if candidate:
                report = candidate
                break
        chapters: List[Dict[str, Any]] = []
        if report and report.get("chapters"):
            chapters = report["chapters"]
        elif report and report.get("batches"):
            grouped: Dict[int, Dict[str, Any]] = {}
            for entry in report["batches"]:
                cid = int(entry.get("chapter_id") or 0)
                row = grouped.setdefault(
                    cid,
                    {
                        "chapter_id": cid,
                        "chapter_name": entry.get("chapter_name") or "Untitled",
                        "batches_total": 0,
                        "batches_done": 0,
                        "paragraphs": 0,
                        "review_flags": 0,
                        "started": True,
                        "done": False,
                    },
                )
                row["batches_total"] += 1
                done_batch = (
                    entry.get("stage") == "done" if entry.get("stage") else bool(entry.get("winner_text"))
                )
                if done_batch:
                    row["batches_done"] += 1
                    row["paragraphs"] += sum(
                        1 for item in (entry.get("items") or []) if item.get("text")
                    ) or (1 if entry.get("winner_text") else 0)
                row["done"] = row["batches_done"] == row["batches_total"]
            chapters = [grouped[key] for key in sorted(grouped)]
        return {
            "language": language,
            "mode": (report or {}).get("mode") or status.get("mode"),
            "run_status": status.get("status"),
            "batches_total": (report or {}).get("batches_total"),
            "chapters": chapters,
        }

    @app.get("/api/books/{book_id}/translate/status")
    def translate_status(book_id: str):
        get_book_or_404(book_id)
        return {"status": translations.get(book_id)._meta()}

    @app.post("/api/books/{book_id}/translate/stop")
    async def translate_stop(book_id: str):
        get_book_or_404(book_id)
        runner = translations.get(book_id)
        runner.stop()
        return {"status": runner._meta()}

    @app.get("/api/books/{book_id}/translate/outputs")
    def translate_outputs(book_id: str):
        get_book_or_404(book_id)
        base = (DATA_ROOT / str(book_id) / "translations").resolve()
        outputs = []
        if base.exists():
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    stat = path.stat()
                    outputs.append(
                        {
                            "path": str(path.relative_to(base)),
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        }
                    )
        return {"outputs": outputs}

    @app.get("/api/books/{book_id}/translate/file")
    def translate_file(book_id: str, path: str):
        get_book_or_404(book_id)
        base = (DATA_ROOT / str(book_id) / "translations").resolve()
        target = (base / path).resolve()
        if not str(target).startswith(str(base)):
            raise HTTPException(400, "invalid path")
        if target.suffix.lower() not in (".txt", ".md", ".json"):
            raise HTTPException(400, "only .txt/.md/.json can be previewed")
        if not target.is_file():
            raise HTTPException(404, f"no such output: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"path": path, "content": content[:400_000]}

    # ------------------------------------------------------------------ refine

    def _refine_report(book_id: str) -> Optional[Dict[str, Any]]:
        path = DATA_ROOT / str(book_id) / "refines" / "refine_report.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @app.post("/api/books/{book_id}/refine")
    async def refine_start(book_id: str, payload: RefineIn):
        get_book_or_404(book_id)
        draft = dict(profile_or_404(payload.draft_profile_id)) if payload.draft_profile_id else None
        refiner = dict(profile_or_404(payload.refiner_profile_id)) if payload.refiner_profile_id else None
        if draft is None:
            raise HTTPException(400, "draft_profile_id required (refine)")
        if refiner is None:
            raise HTTPException(400, "refiner_profile_id required (refine)")
        checker = dict(profile_or_404(payload.checker_profile_id)) if payload.checker_profile_id else None
        config: Dict[str, Any] = {
            "language": payload.language,
            "draft": draft,
            "refiner": refiner,
            "checker": checker,
            "unload_after_call": payload.unload_after_call,
            "chapter_ids": payload.chapter_ids,
            "resume": payload.resume,
            "batch_words": payload.batch_words,
            "refine_rounds": payload.refine_rounds,
            "similarity_stop": payload.similarity_stop,
            "ratio_min": payload.ratio_min,
            "ratio_max": payload.ratio_max,
            "draft_temperature": payload.draft_temperature,
            "style_probe": payload.style_probe,
            "structured": payload.structured,
            "check_mode": payload.check_mode,
        }
        await refines.get(book_id).start(config)
        return {"status": refines.get(book_id)._meta()}

    @app.get("/api/books/{book_id}/refine/status")
    def refine_status(book_id: str):
        get_book_or_404(book_id)
        return {"status": refines.get(book_id)._meta()}

    @app.post("/api/books/{book_id}/refine/stop")
    async def refine_stop(book_id: str):
        get_book_or_404(book_id)
        runner = refines.get(book_id)
        runner.stop()
        return {"status": runner._meta()}

    @app.get("/api/books/{book_id}/refine/progress")
    def refine_progress(book_id: str):
        get_book_or_404(book_id)
        status = refines.get(book_id)._meta()
        report = _refine_report(book_id)
        chapters: List[Dict[str, Any]] = []
        if report and report.get("chapters"):
            chapters = [ch for ch in report["chapters"] if isinstance(ch, dict)]
        elif report and report.get("batches"):
            grouped: Dict[int, Dict[str, Any]] = {}
            for entry in report["batches"]:
                cid = int(entry.get("chapter_id") or 0)
                row = grouped.setdefault(
                    cid,
                    {
                        "chapter_id": cid,
                        "chapter_name": entry.get("chapter_name") or "Untitled",
                        "words_total": 0,
                        "refined_words": 0,
                        "batches_total": 0,
                        "batches_done": 0,
                        "paragraphs": 0,
                        "review_flags": 0,
                        "started": True,
                        "done": False,
                        "versions": refine_engine.existing_versions(
                            DATA_ROOT / str(book_id) / "refined", cid
                        ),
                        "active_version": refine_engine.active_version(
                            DATA_ROOT / str(book_id) / "refined", cid
                        ),
                    },
                )
                row["batches_total"] += 1
                if entry.get("stage") == "done":
                    row["batches_done"] += 1
                    for item in entry.get("items") or []:
                        row["refined_words"] += len((item.get("text") or "").split())
                        row["paragraphs"] += 1 if item.get("text") else 0
                row["review_flags"] += len(
                    [
                        flag
                        for flag in entry.get("review") or []
                        if flag.get("type") != "annotation" and not flag.get("decision")
                    ]
                )
                row["done"] = row["batches_done"] == row["batches_total"]
            chapters = [grouped[key] for key in sorted(grouped)]
        return {
            "mode": (report or {}).get("mode") or status.get("mode"),
            "run_status": status.get("status"),
            "batches_total": (report or {}).get("batches_total"),
            "chapters": chapters,
        }

    @app.get("/api/books/{book_id}/refine/outputs")
    def refine_outputs(book_id: str):
        get_book_or_404(book_id)
        base = (DATA_ROOT / str(book_id)).resolve()
        outputs = []
        for sub in ("refined", "refines"):
            root = base / sub
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    stat = path.stat()
                    outputs.append(
                        {
                            "path": f"{sub}/{path.relative_to(root).as_posix()}",
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        }
                    )
        return {"outputs": outputs}

    @app.get("/api/books/{book_id}/refine/file")
    def refine_file(book_id: str, path: str):
        get_book_or_404(book_id)
        base = (DATA_ROOT / str(book_id)).resolve()
        target = (base / path).resolve()
        if not str(target).startswith(str(base)):
            raise HTTPException(400, "invalid path")
        relative = target.relative_to(base).as_posix()
        if not (relative.startswith("refined/") or relative.startswith("refines/")):
            raise HTTPException(400, "path must point into refined/ or refines/")
        if target.suffix.lower() not in (".txt", ".md", ".json"):
            raise HTTPException(400, "only .txt/.md/.json can be previewed")
        if not target.is_file():
            raise HTTPException(404, f"no such output: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"path": relative, "content": content[:400_000]}

    @app.get("/api/books/{book_id}/refine/versions")
    def refine_versions(book_id: str):
        get_book_or_404(book_id)
        book_dir = DATA_ROOT / str(book_id)
        refined = book_dir / "refined"
        chapters: List[Dict[str, Any]] = []
        for chapter in store.written_chapters(book_id):
            cid = int(chapter["id"])
            versions: List[Dict[str, Any]] = []
            for number in refine_engine.existing_versions(refined, cid):
                payload = refine_engine.read_version(refined, cid, number) or {}
                versions.append(
                    {
                        "version": number,
                        "created": payload.get("created"),
                        "words": payload.get("words") or 0,
                        "review_flags": payload.get("review_flags") or 0,
                        "models": payload.get("models") or {},
                        "run_started_at": payload.get("run_started_at"),
                    }
                )
            active = refine_engine.active_version(refined, cid) if versions else None
            chapters.append(
                {
                    "chapter_id": cid,
                    "chapter_name": chapter.get("name") or "Untitled",
                    "original_words": len((chapter.get("content") or "").split()),
                    "active_version": active,
                    "versions": versions,
                }
            )
        return {"chapters": chapters}

    @app.get("/api/books/{book_id}/refine/version")
    def refine_version(book_id: str, chapter_id: int, version: Optional[int] = None):
        get_book_or_404(book_id)
        refined = DATA_ROOT / str(book_id) / "refined"
        payload = refine_engine.read_version(refined, chapter_id, version)
        if payload is None:
            raise HTTPException(404, "no such refined version")
        return payload

    @app.post("/api/books/{book_id}/refine/versions/select")
    def refine_select_version(book_id: str, payload: RefineSelectIn):
        get_book_or_404(book_id)
        runner = refines.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, "refiner busy; stop the run before switching versions")
        try:
            result = refine_engine.set_active_version(
                DATA_ROOT / str(book_id), payload.chapter_id, payload.version
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return {**result, "status": runner._meta()}

    @app.post("/api/books/{book_id}/refine/reset-all")
    async def refine_reset_all(book_id: str):
        get_book_or_404(book_id)
        runner = refines.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, "refiner busy; stop the run before deleting")
        removed = refine_engine.reset_refine_all(DATA_ROOT / str(book_id))
        return {"removed": removed}

    # --------------------------------------------------------- outline refine

    def _outline_refine_report(book_id: str) -> Optional[Dict[str, Any]]:
        path = DATA_ROOT / str(book_id) / "outline_refines" / "outline_refine_report.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @app.post("/api/books/{book_id}/outline-refine")
    async def outline_refine_start(book_id: str, payload: OutlineRefineIn):
        get_book_or_404(book_id)
        profile = dict(profile_or_404(payload.profile_id))
        config: Dict[str, Any] = {
            "profile": profile,
            "chapter_ids": payload.chapter_ids,
            "resume": payload.resume,
            "refine_rounds": payload.refine_rounds,
            "similarity_stop": payload.similarity_stop,
            "context_prev": payload.context_prev,
            "language": payload.language,
            "unload_after_call": payload.unload_after_call,
            "structured": payload.structured,
            "style_probe": payload.style_probe,
        }
        await outline_refines.get(book_id).start(config)
        return {"status": outline_refines.get(book_id)._meta()}

    @app.get("/api/books/{book_id}/outline-refine/status")
    def outline_refine_status(book_id: str):
        get_book_or_404(book_id)
        return {"status": outline_refines.get(book_id)._meta()}

    @app.post("/api/books/{book_id}/outline-refine/stop")
    async def outline_refine_stop(book_id: str):
        get_book_or_404(book_id)
        runner = outline_refines.get(book_id)
        runner.stop()
        return {"status": runner._meta()}

    @app.get("/api/books/{book_id}/outline-refine/progress")
    def outline_refine_progress(book_id: str):
        get_book_or_404(book_id)
        status = outline_refines.get(book_id)._meta()
        report = _outline_refine_report(book_id)
        chapters: List[Dict[str, Any]] = []
        if report and report.get("chapters"):
            chapters = [ch for ch in report["chapters"] if isinstance(ch, dict)]
        else:
            book = get_book_or_404(book_id)
            refined = DATA_ROOT / str(book_id) / "outline_refined"
            for chapter in sorted((book.get("outline") or {}).get("chapters") or [], key=lambda c: int(c["id"])):
                cid = int(chapter["id"])
                if not str(chapter.get("description") or "").strip():
                    continue
                chapters.append(
                    {
                        "chapter_id": cid,
                        "chapter_name": chapter.get("name") or "Untitled",
                        "word_count": int(chapter.get("word_count") or 0),
                        "brief_words": 0,
                        "started": False,
                        "done": False,
                        "review_notes": 0,
                        "versions": outline_engine.existing_versions(refined, cid),
                        "active_version": outline_engine.active_version(refined, cid),
                    }
                )
        return {
            "run_status": status.get("status"),
            "batches_total": (report or {}).get("batches_total"),
            "chapters": chapters,
        }

    @app.get("/api/books/{book_id}/outline-refine/outputs")
    def outline_refine_outputs(book_id: str):
        get_book_or_404(book_id)
        base = (DATA_ROOT / str(book_id)).resolve()
        outputs = []
        for sub in ("outline_refined", "outline_refines"):
            root = base / sub
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    stat = path.stat()
                    outputs.append(
                        {
                            "path": f"{sub}/{path.relative_to(root).as_posix()}",
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        }
                    )
        return {"outputs": outputs}

    @app.get("/api/books/{book_id}/outline-refine/file")
    def outline_refine_file(book_id: str, path: str):
        get_book_or_404(book_id)
        base = (DATA_ROOT / str(book_id)).resolve()
        target = (base / path).resolve()
        if not str(target).startswith(str(base)):
            raise HTTPException(400, "invalid path")
        relative = target.relative_to(base).as_posix()
        if not (relative.startswith("outline_refined/") or relative.startswith("outline_refines/")):
            raise HTTPException(400, "path must point into outline_refined/ or outline_refines/")
        if target.suffix.lower() not in (".txt", ".md", ".json"):
            raise HTTPException(400, "only .txt/.md/.json can be previewed")
        if not target.is_file():
            raise HTTPException(404, f"no such output: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"path": relative, "content": content[:400_000]}

    @app.get("/api/books/{book_id}/outline-refine/versions")
    def outline_refine_versions(book_id: str):
        get_book_or_404(book_id)
        refined = DATA_ROOT / str(book_id) / "outline_refined"
        chapters: List[Dict[str, Any]] = []
        for chapter in sorted(
            (get_book_or_404(book_id).get("outline") or {}).get("chapters") or [],
            key=lambda c: int(c["id"]),
        ):
            cid = int(chapter["id"])
            if not str(chapter.get("description") or "").strip():
                continue
            versions: List[Dict[str, Any]] = []
            for number in outline_engine.existing_versions(refined, cid):
                payload = outline_engine.read_version(refined, cid, number) or {}
                versions.append(
                    {
                        "version": number,
                        "created": payload.get("created"),
                        "words": payload.get("words") or 0,
                        "models": payload.get("models") or {},
                    }
                )
            chapters.append(
                {
                    "chapter_id": cid,
                    "chapter_name": chapter.get("name") or "Untitled",
                    "original_words": len((chapter.get("description") or "").split()),
                    "active_version": outline_engine.active_version(refined, cid) if versions else None,
                    "versions": versions,
                }
            )
        return {"chapters": chapters}

    @app.get("/api/books/{book_id}/outline-refine/version")
    def outline_refine_version(book_id: str, chapter_id: int, version: Optional[int] = None):
        get_book_or_404(book_id)
        payload = outline_engine.read_version(DATA_ROOT / str(book_id) / "outline_refined", chapter_id, version)
        if payload is None:
            raise HTTPException(404, "no such refined outline version")
        return payload

    @app.post("/api/books/{book_id}/outline-refine/versions/select")
    def outline_refine_select_version(book_id: str, payload: OutlineRefineSelectIn):
        get_book_or_404(book_id)
        runner = outline_refines.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, "outline refiner busy; stop the run before switching versions")
        try:
            result = outline_engine.set_active_version(DATA_ROOT / str(book_id), payload.chapter_id, payload.version)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return {**result, "status": runner._meta()}

    @app.post("/api/books/{book_id}/outline-refine/reset-all")
    async def outline_refine_reset_all(book_id: str):
        get_book_or_404(book_id)
        runner = outline_refines.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, "outline refiner busy; stop the run before deleting")
        removed = outline_engine.reset_outline_refine_all(DATA_ROOT / str(book_id))
        return {"removed": removed}

    @app.get("/api/books/{book_id}/events")
    async def events(book_id: str):
        get_book_or_404(book_id)
        runner = runners.get(book_id)
        translations.get(book_id).bind_emit(runner._emit)
        refines.get(book_id).bind_emit(runner._emit)
        outline_refines.get(book_id).bind_emit(runner._emit)
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

    # ------------------------------------------------------------------ #
    # EPUB export (Feature 2)
    # ------------------------------------------------------------------ #

    def _book_image_loader(book_id: str):
        base = (DATA_ROOT / str(book_id)).resolve()

        def load_image(path: str):
            try:
                target = (base / str(path or "").strip().lstrip("/\\")).resolve()
            except Exception:
                return None
            if not str(target).startswith(str(base) + os.sep) and target != base:
                return None
            if target.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp") or not target.is_file():
                return None
            try:
                return target.read_bytes()
            except Exception:
                return None

        return load_image

    def _book_image_target(book_id: str, stem: str, suffix: str) -> Path:
        folder = DATA_ROOT / str(book_id) / "images"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{stem}{suffix}"

    @app.get("/api/books/{book_id}/export/epub")
    def export_epub(book_id: str):
        book = get_book_or_404(book_id)
        chapters = store.written_chapters(book_id)
        if not chapters:
            raise HTTPException(400, "nothing to export — no written chapters yet")
        layout = load_layout(DATA_ROOT, book_id)
        data, warnings = build_epub(
            title=book.get("title") or "Untitled Book",
            chapters=chapters,
            language=book.get("language") or "en",
            author="",
            layout=layout,
            load_image=_book_image_loader(book_id),
        )
        filename = slugify_filename(book.get("title") or "book")
        headers = {"Content-Disposition": f"attachment; filename={filename}"}
        if warnings:
            headers["X-Epub-Warnings"] = "; ".join(warnings)[:600]
        return Response(content=data, media_type="application/epub+zip", headers=headers)

    # ------------------------------------------------------------------ #
    # Sequel (Feature 1) — Fortsetzung vorbereiten
    # ------------------------------------------------------------------ #

    @app.post("/api/books/{book_id}/sequel/prepare")
    async def sequel_prepare(book_id: str, payload: SequelPrepareIn):
        book = get_book_or_404(book_id)
        chapters = store.written_chapters(book_id)
        if not chapters:
            raise HTTPException(400, "no written chapters — write Band 1 first")
        idea = (payload.idea or "").strip()
        if not idea:
            raise HTTPException(400, "idea for Band 2 is required")
        profile = profile_or_404(payload.planner_profile_id or book.get("planner_profile_id"))
        if profile is None:
            raise HTTPException(400, "planner profile required for the extraction")
        proposal = await prepare_sequel(
            profile, book, chapters, idea, unload=payload.unload_after_call
        )
        return {"proposal": proposal}

    @app.post("/api/books/{book_id}/sequel/create")
    def sequel_create(book_id: str, payload: SequelCreateIn):
        book = get_book_or_404(book_id)
        idea = (payload.idea or "").strip()
        if not idea:
            raise HTTPException(400, "idea for Band 2 is required")
        new_book = create_sequel_book(
            store,
            book,
            payload.proposal or {},
            payload.keep_character_names,
            idea,
            payload.target_words,
            payload.include_dead_as_flashbacks,
        )
        return {"book": new_book}

    # ------------------------------------------------------------------ #
    # Review GUI (Feature 4) — flags decide inside the Translate tab
    # ------------------------------------------------------------------ #

    def _review_language(book: Dict[str, Any]) -> str:
        settings = book.get("translation_settings") or {}
        language = settings.get("target_language") if isinstance(settings, dict) else None
        language = language or translations.get(book.get("id") or "").language
        return language or "German"

    @app.get("/api/books/{book_id}/translate/review")
    def translate_review(book_id: str):
        book = get_book_or_404(book_id)
        try:
            return build_review_response(DATA_ROOT, book_id, _review_language(book))
        except ReviewError as exc:
            raise HTTPException(exc.status, exc.message)

    @app.post("/api/books/{book_id}/translate/review/decide")
    def translate_review_decide(book_id: str, payload: ReviewDecideIn):
        book = get_book_or_404(book_id)
        translation_runner = translations.get(book_id)
        if translation_runner._task is not None and not translation_runner._task.done():
            raise HTTPException(409, "translation run is active — review decisions are locked while it runs")
        try:
            payload_row = apply_decision(
                DATA_ROOT,
                book_id,
                _review_language(book),
                book.get("title") or "",
                payload.batch_index,
                payload.type,
                payload.paragraph,
                payload.detail,
                payload.action,
                payload.text,
            )
        except ReviewError as exc:
            raise HTTPException(exc.status, exc.message)
        return {"open_flags": payload_row["open_flags"], "total_flags": payload_row["total_flags"], "review": payload_row}

    @app.post("/api/books/{book_id}/translate/review/annotations/read")
    def translate_annotations_read(book_id: str, payload: Dict[str, Any]):
        """Mark annotations read — writes ONLY the side file, so it stays
        instant even while a translation run is rewriting the report."""
        book = get_book_or_404(book_id)
        ids = (payload or {}).get("ids")
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "ids required")
        language = _review_language(book)
        merged = review_add_read_marks(DATA_ROOT, book_id, language, ids)
        review = build_review_response(DATA_ROOT, book_id, language)
        return {"read_count": len(merged), "review": review}

    @app.post("/api/books/{book_id}/translate/review/fix-paragraph")
    def translate_fix_paragraph(book_id: str, payload: Dict[str, Any]):
        """Direct German text edit from the review GUI — writes into all outputs."""
        book = get_book_or_404(book_id)
        translation_runner = translations.get(book_id)
        if translation_runner._task is not None and not translation_runner._task.done():
            raise HTTPException(409, "translation run is active — text edits are locked while it runs")
        try:
            batch_index = int((payload or {}).get("batch_index"))
            paragraph = int((payload or {}).get("paragraph"))
        except (TypeError, ValueError):
            raise HTTPException(400, "batch_index and paragraph required")
        try:
            payload_row = review_fix_paragraph(
                DATA_ROOT,
                book_id,
                _review_language(book),
                book.get("title") or "",
                batch_index,
                paragraph,
                str((payload or {}).get("text") or ""),
            )
        except ReviewError as exc:
            raise HTTPException(exc.status, exc.message)
        return {
            "open_flags": payload_row["open_flags"],
            "total_flags": payload_row["total_flags"],
            "edited_paragraph": payload_row.get("edited_paragraph"),
            "review": payload_row,
        }

    @app.post("/api/books/{book_id}/translate/review/retranslate")
    async def translate_review_retranslate(book_id: str, payload: Dict[str, Any]):
        """Re-run draft → refine → check for one corrupted batch (same chain)."""
        book = get_book_or_404(book_id)
        translation_runner = translations.get(book_id)
        if translation_runner._task is not None and not translation_runner._task.done():
            raise HTTPException(409, "translation run is active — retranslation is locked while it runs")
        language = _review_language(book)
        try:
            batch_index = int((payload or {}).get("batch_index"))
        except (TypeError, ValueError):
            raise HTTPException(400, "batch_index required")
        report_path, report_mode = review_current_report(DATA_ROOT, book_id, language)
        if report_path is None:
            raise HTTPException(404, f"no report for language {language}")
        if report_mode == "ensemble":
            raise HTTPException(400, "the latest report for this language is an ensemble report")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, f"report unreadable ({exc}) — restore the .bak first")
        if report.get("mode") != "authorial":
            raise HTTPException(400, "retranslation is only available for authorial runs")
        entries = report.get("batches") or []
        position = next(
            (i for i, e in enumerate(entries) if isinstance(e, dict) and e.get("index") == batch_index),
            None,
        )
        if position is None:
            raise HTTPException(404, f"batch index {batch_index} not in report")
        meta = {key: value for key, value in report.items() if key not in ("chapters", "batches")}
        settings = meta.get("settings") or {}

        def _profile_for(role: str) -> Optional[Dict[str, Any]]:
            info = meta.get(role) or {}
            profile = profiles.find_by_name(str(info.get("name") or ""))
            if profile is None and info.get("model"):
                profile = profiles.find_by_name(str(info.get("model")))
            return profile

        draft_profile = _profile_for("draft")
        refiner_profile = _profile_for("refiner")
        if draft_profile is None or refiner_profile is None:
            raise HTTPException(404, "draft/refiner profile not found anymore — check the LLM Profiles tab")
        checker_profile = _profile_for("checker")
        try:
            new_entry = await retranslate_batch(
                entries[position],
                language,
                book,
                draft_profile,
                refiner_profile,
                checker_profile,
                bool((payload or {}).get("unload_after_call", True)),
                previous_german_text(report, batch_index),
                max(1, int(settings.get("refine_rounds") or 2)),
                min(1.0, max(0.5, float(settings.get("similarity_stop") or 0.97))),
                min(2.0, max(0.1, float(settings.get("ratio_min") or 0.85))),
                min(5.0, max(0.5, float(settings.get("ratio_max") or 1.45))),
                structured=bool(meta.get("structured", True)),
                check_mode=str(meta.get("check_mode") or (meta.get("settings") or {}).get("check_mode") or "essential"),
            )
        except (LlmError, ValueError) as exc:
            raise HTTPException(502, f"retranslation failed: {exc}")
        entries[position] = new_entry
        report["batches"] = entries
        shutil.copyfile(report_path, report_path.with_suffix(".json.bak"))
        writer = AuthorialWriter(report_path.parent)
        restore_writer(writer, report)
        writer.persist(meta, book.get("title") or "")
        payload_row = collect_rows(writer.as_report(meta), editable=True)
        return {"open_flags": payload_row["open_flags"], "total_flags": payload_row["total_flags"], "review": payload_row}

    @app.post("/api/books/{book_id}/translate/reset-all")
    def translate_reset_all(book_id: str):
        """Clean slate: delete the whole translation directory — every report,
        output file, read-marker — for ALL languages of this book."""
        get_book_or_404(book_id)
        runner = translations.get(book_id)
        if runner._task is not None and not runner._task.done():
            raise HTTPException(409, "translation run is active — stop it first")
        base = DATA_ROOT / str(book_id) / "translations"
        removed: List[str] = []
        if base.exists():
            import shutil as _sh

            for child in sorted(base.iterdir()):
                if child.is_dir():
                    _sh.rmtree(child)
                    removed.append(child.name)
                else:
                    child.unlink()
                    removed.append(child.name)
        return {"removed": removed}

    @app.post("/api/books/{book_id}/translate/review/reset-chapter")
    def translate_reset_chapter(book_id: str, payload: Dict[str, Any]):
        """Reset ONE chapter's translation batches so Resume redoes only them
        (with the current output mode — e.g. the structured tool call)."""
        book = get_book_or_404(book_id)
        translation_runner = translations.get(book_id)
        if translation_runner._task is not None and not translation_runner._task.done():
            raise HTTPException(409, "translation run is active — stop it first")
        try:
            chapter_id = int((payload or {}).get("chapter_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "chapter_id required")
        language = _review_language(book)
        report_path, report_mode = review_current_report(DATA_ROOT, book_id, language)
        if report_path is None:
            raise HTTPException(404, f"no report for language {language}")
        if report_mode == "ensemble":
            raise HTTPException(400, "the latest report for this language is an ensemble report")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, f"report unreadable ({exc}) — restore the .bak first")
        if report.get("mode") != "authorial":
            raise HTTPException(400, "chapter reset is only available for authorial runs")
        meta = {key: value for key, value in report.items() if key not in ("chapters", "batches")}
        entries = report.get("batches") or []
        fresh: List[Dict[str, Any]] = []
        reset_batches = 0
        for entry in entries:
            if isinstance(entry, dict) and entry.get("chapter_id") == chapter_id:
                # placeholder WITH the real source_hash: resume keeps the slot
                # in place, stage "draft" makes the run re-process exactly this
                # batch with the current output mode
                fresh.append(
                    {
                        "index": entry.get("index"),
                        "chapter_id": chapter_id,
                        "chapter_name": entry.get("chapter_name") or "",
                        "source_hash": entry.get("source_hash"),
                        "stage": "draft",
                        "draft_error": None,
                        "draft_text": "",
                        "draft_notes": [],
                        "heading": None,
                        "german_segments": [],
                        "items": [],
                        "review": [],
                        "errors": [],
                    }
                )
                reset_batches += 1
            else:
                fresh.append(entry)
        if not reset_batches:
            raise HTTPException(404, f"chapter {chapter_id} has no batches in the report")
        report["batches"] = fresh
        shutil.copyfile(report_path, report_path.with_suffix(".json.bak"))
        writer = AuthorialWriter(report_path.parent)
        restore_writer(writer, report)
        writer.persist(meta, book.get("title") or "")
        payload_row = collect_rows(writer.as_report(meta), editable=True)
        return {
            "open_flags": payload_row["open_flags"],
            "total_flags": payload_row["total_flags"],
            "reset_batches": reset_batches,
            "review": payload_row,
        }

    # ------------------------------------------------------------------ #
    # Layout tab (Feature 3) — images/prompts/illustration positions
    # ------------------------------------------------------------------ #

    @app.get("/api/books/{book_id}/layout")
    def get_layout(book_id: str):
        get_book_or_404(book_id)
        chapters = store.written_chapters(book_id)
        layout = load_layout(DATA_ROOT, book_id)
        rows = book_word_positions(chapters)
        for entry in layout.get("illustrations") or []:
            entry["anchor_valid"] = anchor_valid_for(rows, entry)
        stats = {
            "total_words": sum(row["words"] for row in rows),
            "per_chapter": [
                {
                    "chapter_id": row["chapter_id"],
                    "chapter_name": row["chapter_name"],
                    "words": row["words"],
                    "paragraphs": len(row["paragraphs"]),
                }
                for row in rows
            ],
        }
        return {"layout": layout, "stats": stats}

    @app.put("/api/books/{book_id}/layout")
    def put_layout(book_id: str, payload: LayoutIn):
        get_book_or_404(book_id)
        layout = load_layout(DATA_ROOT, book_id)
        for key in ("style_directive", "cover_image_path"):
            value = getattr(payload, key)
            if value is not None:
                layout[key] = str(value)[:2000]
        if payload.words_per_page is not None:
            layout["words_per_page"] = max(50, min(2000, int(payload.words_per_page)))
        if payload.images_every_n_pages is not None:
            layout["images_every_n_pages"] = max(1, min(50, int(payload.images_every_n_pages)))
        if payload.characters is not None:
            layout["characters"] = [
                {"name": str(row.get("name") or "")[:120], "visual": str(row.get("visual") or "")[:4000]}
                for row in payload.characters
                if isinstance(row, dict) and row.get("name")
            ]
        if payload.illustrations is not None:
            layout["illustrations"] = [
                {
                    **row,
                    "id": str(row.get("id") or "")[:40],
                    "chapter_id": int(row.get("chapter_id")) if row.get("chapter_id") is not None else None,
                    "paragraph_index": int(row["paragraph_index"]) if isinstance(row.get("paragraph_index"), int) else None,
                }
                for row in payload.illustrations
                if isinstance(row, dict) and row.get("id")
            ]
        save_layout(DATA_ROOT, book_id, layout)
        return {"layout": layout}

    async def _layout_profile(book: Dict[str, Any], payload: Dict[str, Any]):
        profile = profile_or_404((payload or {}).get("profile_id") or book.get("planner_profile_id"))
        if profile is None:
            raise HTTPException(400, "profile_id required (planner profile default)")
        return profile

    async def _extract_one_visual(
        book: Dict[str, Any],
        chapters: List[Dict[str, Any]],
        profile: Optional[Dict[str, Any]],
        codex_entry: Dict[str, Any],
        unload: bool,
    ) -> Dict[str, Any]:
        """Extraction for ONE codex character: sequential + Ollama-unload.

        Returns {"name", "status": ok|no_text|error, "visual"?, "error"?}.
        A profile is only needed when the character actually occurs in the text.
        """
        name = (codex_entry.get("name") or "").strip()
        aliases = [name] + [a for a in codex_entry.get("aliases") or [] if a.strip()]
        patterns = [p for p in (alias_pattern(a) for a in aliases) if p is not None]
        chunks: List[str] = []
        for chapter in chapters:
            chunks.extend(passage_chunks_for(chapter, patterns))
        if not chunks:
            return {"name": name, "status": "no_text", "detail": "kein Namensvorkommen im Manuskript"}
        if profile is None:
            return {"name": name, "status": "error", "error": "profile_id required (planner profile default)"}
        try:
            response = await call_llm(
                profile,
                visual_extract_messages(
                    [{"name": name, "aliases": codex_entry.get("aliases") or []}], chunks[:6]
                ),
                temperature=0.3,
                unload=unload,
            )
            visuals = parse_visuals(response.content or "", [{"name": name, "aliases": []}])
            visual = next((row["visual"] for row in visuals if row.get("visual")), "")
        except Exception as exc:
            return {"name": name, "status": "error", "error": str(exc)[:300]}
        if not visual:
            return {"name": name, "status": "error", "error": "leeres Ergebnis der Extraktion"}
        return {"name": name, "status": "ok", "visual": visual[:4000]}

    def _merge_layout_character(layout: Dict[str, Any], name: str, visual: str) -> None:
        rows = {(row.get("name") or "").strip(): row for row in layout.get("characters") or []}
        existing = rows.get(name) or {}
        rows[name] = {"name": name, "visual": (visual or existing.get("visual") or "")[:4000]}
        layout["characters"] = sorted(rows.values(), key=lambda r: r["name"].casefold())

    @app.post("/api/books/{book_id}/layout/extract-characters")
    async def layout_extract_characters(book_id: str, payload: Dict[str, Any]):
        """Bulk extraction (kept for API parity — the GUI drives per-character calls)."""
        book = get_book_or_404(book_id)
        chapters = store.written_chapters(book_id)
        if not chapters:
            raise HTTPException(400, "no written chapters to extract visuals from")
        profile = await _layout_profile(book, payload)
        unload = bool((payload or {}).get("unload_after_call", True))
        codex = (book.get("codex") or {}).get("characters") or []
        layout = load_layout(DATA_ROOT, book_id)
        results: List[Dict[str, Any]] = []
        for entry in codex:
            result = await _extract_one_visual(book, chapters, profile, entry, unload)
            if result["status"] == "ok":
                _merge_layout_character(layout, result["name"], result["visual"])
            results.append(result)
        save_layout(DATA_ROOT, book_id, layout)
        return {"characters": layout["characters"], "results": results}

    @app.post("/api/books/{book_id}/layout/extract-characters/{character_name}")
    async def layout_extract_character(book_id: str, character_name: str, payload: Dict[str, Any]):
        """One extraction call for one figure — the GUI shows live progress."""
        book = get_book_or_404(book_id)
        codex_entry = next(
            (
                entry
                for entry in (book.get("codex") or {}).get("characters") or []
                if (entry.get("name") or "").strip().casefold() == character_name.strip().casefold()
            ),
            None,
        )
        if codex_entry is None:
            raise HTTPException(404, f"character {character_name} not in codex")
        chapters = store.written_chapters(book_id)
        if not chapters:
            raise HTTPException(400, "no written chapters to extract visuals from")
        profile = profile_or_404((payload or {}).get("profile_id") or book.get("planner_profile_id"))
        unload = bool((payload or {}).get("unload_after_call", True))
        result = await _extract_one_visual(book, chapters, profile, codex_entry, unload)
        if result["status"] == "ok":
            layout = load_layout(DATA_ROOT, book_id)
            _merge_layout_character(layout, result["name"], result["visual"])
            save_layout(DATA_ROOT, book_id, layout)
        return {"result": result, "character": {"name": result["name"], "visual": result.get("visual", "")}}

    @app.post("/api/books/{book_id}/layout/compute-positions")
    def layout_compute_positions(book_id: str):
        get_book_or_404(book_id)
        chapters = store.written_chapters(book_id)
        layout = load_layout(DATA_ROOT, book_id)
        updated = compute_positions(chapters, layout)
        save_layout(DATA_ROOT, book_id, updated)
        return {"layout": updated}

    @app.post("/api/books/{book_id}/layout/illustrations/{pos_id}/describe")
    async def layout_describe(book_id: str, pos_id: str, payload: Dict[str, Any]):
        book = get_book_or_404(book_id)
        chapters = store.written_chapters(book_id)
        if not chapters:
            raise HTTPException(400, "no written chapters yet")
        layout = load_layout(DATA_ROOT, book_id)
        entry = next((e for e in layout.get("illustrations") or [] if e.get("id") == pos_id), None)
        if entry is None:
            raise HTTPException(404, f"illustration {pos_id} not found — recompute positions")
        rows = book_word_positions(chapters)
        if not anchor_valid_for(rows, entry):
            raise HTTPException(400, "anchor unclear — recompute positions first")
        profile = await _layout_profile(book, payload)
        unload = bool((payload or {}).get("unload_after_call", True))
        context = context_window(rows, entry["chapter_id"], entry["paragraph_index"] or 0)
        if not context:
            raise HTTPException(400, "no text around this anchor")
        try:
            response = await call_llm(
                profile, describe_messages(context, layout, "Deutsch"), temperature=0.4, unload=unload
            )
            fields = parse_describe(response.content or "")
        except (LlmError, ValueError) as exc:
            raise HTTPException(502, f"scene description failed: {exc}")
        entry.update(fields)
        visuals = character_visuals_in(context, book, layout)
        entry["prompt"] = compose_prompt(entry, layout.get("style_directive") or "", visuals)
        entry["people_in_scene"] = [row["name"] for row in visuals]
        save_layout(DATA_ROOT, book_id, layout)
        return {"illustration": entry}

    @app.post("/api/books/{book_id}/layout/illustrations/{pos_id}/image")
    def layout_upload_image(book_id: str, pos_id: str, file: UploadFile):
        get_book_or_404(book_id)
        layout = load_layout(DATA_ROOT, book_id)
        entry = next((e for e in layout.get("illustrations") or [] if e.get("id") == pos_id), None)
        if entry is None:
            raise HTTPException(404, f"illustration {pos_id} not found")
        suffix = "." + (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
        if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
            raise HTTPException(400, "only jpg/png/webp")
        data = file.file.read()
        if len(data) > 4 * 1024 * 1024:
            raise HTTPException(400, "image too large (limit 4 MB)")
        target = _book_image_target(book_id, pos_id, suffix)
        target.write_bytes(data)
        entry["image_path"] = f"images/{pos_id}{suffix}"
        save_layout(DATA_ROOT, book_id, layout)
        return {"illustration": entry}

    @app.delete("/api/books/{book_id}/layout/illustrations/{pos_id}/image")
    def layout_delete_image(book_id: str, pos_id: str):
        get_book_or_404(book_id)
        layout = load_layout(DATA_ROOT, book_id)
        entry = next((e for e in layout.get("illustrations") or [] if e.get("id") == pos_id), None)
        if entry is None:
            raise HTTPException(404, f"illustration {pos_id} not found")
        path = str(entry.get("image_path") or "")
        if path:
            try:
                (DATA_ROOT / str(book_id) / path.strip().lstrip("/\\")).resolve().unlink(missing_ok=True)
            except Exception:
                pass
        entry["image_path"] = ""
        save_layout(DATA_ROOT, book_id, layout)
        return {"illustration": entry}

    @app.post("/api/books/{book_id}/layout/cover")
    def layout_upload_cover(book_id: str, file: UploadFile):
        get_book_or_404(book_id)
        suffix = "." + (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
        if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
            raise HTTPException(400, "only jpg/png/webp")
        data = file.file.read()
        if len(data) > 4 * 1024 * 1024:
            raise HTTPException(400, "image too large (limit 4 MB)")
        target = _book_image_target(book_id, "cover", suffix)
        target.write_bytes(data)
        layout = load_layout(DATA_ROOT, book_id)
        layout["cover_image_path"] = f"images/cover{suffix}"
        save_layout(DATA_ROOT, book_id, layout)
        return {"cover_image_path": layout["cover_image_path"]}

    @app.delete("/api/books/{book_id}/layout/cover")
    def layout_delete_cover(book_id: str):
        get_book_or_404(book_id)
        layout = load_layout(DATA_ROOT, book_id)
        path = str(layout.get("cover_image_path") or "")
        if path:
            try:
                (DATA_ROOT / str(book_id) / path.strip().lstrip("/\\")).resolve().unlink(missing_ok=True)
            except Exception:
                pass
        layout["cover_image_path"] = ""
        save_layout(DATA_ROOT, book_id, layout)
        return {"cover_image_path": ""}

    @app.get("/api/books/{book_id}/layout/image")
    def layout_get_image(book_id: str, path: str):
        get_book_or_404(book_id)
        data = _book_image_loader(book_id)(path)
        if data is None:
            raise HTTPException(404, "image not found")
        from ..export.epub import media_type_of

        return Response(content=data, media_type=media_type_of(path))

    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="static")

    return app


app = create_app()