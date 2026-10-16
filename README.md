# IsaacWriter — NstAgent Book Writing Studio

Write full-length books (10K–100K+ words) with LLMs, using the method from the paper
*"Scaling Long-Form Story Generation via Narrative State Tracking"* (NstAgent, arXiv 2609.35759)
as the core engine, wrapped in a local GUI for planning, monitoring, intervention and statistics.

## What it does

- **Frozen 4-stage planning** (paper-faithful): premise → detailed synopsis → acts → chapter-level
  JSON outline with per-chapter word targets (10K→10 chapters, 20K→15, 50K→25, 100K→40, proportional
  word normalization). Every stage is editable in the GUI before you freeze the outline.
- **Chapter-by-chapter writing agent** (paper-faithful `narrative_ops` protocol): per chapter the
  model may `read` (max 3), `search` (max 5, SQLite FTS5 + BM25 sentence windows), `write` the full
  chapter (±20 % length gate, rejected drafts compacted out of history), `correct` (max 3) and —
  exactly once — `update` the typed narrative state `sₜ = (characters, past events, future
  requirements)` with a four-array atomic JSON call. Ends with `DONE`, max 50 turns/chapter,
  checkpoint after every chapter.
- **Freely selectable LLMs**: any OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp server,
  OpenRouter, OpenAI, DeepSeek, …) as editable profiles with temperature, output-token limit and
  price per 1M tokens (drives the cost statistics). Separate planner and writer model per book.
- **Intervention**: run fully automatically or pause per chapter (review gate: approve / edit the
  prose / demand a rewrite with instructions / attach author notes injected into the next chapter
  prompt). Pause, stop and resume anytime — everything is checkpointed.
- **Codex** (story bible): characters with aliases/roles/colors, settings, world lore. The codex is
  injected into the planner prompts and powers the mention statistics; the live agent-side narrative
  state is shown next to it.
- **Statistics**: character presence timeline (who appears when, who leaves — from alias mention
  scanning of every chapter, with entry/exit markers), settings timeline, plot-thread gantt (open
  obligations from the state updates), words-per-chapter vs. target with cumulative curve,
  token/cost development, tool-usage totals, and a SVG concept graph (premise → acts → chapters with
  involved characters).
- **Export**: manuscript as Markdown or TXT (chapter titles + prose), statistics as CSV.
- **Translate** (one dashboard, shared LLM profiles): turn the written manuscript into a bilingual
  side-by-side edition (EN↔DE and any target language) with the external tool
  [bilingual_book_maker](https://github.com/yihong0618/bilingual_book_maker) (**MIT** by yihong0618).
  It runs as an independent process in its own venv — a running chapter-writing job is never
  interrupted, and the same LLM profiles defined in *LLM Profiles* are used. Outputs (bilingual
  TXT/MD, test previews, partial outputs) live under `data/<book-id>/translations/` and can be
  viewed/downloaded in the Translate tab. License notice: see `THIRD_PARTY_NOTICES.md`.

## Run (Windows)

```
pip install -r backend/requirements.txt
cd frontend && npm install && npm run build && cd ..
python -m uvicorn backend.api.server:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000.

Development mode (hot reload): run the backend as above plus `npm run dev` in `frontend/`
(Vite serves http://localhost:5173 and proxies `/api` to port 8000).

## First steps in the UI

1. **LLM Profiles** tab: create a profile. For a local Ollama: base URL `http://localhost:11434/v1`,
   any placeholder API key, model name (use *Test* to list installed models). For paid APIs: real
   key + price per 1M tokens for cost stats.
2. **New Book**: title, story prompt, language (EN/DE), target words, optional style notes.
3. **Codex** tab: register your characters (names + aliases!), settings and lore before planning —
   the planner will use them and the statistics rely on the aliases.
4. **Outline** tab: pick the planner profile, generate, review/edit every stage, freeze.
5. **Write** tab: pick the writer profile, choose *full auto* or *review each chapter*, start.
6. **Statistics** tab: watch presence timelines, plot threads, progress and cost.

## How the paper protocol maps to the app

| Paper (NstAgent) | IsaacWriter |
|---|---|
| Planner stages 1–4 with frozen outline | Outline tab, `backend/nstagent/planner.py` |
| State `sₜ=(Cₜ,Pₜ,Fₜ)` with 4-array atomic update | `backend/nstagent/state.py`, `update` tool in `agent.py` |
| Tools read/search/write/correct with budgets | `backend/nstagent/agent.py` (read≤3, search≤5, correct≤3) |
| ±20 % length gate, rejected-draft compaction | `write` tool handler |
| SQLite FTS5 BM25 sentence-window search | `backend/nstagent/search_index.py` |
| Checkpoint per chapter, auto-resume | `backend/store/project.py` + runner |
| Extensions | Codex injection, author notes, review gate, mention statistics, GUI |

## Tests

```
python -m pytest backend/tests/test_core.py backend/tests/test_runner.py -q
```

An OpenAI-compatible **mock LLM server** (`backend/tests/mock_llm_server.py`) and a full
end-to-end driver (`backend/tests/e2e_run.py`) are included: they validate outline generation,
writing, review gates, statistics and export without any real model.

## Translation setup (optional, one command each)

The Translate tab needs the external tool locally (kept out of this repo, not vendored):

```
git clone --depth 1 https://github.com/yihong0618/bilingual_book_maker.git tools/bilingual_book_maker
python -m venv tools/bbm-venv
tools\bbm-venv\Scripts\pip install -e tools/bilingual_book_maker
```

Uses the same LLM profiles as book writing (OpenAI-compatible endpoints). Test mode translates only
the first N lines; interrupted runs can be resumed. `bilingual_book_maker` is MIT-licensed by
yihong0618 — full notice in `THIRD_PARTY_NOTICES.md`.

## Notes

- Cost figures are estimates from the configured per-1M-token prices, summed from API usage fields.
- The narrative-state entries can also be hand-edited in the GUI (pause the run first).
- Everything is stored under `data/<book-id>/` as plain JSON files (chapters, state snapshots,
  metrics) — easy to back up, easy to diff.