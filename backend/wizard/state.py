"""Wizard state: schema + atomic persistence for ``wizard_state.json``.

The wizard is strictly additive: its state lives in
``data/<book-id>/wizard/wizard_state.json`` and touches nothing else. Every
state change is written back immediately (Step-Wechsel, Pause, Stop,
throttled progress, Fehler) so a crash loses at most the active chapter —
the step engines keep their own checkpoints.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..store.project import _write_json

STATUS_CONFIG = "config"
STATUS_RUNNING = "running"
STATUS_PAUSING = "pausing"
STATUS_PAUSED = "paused"
STATUS_STOPPING = "stopping"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUSES = (
    STATUS_CONFIG,
    STATUS_RUNNING,
    STATUS_PAUSING,
    STATUS_PAUSED,
    STATUS_STOPPING,
    STATUS_STOPPED,
    STATUS_DONE,
    STATUS_ERROR,
)

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_PAUSED = "paused"
STEP_DONE = "done"
STEP_ERROR = "error"
STEP_SKIPPED = "skipped"

STEPLIKE_KINDS = ("outline", "outline_refine", "write", "text_refine", "translate", "translate_refine")

HISTORY_MAX = 250


def wizard_dir(book_dir: Path | str) -> Path:
    return Path(book_dir) / "wizard"


def state_path(book_dir: Path | str) -> Path:
    return wizard_dir(book_dir) / "wizard_state.json"


def load_state(book_dir: Path | str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(state_path(book_dir).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_state(book_dir: Path | str, state: Dict[str, Any]) -> None:
    state["updated"] = _now_iso()
    _write_json(state_path(book_dir), state)


def default_steps() -> List[Dict[str, Any]]:
    """Order + defaults for the 6 wizard steps (see WIZARD_PLAN §2 B)."""
    return [
        {"step": 1, "kind": "outline", "enabled": True, "planner_profile_id": None},
        {
            "step": 2, "kind": "outline_refine", "enabled": True,
            "candidate_profile_ids": [], "judge_profile_id": None, "parallel": False,
            "refine_rounds": 1, "similarity_stop": 0.95, "context_prev": 3,
        },
        {"step": 3, "kind": "write", "enabled": True, "writer_profile_id": None},
        {
            "step": 4, "kind": "text_refine", "enabled": True,
            "candidate_profile_ids": [], "judge_profile_id": None, "parallel": False,
            "refine_rounds": 2, "similarity_stop": 0.97, "draft_temperature": None,
            "style_probe": "",
        },
        {
            "step": 5, "kind": "translate", "enabled": True,
            "draft_profile_id": None, "refiner_profile_id": None, "checker_profile_id": None,
            "target_language": "German", "batch_words": 250, "style_probe": "",
        },
        {
            "step": 6, "kind": "translate_refine", "enabled": True,
            "candidate_profile_ids": [], "judge_profile_id": None, "parallel": False,
            "refine_rounds": 1,
        },
    ]


def new_state(book_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    steps = list(config.get("steps") or [])
    now = _now_iso()
    return {
        "id": book_id,
        "status": STATUS_RUNNING,
        "config": {"book": dict(config.get("book") or {}), "steps": steps,
                   "pause_between_steps": bool(config.get("pause_between_steps"))},
        "current_step": None,
        "steps_state": [
            {"step": cfg["step"], "kind": cfg["kind"], "status": STEP_PENDING,
             "started_at": None, "finished_at": None, "error": None,
             "progress": {"done": 0, "total": None}}
            for cfg in steps
        ],
        "history": [{"at": now, "event": "wizard created"}],
        "created": now,
        "updated": now,
    }


def history_add(state: Dict[str, Any], event: str) -> None:
    state.setdefault("history", []).append({"at": _now_iso(), "event": event[:400]})
    if len(state["history"]) > HISTORY_MAX:
        state["history"] = state["history"][-HISTORY_MAX:]


def step_state(state: Dict[str, Any], step_no: int) -> Optional[Dict[str, Any]]:
    for row in state.get("steps_state") or []:
        if int(row.get("step") or 0) == int(step_no):
            return row
    return None


def state_done(state: Dict[str, Any]) -> bool:
    rows = state.get("steps_state") or []
    return bool(rows) and all(
        row.get("status") in (STEP_DONE, STEP_SKIPPED) for row in rows
    )


def config_step(state: Dict[str, Any], step_no: int) -> Optional[Dict[str, Any]]:
    for row in (state.get("config") or {}).get("steps") or []:
        if int(row.get("step") or 0) == int(step_no):
            return row
    return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())