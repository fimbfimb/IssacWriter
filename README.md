# IssacWriter

An NstAgent-based book writing studio: a local application that writes **full-length books (10K–100K+ words)** with LLMs — planned, written, revised, and consistency-tracked chapter by chapter.

The core writing engine is a faithful implementation of the method from the paper
[*"Scaling Long-Form Story Generation via Narrative State Tracking"*](https://arxiv.org/abs/2609.35759) (NstAgent).

## What it does

1. **Planner** — turns a premise into a *frozen outline*: premise → synopsis → acts → chapters,
   each chapter with a proportional word target scaled to the requested total length.
2. **Chapter loop** — a novelist agent writes each chapter in a tool-use loop:
   it can `read` finished chapters, `search` the manuscript (BM25 via SQLite FTS5),
   `write` the chapter (gated to ±20 % of its word target), `correct` exact passages, and
   `update` a structured **narrative state**.
3. **Narrative state tracking** — characters, past events, and future requirements are kept
   in a `{character_states, past_events, future_requirements}` state with a single atomic
   4-array update per chapter (`upsert_character_state`, `add_past_event`,
   `add_future_requirement`, `resolve_future_requirement`). This is what keeps long books
   coherent: open plot threads, character departures, and setup/payoff stay explicit.
4. **Checkpoints** — after every chapter the book is checkpointed; runs can pause, resume,
   or stop at any point.

## Features

- **Any OpenAI-compatible LLM** (Ollama, LM Studio, llama.cpp, OpenRouter, OpenAI, DeepSeek, …)
  via named connection profiles; separate planner/writer model per book, with token & cost accounting.
- **Multilingual books** — prompt templates in English & German.
- **Review gate** — per-chapter approval mode: approve prose, edit chapters or narrative state,
  add author notes injected into the next chapter, or rewrite.
- **Codex** — user-authored characters, settings, and lore (with aliases) that get injected into
  the planning prompts and can be synced with the agent's live state.
- **Statistics** — character/setting presence timelines (mention scan + state snapshots),
  words vs. target per chapter, per-chapter tool calls, tokens/cost, and dangling plot threads
  (future requirements that were never resolved).
- **Export** — full manuscript as Markdown/TXT, state JSON, stats CSV.
- **REST API with SSE** — every run step (tool calls, writes, gates) is streamed live.

## Project layout

```
backend/
├── main.py                 # uvicorn entry point
├── api/server.py           # FastAPI routes + SSE event stream
├── llm/                    # OpenAI-compatible client, profiles, usage & cost accounting
├── nstagent/
│   ├── prompts.py          # system/chapter/planner prompts (EN/DE, from paper App. A.2)
│   ├── state.py            # narrative state (C/P/F), 4-array atomic update, snapshots
│   ├── tools.py            # read/search/write/correct/update schemas + handlers
│   ├── search_index.py     # SQLite FTS5 BM25 sentence-window index
│   ├── planner.py          # premise → synopsis → acts → chapters, word normalization
│   ├── agent.py            # chapter tool-use loop, ±20 % length gate, budgets
│   └── runner.py           # background run controller: auto vs. review gate, pause/resume/stop
├── store/project.py        # book persistence (JSON files + checkpoints)
├── stats/mentions.py       # mention scan → presence timelines
└── tests/                  # pytest: state ops, length gate, tokenization, mock-LLM run
```

## Getting started

Requirements: **Python 3.10+**

```bash
pip install -r backend/requirements.txt
python -m backend.main --port 8000
```

The API is then served at `http://127.0.0.1:8000` (health check: `GET /api/health`).

1. Create an LLM profile (`POST /api/profiles`) with your endpoint's base URL, key, and model —
   any OpenAI-compatible server works (e.g. Ollama at `http://localhost:11434/v1`).
2. Create a book (`POST /api/books`), generate the outline, review it, and freeze it.
3. Start the run (`POST /api/books/{id}/run/start`) — in `auto` mode it writes straight through
   the outline; in `review` mode it pauses after each chapter for your approval.
4. Follow the live event stream (`GET /api/books/{id}/events`) and check stats
   (`GET /api/books/{id}/stats`) while it writes.

### Run tests

```bash
python -m pytest backend/tests -q
```

Tests use a built-in mock LLM server, so they run fully offline.

## Status

- ✅ **Backend** — NstAgent core (planner, chapter loop, narrative state, FTS index),
  run controller (auto/review, pause/resume/stop), REST API + SSE, persistence, stats, export.
- 🔜 **Web GUI** — book library, outline editor, live write view, codex, statistics dashboard
  (planned: Vite + React + TypeScript frontend served by the backend).

## License

[MIT](LICENSE)