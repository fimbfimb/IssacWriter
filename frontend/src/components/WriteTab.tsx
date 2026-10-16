import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Book, ChapterRecord, Profile } from "../types";
import LiveStreamBox from "./LiveStreamBox";

function formatEvent(event: any): { time: string; html: string } {
  const time = event.sent_at ? new Date(event.sent_at * 1000).toLocaleTimeString() : "";
  const safe = (text: string) => String(text).replace(/[<>&]/g, "");
  if (event.type === "planner_stage_started") return { time, html: `Planning <b>${safe(event.stage)}</b>…` };
  if (event.type === "planner_stage_done") return { time, html: `Finished stage <b>${safe(event.stage)}</b>` };
  if (event.type === "planner_usage") return { time, html: `Planner tokens ${event.usage?.completion_tokens ?? 0} out` };
  if (event.type === "outline_done") return { time, html: `Outline complete — ${event.chapters} chapters` };
  if (event.type === "chapter_started")
    return { time, html: `Chapter ${event.chapter_id} started: <b>${safe(event.name)}</b> (target ${event.target} words)` };
  if (event.type === "chapter_done")
    return {
      time,
      html: `<span class="ev-ok">✔ Chapter ${event.chapter_id} accepted</span> (${event.word_count}/${event.target} words)`,
    };
  if (event.type === "chapter_retry")
    return { time, html: `<span class="ev-fail">✘ chapter ${event.chapter_id} retry #${event.attempt}: ${safe(event.error)}</span>` };
  if (event.type === "chapter_edited") return { time, html: `Chapter ${event.chapter_id} edited by author` };
  if (event.type === "chapter_rewrite_requested")
    return { time, html: `Rewrite requested for chapter ${event.chapter_id}` };
  if (event.type === "author_note_added") return { time, html: `Author note stored for next chapter` };
  if (event.type === "review_pending") return { time, html: `<span class="ev-ok">Review gate: waiting for your decision on chapter ${event.chapter_id}</span>` };
  if (event.type === "tool_event") {
    const tool = event.tool ?? "?";
    const summary = event.summary ?? {};
    const ok = summary.ok !== false && event.ok !== false;
    let detail = "";
    if (tool === "search") detail = `"${safe(summary.query ?? "")}" → ${summary.hit_count ?? 0} hits`;
    else if (tool === "read") detail = `chapter ${summary.chapter_id}`;
    else if (tool === "write") detail = `${summary.word_count ?? "?"} words vs range ${JSON.stringify(summary.accepted_range ?? [])}`;
    else if (tool === "update") detail = ok ? "state updated" : (summary.errors ?? ["error"]).join("; ");
    else if (tool === "correct") detail = `chapter ${summary.chapter_id}`;
    return {
      time,
      html: `<span class="ev-tool">${tool}</span> ${ok ? '<span class="ev-ok">ok</span>' : '<span class="ev-fail">rejected</span>'} — ${detail}`,
    };
  }
  if (event.type === "llm_usage")
    return { time, html: `tokens: ${event.usage.prompt_tokens} in / ${event.usage.completion_tokens} out` };
  if (event.type === "runner_status") return { time, html: `Runner: ${safe(event.status)}` };
  if (event.type === "runner_error")
    return { time, html: `<span class="ev-fail">⚠ runner error: ${safe(event.error)}</span>` };
  if (event.type === "runner_stopped") return { time, html: "Runner stopped" };
  return { time, html: safe(event.type ?? "") };
}

