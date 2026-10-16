"""Ensemble translation: several candidate models per block, a judge model decides.

Runs fully through the shared LLM profiles of the dashboard. For every block of
the manuscript every candidate translates the block (strictly sequentially, so
local models never share VRAM), then a separate judge model picks the winner.
Local Ollama models are unloaded right after every call (keep_alive=0) so no
model clogs VRAM between calls.

Incremental checkpointing: after every batch the report + partial bilingual
file are rewritten, so an interrupted run can resume finished batches for free.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from ..llm.provider import LlmClient, LlmError, LlmProfile, extract_json

MAX_GLOSSARY_ENTRIES = 60


class TranslationStopped(Exception):
    """Raised when the user stops an ensemble run — partial output is kept."""


def build_batches(chapters: List[Dict[str, Any]], batch_size: int) -> List[Dict[str, Any]]:
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
        chunks: List[List[str]] = []
        for start in range(0, len(paragraphs), max(1, batch_size)):
            chunks.append(paragraphs[start : start + max(1, batch_size)])
        for chunk in chunks:
            batches.append(
                {
                    "chapter_id": int(chapter["id"]),
                    "chapter_name": chapter.get("name") or "Untitled",
                    "header": header if chunks[0] is chunk else None,
                    "paragraphs": chunk,
                    "source_hash": hashlib.sha256("\n".join(chunk).encode("utf-8")).hexdigest()[:16],
                }
            )
    return batches


def codex_glossary(book: Dict[str, Any]) -> str:
    codex = book.get("codex") or {}
    entries: List[str] = []
    for kind in ("characters", "settings", "lore"):
        for entry in codex.get(kind) or []:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            aliases = [a for a in (entry.get("aliases") or []) if a.strip()]
            entries.append(f"{name} (also: {', '.join(aliases)})" if aliases else name)
    if not entries:
        return ""
    return (
        "Glossary — treat these entity names/aliases consistently across blocks:\n- "
        + "\n- ".join(entries[:MAX_GLOSSARY_ENTRIES])
    )


def style_context(book: Dict[str, Any]) -> str:
    parts = []
    notes = (book.get("genre_notes") or "").strip()
    if notes:
        parts.append(f"Authorial style to preserve: {notes}")
    prompt = (book.get("story_prompt") or "").strip()
    if prompt:
        parts.append(f"Brief premise for orientation: {prompt[:600]}")
    return "\n".join(parts)


def candidate_messages(source_block: str, language: str, book: Dict[str, Any]) -> List[Dict[str, Any]]:
    system_parts = [
        "You are a professional literary translator.",
        f"Translate the given source block into {language}.",
        "One output paragraph per input paragraph, same order; if the block contains a chapter heading, translate it as a heading.",
        "Output ONLY the translation — no numbers, no language names, no notes, no preamble.",
        "Preserve digits, punctuation marks and quotations as in the original.",
    ]
    glossary = codex_glossary(book)
    if glossary:
        system_parts.append(glossary)
    style = style_context(book)
    if style:
        system_parts.append(style)
    return [
        {"role": "system", "content": "\n".join(system_parts)},
        {"role": "user", "content": source_block},
    ]


def judge_messages(
    source_block: str,
    candidates: List[Dict[str, Any]],
    language: str,
    book: Dict[str, Any],
) -> List[Dict[str, Any]]:
    system_parts = [
        "You are a demanding literary editor.",
        f"Several candidate translations of one source block were produced by different models. The target language is {language}.",
        "Pick the candidate with the best combination of:",
        "1) faithful meaning — no omissions, no additions, nothing skipped",
        "2) idiomatic, natural, literary target language — reject stiff or machine-like phrasing",
        "3) narrative tone and register consistent with the style notes",
        "4) structure preserved (similar paragraph count and order)",
        'Respond ONLY with JSON exactly like {"winner": <candidate number>, "reason": "<one short sentence>"}.',
    ]
    style = style_context(book)
    if style:
        system_parts.append(style)
    glossary = codex_glossary(book)
    if glossary:
        system_parts.append(glossary)
    numbered = "\n\n".join(
        f"--- CANDIDATE {i + 1} ({c['name']}) ---\n{c['text']}" for i, c in enumerate(candidates)
    )
    return [
        {"role": "system", "content": "\n".join(system_parts)},
        {"role": "user", "content": f"--- SOURCE BLOCK ---\n{source_block}\n\n{numbered}"},
    ]


def _parse_judge_decision(raw: str, candidate_count: int) -> Dict[str, Any]:
    try:
        payload = extract_json(raw)
        winner = int(payload.get("winner"))
        if 1 <= winner <= candidate_count:
            return {"winner": winner, "reason": str(payload.get("reason") or "").strip()}
    except Exception:
        pass
    match = re.search(r'"winner"\s*:\s*"?(\d+)', raw or "")
    if match:
        winner = int(match.group(1))
        if 1 <= winner <= candidate_count:
            return {"winner": winner, "reason": ""}
    return {"winner": 1, "reason": ""}  # judge failed → default to first valid candidate


async def unload_ollama_profile(profile: Dict[str, Any]) -> None:
    """Equivalent of `ollama stop <model>`: free the model from VRAM immediately."""
    base = (profile.get("base_url") or "").strip().rstrip("/")
    lowered = base.lower()
    if "ollama" not in lowered and ":11434" not in lowered:
        return
    root = base[:-3] if base.endswith("/v1") else base
    url = root + "/api/generate"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(url, json={"model": profile.get("model"), "keep_alive": 0})
    except Exception:
        pass


class AssemblyWriter:
    """Collects finished batch results and (re)writes output + report files."""

    def __init__(self, outdir: Path):
        self.outdir = outdir
        self.bilingual_path = outdir / "book_bilingual.txt"
        self.report_path = outdir / "ensemble_report.json"
        self.finished: List[Dict[str, Any]] = []

    def add(self, batch: Dict[str, Any], result: Dict[str, Any]) -> None:
        self.finished.append({**result, "chapter_id": batch["chapter_id"]})

    def bilingual_text(self) -> str:
        blocks: List[str] = []
        for entry in self.finished:
            source = (entry.get("source_block") or "").rstrip()
            translation = (entry.get("winner_text") or "").strip()
            if source:
                blocks.append(source)
            if translation:
                blocks.append(translation)
            blocks.append("")
        return "\n".join(blocks).rstrip() + "\n"

    def source_for(self, batch: Dict[str, Any]) -> str:
        lines: List[str] = []
        if batch.get("header"):
            lines.append(batch["header"])
        lines.extend(batch["paragraphs"])
        return "\n".join(lines)

    def persist(self, meta: Dict[str, Any]) -> None:
        self.bilingual_path.write_text(self.bilingual_text(), encoding="utf-8")
        report = dict(meta)
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


async def run_ensemble_translation(
    *,
    chapters: List[Dict[str, Any]],
    language: str,
    candidate_profiles: List[Dict[str, Any]],
    judge_profile: Optional[Dict[str, Any]],
    unload_after_call: bool,
    batch_size: int,
    outdir: Path,
    book: Dict[str, Any],
    resume: bool,
    emit: Callable[[Dict[str, Any]], Awaitable[None]],
    should_stop: Callable[[], bool],
) -> Dict[str, Any]:
    started = time.time()
    outdir.mkdir(parents=True, exist_ok=True)
    writer = AssemblyWriter(outdir)
    batches = build_batches(chapters, batch_size)
    candidate_names = [f"{p.get('name') or p.get('model')}" for p in candidate_profiles]

    meta: Dict[str, Any] = {
        "mode": "ensemble",
        "language": language,
        "batch_size": batch_size,
        "batches_total": len(batches),
        "candidates": [
            {"name": name, "model": p.get("model")} for name, p in zip(candidate_names, candidate_profiles)
        ],
        "judged_by": (judge_profile or {}).get("name"),
        "totals": {"wins": {}, "tokens": {}},
    }

    # resume: reload finished batches whose source hash still matches
    if resume:
        saved = AssemblyWriter.load_report(writer.report_path)
        reuse = 0
        if saved and int(saved.get("batches_total") or 0) == len(batches):
            for entry in saved.get("batches") or []:
                index = int(entry.get("index", -1))
                if 0 <= index < len(batches) and entry.get("source_hash") == batches[index]["source_hash"]:
                    entry["source_block"] = writer.source_for(batches[index])
                    writer.finished.append(entry)
                    reuse += 1
            writer.persist(meta)
        if reuse:
            await emit({"type": "translation_batch", "batch_index": reuse, "batches_total": len(batches), "note": f"resumed — {reuse} finished batches reused"})
        elif saved is None:
            resume = False

    judge_client: Optional[LlmClient] = None
    if judge_profile and len(candidate_profiles) > 1:
        judge_client = LlmClient(LlmProfile(**judge_profile))

    totals = meta["totals"]

    def _accumulate(name: str, usage: Any, wins: int = 0) -> None:
        bucket = totals["tokens"].setdefault(name, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0})
        bucket["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        bucket["cost"] += float(usage.get("cost") or 0.0)
        if wins:
            totals["wins"][name] = totals["wins"].get(name, 0) + wins

    for index, batch in enumerate(batches):
        if index < len(writer.finished):
            continue
        if should_stop():
            writer.persist({**meta, "interrupted": True})
            raise TranslationStopped()
        source_block = writer.source_for(batch)
        results: List[Dict[str, Any]] = []
        errors: List[str] = []
        for cand_name, cand in zip(candidate_names, candidate_profiles):
            if should_stop():
                writer.persist({**meta, "interrupted": True})
                raise TranslationStopped()
            client = LlmClient(LlmProfile(**cand))
            try:
                response = await client.chat(
                    candidate_messages(source_block, language, book),
                    temperature=float(cand.get("temperature", 0.7) or 0.7),
                )
                text = (response.content or "").strip()
                if not text:
                    raise LlmError("empty translation")
                usage = response.usage.to_dict() if response.usage else {}
                results.append({"name": cand_name, "text": text})
                _accumulate(cand_name, usage)
                await emit(
                    {
                        "type": "translation_output",
                        "lines": [f"[{cand_name}] {text.splitlines()[0][:140]}"],
                    }
                )
            except Exception as exc:
                errors.append(f"{cand_name}: {exc}")
            finally:
                await client._http.aclose()
                if unload_after_call:
                    await unload_ollama_profile(cand)
        if not results:
            await emit(
                {"type": "translation_batch", "batch_index": index, "batches_total": len(batches), "error": "; ".join(errors)}
            )
            writer.finished.append(
                {
                    "index": index,
                    "chapter_id": batch["chapter_id"],
                    "source_hash": batch["source_hash"],
                    "source_block": source_block,
                    "winner_index": 0,
                    "winner": None,
                    "reason": "; ".join(errors),
                    "winner_text": "",
                }
            )
            continue

        if judge_client is not None and len(results) >= 2:
            if should_stop():
                writer.persist({**meta, "interrupted": True})
                raise TranslationStopped()
            try:
                response = await judge_client.chat(
                    judge_messages(source_block, results, language, book),
                    temperature=0.2,
                    max_output_tokens=1200,
                )
                decision = _parse_judge_decision(response.content or "", len(results))
                usage = response.usage.to_dict() if response.usage else {}
            except Exception as exc:
                decision = _parse_judge_decision("", len(results))
                usage = {}
                errors.append(f"judge: {exc}")
                decision["reason"] = f"judge failed ({exc}); defaulted"
            finally:
                await judge_client._http.aclose()
                if unload_after_call and judge_profile:
                    await unload_ollama_profile(judge_profile)
            _accumulate((judge_profile or {}).get("name") or "judge", usage)
        else:
            decision = {"winner": 1, "reason": "single candidate — no judging"}

        winner = results[decision["winner"] - 1]
        _accumulate(winner["name"], {}, wins=1)
        entry = {
            "index": index,
            "chapter_id": batch["chapter_id"],
            "chapter_name": batch["chapter_name"],
            "source_hash": batch["source_hash"],
            "source_block": source_block,
            "winner_index": decision["winner"],
            "winner": winner["name"],
            "reason": decision["reason"],
            "winner_text": winner["text"],
            "candidates": [r["name"] for r in results],
            "errors": errors if errors else None,
        }
        writer.finished.append(entry)
        writer.persist(meta)
        await emit(
            {
                "type": "translation_batch",
                "batch_index": index + 1,
                "batches_total": len(batches),
                "chapter_id": batch["chapter_id"],
                "winner": winner["name"],
                "reason": decision["reason"],
            }
        )

    writer.persist({**meta, "interrupted": False, "finished_at": time.time()})
    gaps = [e for e in writer.finished if not e.get("winner_text")]
    return {
        "output_file": str(writer.bilingual_path),
        "report_file": str(writer.report_path),
        "batches_total": len(batches),
        "batches_done": len(writer.finished),
        "gaps": len(gaps),
        "totals": totals,
    }