export default function WriteTab({
  book,
  events,
  refreshBook,
  showToast,
}: {
  book: Book;
  events: any[];
  refreshBook: () => Promise<void>;
  showToast: (m: string) => void;
}) {
  const [chapters, setChapters] = useState<ChapterRecord[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [editContent, setEditContent] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [writerProfileId, setWriterProfileId] = useState(book.writer_profile_id ?? "");
  const [notes, setNotes] = useState(book.author_notes ?? "");
  const [reviewContent, setReviewContent] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [mode, setMode] = useState<"auto" | "review" | "single">(book.run_settings?.mode ?? "auto");
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setNotes(book.author_notes ?? "");
    setMode(book.run_settings?.mode ?? "auto");
  }, [book.author_notes, book.run_settings?.mode]);

  const refreshChapters = () =>
    api
      .getChapters(book.id)
      .then((r) => setChapters(r.chapters))
      .catch(() => {});

  useEffect(() => {
    refreshChapters();
  }, [book.id, book.chapters_written, book.runner?.status]);

  useEffect(() => {
    api.listProfiles().then((r) => {
      setProfiles(r.profiles);
      setWriterProfileId((prev) => prev || r.profiles[0]?.id || "");
    });
  }, []);

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [events.length]);

  const runnerStatus = book.runner?.status ?? "idle";
  const busy = ["writing", "waiting_review", "generating_outline", "stopping"].includes(runnerStatus);

  const chaptersPlanned = book.outline?.chapters?.length ?? 0;
  const outlineChapters = book.outline?.chapters ?? [];

  const selectedChapter = chapters.find((c) => c.id === selected) ?? null;

  useEffect(() => {
    setEditContent(null);
  }, [selected]);

  const startRun = async () => {
    if (!outlineChapters.length || !book.outline?.frozen) {
      showToast("Freeze the outline first (Outline tab)");
      return;
    }
    if (!writerProfileId) {
      showToast("Select a writer LLM profile");
      return;
    }
    try {
      await api.patchBook(book.id, { run_settings: { ...book.run_settings, mode } });
      await api.runStart(book.id, { writer_profile_id: writerProfileId });
      showToast("Writing started");
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const runnerAction = async (fn: () => Promise<any>, label: string) => {
    try {
      await fn();
      showToast(label);
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const saveNotes = async () => {
    await api.putNotes(book.id, notes);
    showToast("Author notes stored — injected into the next chapter prompt");
    refreshBook();
  };

  const submitReview = async (action: "approve" | "edit" | "rewrite") => {
    const payload: any = { action };
    if (action === "edit") payload.content = reviewContent;
    if (action === "rewrite") payload.note = reviewNote;
    try {
      await api.runReview(book.id, payload);
      setReviewContent("");
      setReviewNote("");
      showToast(action === "approve" ? "Approved — continuing" : action === "edit" ? "Edit saved" : "Rewriting chapter");
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const saveChapterEdit = async () => {
    if (selected === null || editContent === null) return;
    try {
      await api.patchChapter(book.id, selected, { content: editContent });
      showToast("Chapter text saved (statistics will reflect this)");
      setEditContent(null);
      refreshChapters();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const totalWords = chapters.reduce((s, c) => s + (c.written_words ?? 0), 0);

  const feedLines = useMemo(
    () => events.slice(-200).map((event, i) => ({ ...formatEvent(event), key: i })),
    [events],
  );

  return (
    <div>
      {runnerStatus === "waiting_review" && (
        <div className="review-banner">
          <strong>Review gate — chapter {book.runner?.current_chapter ?? "?"} complete.</strong>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button className="btn-primary btn-good" onClick={() => submitReview("approve")}>
              Approve & continue
            </button>
            <button className="btn-primary" onClick={() => submitReview("edit")}>
              Edit
            </button>
            <button className="btn-primary" onClick={() => submitReview("rewrite")}>
              Rewrite chapter
            </button>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 10 }}>
            <textarea
              rows={3}
              placeholder="For Edit: paste the corrected chapter text here…"
              value={reviewContent}
              onChange={(e) => setReviewContent(e.target.value)}
            />
            <textarea
              rows={3}
              placeholder="For Rewrite: instructions for the agent, e.g. 'too abrupt — give Marta more doubt' (stored as author note)…"
              value={reviewNote}
              onChange={(e) => setReviewNote(e.target.value)}
            />
          </div>
        </div>
      )}

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
          <div className="form-row" style={{ margin: 0, alignItems: "flex-end" }}>
            <div className="form-col" style={{ minWidth: 260 }}>
              <label>Writer LLM profile</label>
              <select value={writerProfileId} onChange={(e) => setWriterProfileId(e.target.value)} disabled={busy}>
                <option value="">— select —</option>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.model})
                  </option>
                ))}
              </select>
            </div>
            <div className="form-col" style={{ minWidth: 220 }}>
              <label>Run mode</label>
              <select value={mode} onChange={(e) => setMode(e.target.value as any)} disabled={busy}>
                <option value="auto">Full auto — write all chapters</option>
                <option value="review">Review each chapter</option>
                <option value="single">Single step — write one chapter</option>
              </select>
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {!busy && (
              <button className="btn-primary" onClick={startRun}>
                {mode === "single" && (book.chapters_written ?? 0) > 0 ? "▶ Write next chapter" : "▶ Start writing"}
              </button>
            )}
            {(runnerStatus === "writing" || runnerStatus === "generating_outline") && (
              <>
                <button onClick={() => runnerAction(api.runPause.bind(null, book.id), "Pausing after this chapter")}>
                  ⏸ Pause
                </button>
                <button className="btn-danger" onClick={() => runnerAction(api.runStop.bind(null, book.id), "Stopping after this chapter")}>
                  ⏹ Stop
                </button>
                <button
                  className="btn-danger"
                  onClick={() => {
                    if (!window.confirm("Abort now? The in-flight LLM call is cut immediately. The current chapter restarts from scratch on the next start.")) return;
                    runnerAction(api.runAbort.bind(null, book.id), "Aborted");
                  }}
                >
                  ◼ Abort now
                </button>
              </>
            )}
            {runnerStatus === "paused" && book.runner?.task_alive && (
              <button className="btn-primary" onClick={() => runnerAction(api.runResume.bind(null, book.id), "Resumed")}>
                ▶ Resume
              </button>
            )}
            {["done", "stopped", "stepped"].includes(runnerStatus) ||
            (runnerStatus === "paused" && !book.runner?.task_alive) ? (
              <button className="btn-primary" onClick={startRun}>
                {mode === "single" ? "▶ Write next chapter" : "▶ Continue writing"}
              </button>
            ) : null}
          </div>
        </div>
        <div className="inline-note">
          {chapters.length} of {chaptersPlanned} chapters written · {totalWords.toLocaleString()} words · the state,
          chapters and metrics checkpoint after every chapter — you can pause, stop, abort or close the browser anytime.
        </div>
      </div>

      <div className="card">
        <strong>Author notes</strong>
        <div className="inline-note">
          Anything you type here is injected into the next chapter prompt (# Author Notes) and cleared afterwards.
        </div>
        <textarea
          rows={2}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="e.g. Slow down in the next chapter; plant the red umbrella as a recurring image."
        />
        <div style={{ textAlign: "right", marginTop: 6 }}>
          <button disabled={busy} onClick={saveNotes}>
            Save notes
          </button>
        </div>
      </div>

      <div className="split">
        <div className="card" style={{ padding: 8 }}>
          <div className="muted" style={{ padding: "8px 8px 6px", fontSize: 12 }}>
            Chapters
          </div>
          {chapters.length === 0 && <div className="muted" style={{ padding: 8 }}>Nothing written yet.</div>}
          {chapters.map((chapter) => {
            const overUnder = (chapter.written_words ?? 0) - (chapter.target ?? 0);
            return (
              <div key={chapter.id}>
                <div
                  className={`chapter-list-item ${selected === chapter.id ? "selected" : ""}`}
                  onClick={() => setSelected(chapter.id)}
                >
                  <span className="muted" style={{ width: 26, fontSize: 12 }}>
                    {String(chapter.id).padStart(2, "0")}
                  </span>
                  <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {chapter.name}
                  </span>
                  <span className="muted" style={{ fontSize: 11.5 }}>
                    {chapter.written_words?.toLocaleString()} w
                    {chapter.target ? (
                      <span style={{ color: Math.abs(overUnder) <= 0.25 * chapter.target ? "var(--good)" : "var(--warn)" }}>
                        {" "}
                        ({overUnder >= 0 ? "+" : ""}
                        {overUnder})
                      </span>
                    ) : null}
                  </span>
                </div>
              </div>
            );
          })}
        </div>

        <div>
          {(runnerStatus === "writing" ||
            runnerStatus === "waiting_review" ||
            runnerStatus === "generating_outline" ||
            runnerStatus === "paused") && (
            <div className="card">
              <LiveStreamBox events={events} active={runnerStatus === "writing" || runnerStatus === "generating_outline"} />
              {runnerStatus !== "generating_outline" && (
                <>
                  <strong style={{ display: "block", margin: "12px 0 6px" }}>Event log</strong>
                  <div ref={feedRef} className="event-feed" style={{ height: 260 }}>
                    {feedLines.map((line) => (
                      <div key={line.key}>
                        <span className="ev-time">{line.time}</span>
                        <span dangerouslySetInnerHTML={{ __html: line.html }} />
                      </div>
                    ))}
                    {feedLines.length === 0 && <span className="muted">waiting for events…</span>}
                  </div>
                </>
              )}
            </div>
          )}

          {selectedChapter ? (
            <div className="card">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <strong>
                  Chapter {selectedChapter.id}: {selectedChapter.name}
                </strong>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {selectedChapter.written_words?.toLocaleString()} / {selectedChapter.target?.toLocaleString()} words
                    {selectedChapter.usage
                      ? ` · ${(selectedChapter.usage.prompt_tokens ?? 0).toLocaleString()} in / ${(
                          selectedChapter.usage.completion_tokens ?? 0
                        ).toLocaleString()} out tokens · cost €${(selectedChapter.usage.cost ?? 0).toFixed(4)}`
                      : ""}
                  </span>
                  {editContent === null ? (
                    <button onClick={() => setEditContent(selectedChapter.content ?? "")}>Edit</button>
                  ) : (
                    <>
                      <button className="btn-primary" onClick={saveChapterEdit}>
                        Save text
                      </button>
                      <button onClick={() => setEditContent(null)}>Cancel</button>
                    </>
                  )}
                </div>
              </div>
              {editContent !== null ? (
                <textarea rows={22} value={editContent} onChange={(e) => setEditContent(e.target.value)} />
              ) : (
                <div className="prose-view">{selectedChapter.content}</div>
              )}
               {selectedChapter.tool_counts && (
                <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
                  Tool calls:{" "}
                  {Object.entries(selectedChapter.tool_counts)
                    .map(([tool, n]) => `${tool} ${n}`)
                    .join(" · ")}
                </div>
              )}
            </div>
          ) : (
            <div className="card muted">Select a chapter on the left to read or edit it.</div>
          )}
        </div>
      </div>
    </div>
  );
}