import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Book, ChapterRecord, Profile, TranslationStatus } from "../types";

const JUDGE_WARNING = "Use several candidate models and a separate judge model: every block is translated by each candidate, the judge picks the winning translation. Local Ollama candidates are unloaded right after each call (keep_alive: 0) so no model clogs VRAM.";

function formatTranslationEvent(event: any): { time: string; html: string } {
  const time = event.sent_at ? new Date(event.sent_at * 1000).toLocaleTimeString() : "";
  const safe = (text: string) => String(text).replace(/[<>&]/g, "");
  if (event.type === "translation_started")
    return {
      time,
      html: `Translation started — <b>${safe(event.language)}</b> · mode <b>${safe(event.mode ?? "tool")}</b>${
        event.candidates?.length ? ` · candidates: ${event.candidates.map((c: string) => safe(c)).join(", ")} · judge: ${safe(event.judge ?? "—")}` : ` · <b>${safe(event.profile_name ?? "")}</b> (${safe(event.model ?? "")})`
      }${event.test_num > 0 ? ` · test mode: first ${event.test_num} lines` : ""}`,
    };
  if (event.type === "translation_batch") {
    if (event.error) return { time, html: `<span class="ev-fail">✘ block ${event.batch_index}/${event.batches_total} failed: ${safe(event.error)}</span>` };
    if (event.note) return { time, html: safe(event.note) };
    return {
      time,
      html: `<span class="ev-ok">✔ block ${event.batch_index}/${event.batches_total} (chapter ${event.chapter_id ?? "?"})</span> — winner <b>${safe(event.winner ?? "")}</b>${event.reason ? `: ${safe(event.reason)}` : ""}`,
    };
  }
  if (event.type === "translation_output") return { time, html: safe((event.lines ?? []).slice(-2).join(" \u00b7 ")) };
  if (event.type === "translation_done") {
    if (event.status === "done")
      return {
        time,
        html: `<span class="ev-ok">✔ Translation complete</span> — saved <code>${safe(event.output_file ?? "")}</code>${
          event.gaps ? ` <span style="color: var(--warn)">(⚠ ${event.gaps} blocks failed)</span>` : ""
        }`,
      };
    return { time, html: `<span style="color: var(--warn)">⚠ Translation interrupted — partial output kept (resume possible)</span>` };
  }
  if (event.type === "translation_error")
    return { time, html: `<span class="ev-fail">✘ Translation error: ${safe(event.error ?? "")}</span>` };
  return { time, html: safe(event.type ?? "") };
}

const TRANSLATION_EVENTS = ["translation_started", "translation_batch", "translation_output", "translation_done", "translation_error"];

export default function TranslateTab({
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
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileId, setProfileId] = useState(book.writer_profile_id ?? "");
  const [mode, setMode] = useState<"tool" | "ensemble">("tool");
  const [candidates, setCandidates] = useState<Set<string>>(new Set());
  const [judgeId, setJudgeId] = useState("");
  const [unloadAfterCall, setUnloadAfterCall] = useState(true);
  const [targetLanguage, setTargetLanguage] = useState("German");
  const [chapters, setChapters] = useState<ChapterRecord[]>([]);
  const [chapterSelection, setChapterSelection] = useState<Set<number> | null>(null);
  const [singleTranslate, setSingleTranslate] = useState(false);
  const [testMode, setTestMode] = useState(false);
  const [testNum, setTestNum] = useState(10);
  const [batchSize, setBatchSize] = useState(10);
  const [resume, setResume] = useState(false);
  const [tStatus, setTStatus] = useState<Record<string, any> | null>(book.translation ?? null);
  const [outputs, setOutputs] = useState<{ path: string; size: number; modified: number }[]>([]);
  const [viewContent, setViewContent] = useState<{ path: string; content: string } | null>(null);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setTStatus(book.translation ?? null);
  }, [book.translation?.status]);

  useEffect(() => {
    api.listProfiles().then((r) => {
      setProfiles(r.profiles);
      setProfileId((prev) => prev || (book.writer_profile_id ?? "") || r.profiles[0]?.id || "");
      setJudgeId((prev) => prev || (book.writer_profile_id ?? "") || r.profiles[0]?.id || "");
      setCandidates((prev) => {
        if (prev.size) return prev;
        const first = new Set<string>();
        const best = r.profiles[0]?.id;
        if (best) first.add(best);
        const second = r.profiles[1]?.id;
        if (second) first.add(second);
        return first;
      });
    });
  }, []);

  const refreshStatus = () =>
    api.translateStatus(book.id).then((r) => setTStatus(r.status)).catch(() => {});

  const refreshOutputs = () =>
    api.translateOutputs(book.id).then((r) => setOutputs(r.outputs)).catch(() => {});

  useEffect(() => {
    refreshStatus();
    refreshOutputs();
    api.getChapters(book.id).then((r) => setChapters(r.chapters)).catch(() => {});
    const timer = window.setInterval(refreshStatus, 3000);
    return () => window.clearInterval(timer);
  }, [book.id]);

  useEffect(() => {
    if (tStatus?.status === "done" || tStatus?.status === "stopped" || tStatus?.status === "error") refreshOutputs();
  }, [tStatus?.status]);

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [events.length]);

  useEffect(() => {
    api.getChapters(book.id).then((r) => setChapters(r.chapters)).catch(() => {});
  }, [book.id, book.chapters_written || 0]);

  const translationEvent = useMemo(
    () =>
      events
        .filter((e) => TRANSLATION_EVENTS.includes(e.type))
        .slice(-100)
        .map((event, i) => ({ ...formatTranslationEvent(event), key: i })),
    [events],
  );

  const statusValues = tStatus ?? book.translation ?? null;
  const tState = (statusValues?.status ?? "idle") as string;
  const toolReady = statusValues?.tool?.ready ?? true;
  const busy = tState === "running" || tState === "stopping";

  const start = async (resumeRun: boolean) => {
    if (!(book.chapters_written ?? 0)) {
      showToast("Nothing written yet — write at least one chapter first");
      return;
    }
    if (!profileId) {
      showToast("Select an LLM profile (same profiles as book writing)");
      return;
    }
    try {
      setResume(resumeRun);
      const payload: any = {
        target_language: targetLanguage,
        mode,
        profile_id: profileId,
        chapter_ids: chapterSelection !== null ? Array.from(chapterSelection) : null,
        single_translate: singleTranslate,
        test_num: testMode ? testNum : 0,
        batch_size: batchSize,
        resume: resumeRun,
      };
      if (mode === "ensemble") {
        payload.candidate_profile_ids = Array.from(candidates);
        payload.judge_profile_id = judgeId || null;
        payload.unload_after_call = unloadAfterCall;
      } else {
        payload.candidate_profile_ids = [];
      }
      await api.translateStart(book.id, payload);
      showToast(resumeRun ? "Resuming translation" : "Translation started");
      refreshStatus();
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const stop = async () => {
    try {
      await api.translateStop(book.id);
      showToast("Stopping — the tool saves its partial output");
      refreshStatus();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const view = async (path: string) => {
    try {
      setViewContent(await api.translateFile(book.id, path));
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const download = async (path: string) => {
    try {
      const { content } = await api.translateFile(book.id, path);
      const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = path.split("/").pop() ?? "bilingual.txt";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      showToast(err.message);
    }
  };

  return (
    <div>
      {!toolReady && (
        <div className="error-banner">
          Translation tool not installed. Run once:
          <pre style={{ margin: "6px 0 0" }}>
            {`git clone --depth 1 https://github.com/yihong0618/bilingual_book_maker.git tools/bilingual_book_maker
python -m venv tools/bbm-venv
tools\\bbm-venv\\Scripts\\pip install -e tools/bilingual_book_maker`}
          </pre>
        </div>
      )}

      <div className="card">
        <strong>Bilingual translation (one dashboard, shared profiles)</strong>
        <div className="inline-note">
          Mode 1 uses bilingual_book_maker (MIT) as an external tool. Mode 2 translates every block
          with several models and a judge model picks the winner. Both run beside the writing run —
          chapters and checkpoints stay untouched.
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="form-col" style={{ minWidth: 240 }}>
            <label>Mode</label>
            <select value={mode} onChange={(e) => setMode(e.target.value as any)} disabled={busy}>
              <option value="tool">Single model (bilingual_book_maker)</option>
              <option value="ensemble">Model ensemble + judge</option>
            </select>
          </div>
          {mode === "tool" ? (
            <div className="form-col" style={{ minWidth: 250 }}>
              <label>LLM profile (shared dashboard profiles)</label>
              <select value={profileId} onChange={(e) => setProfileId(e.target.value)} disabled={busy}>
                <option value="">— select —</option>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.model})
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <div className="form-col" style={{ minWidth: 180 }}>
            <label>Target language</label>
            <input value={targetLanguage} onChange={(e) => setTargetLanguage(e.target.value)} disabled={busy} />
          </div>
          <div className="form-col" style={{ minWidth: 140 }}>
            <label>{mode === "ensemble" ? "Paragraphs per block" : "Batch size (lines/request)"}</label>
            <input
              type="number"
              min={1}
              max={200}
              value={batchSize}
              onChange={(e) => setBatchSize(Number(e.target.value) || 10)}
              disabled={busy}
            />
          </div>
          <div className="form-col" style={{ minWidth: 150 }}>
            <label>
              <input
                type="checkbox"
                checked={singleTranslate}
                onChange={(e) => setSingleTranslate(e.target.checked)}
                disabled={busy}
              />{" "}
              Only translation (no bilingual)
            </label>
            <label>
              <input type="checkbox" checked={testMode} onChange={(e) => setTestMode(e.target.checked)} disabled={busy} />{" "}
              Test mode
            </label>
          </div>
          {testMode && (
            <div className="form-col" style={{ minWidth: 120 }}>
              <label>Test: first N lines</label>
              <input
                type="number"
                min={1}
                value={testNum}
                onChange={(e) => setTestNum(Number(e.target.value) || 10)}
                disabled={busy}
              />
            </div>
          )}
          <div style={{ display: "flex", gap: 8 }}>
            {!busy && (
              <button className="btn-primary" onClick={() => start(false)}>
                ▶ Start translation
              </button>
            )}
            {!busy && ["stopped", "error"].includes(tState) && (
              <button className="btn-primary" onClick={() => start(true)}>
                ⟳ Resume last run
              </button>
            )}
            {busy && (
              <button className="btn-danger" onClick={stop}>
                ⏹ Stop
              </button>
            )}
          </div>
        </div>
        {mode === "ensemble" && (
          <div style={{ marginTop: 12, borderTop: "1px solid rgba(128,140,160,0.25)", paddingTop: 12 }}>
            <div className="inline-note" style={{ marginTop: 0 }}>{JUDGE_WARNING}</div>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
              <div className="form-col" style={{ minWidth: 300 }}>
                <label>Candidate profiles (translate every block)</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {profiles.map((p) => (
                    <label key={p.id} className="chip" style={{ cursor: "pointer", color: candidates.has(p.id) ? "inherit" : "var(--muted)" }}>
                      <input
                        type="checkbox"
                        checked={candidates.has(p.id)}
                        onChange={() =>
                          setCandidates((prev) => {
                            const next = new Set(prev);
                            if (next.has(p.id)) next.delete(p.id);
                            else next.add(p.id);
                            return next;
                          })
                        }
                        disabled={busy}
                      />{" "}
                      {p.name} ({p.model})
                    </label>
                  ))}
                </div>
              </div>
              <div className="form-col" style={{ minWidth: 250 }}>
                <label>Judge model (picks the winner per block)</label>
                <select value={judgeId} onChange={(e) => setJudgeId(e.target.value)} disabled={busy}>
                  <option value="">— none (first candidate wins) —</option>
                  {profiles.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} ({p.model})
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-col" style={{ minWidth: 260 }}>
                <label>
                  <input
                    type="checkbox"
                    checked={unloadAfterCall}
                    onChange={(e) => setUnloadAfterCall(e.target.checked)}
                    disabled={busy}
                  />{" "}
                  Unload Ollama models after every call (keep_alive 0 — VRAM free)
                </label>
              </div>
            </div>
          </div>
        )}
        <div className="inline-note" style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <span className={`chip status-${tState}`}>{tState}</span>
          {statusValues?.language && <span className="chip">{statusValues.language}</span>}
          {statusValues?.profile_name && (
            <span className="chip">
              {statusValues.profile_name} · {statusValues.model}
            </span>
          )}
          {statusValues?.error && <span style={{ color: "var(--danger)", fontSize: 12.5 }}>{statusValues.error}</span>}
        </div>
      </div>

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <strong>Chapters to translate</strong>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => setChapterSelection(null)}>All</button>
            <button onClick={() => setChapterSelection(new Set())}>None</button>
          </div>
        </div>
        {chapters.length === 0 && <div className="muted" style={{ padding: 8 }}>Nothing written yet.</div>}
        {chapters.map((chapter) => {
          const checked = chapterSelection === null || chapterSelection.has(chapter.id);
          return (
            <div
              key={chapter.id}
              className="chapter-list-item"
              onClick={() => {
                const next = new Set(chapterSelection ?? []);
                if (next.has(chapter.id)) next.delete(chapter.id);
                else next.add(chapter.id);
                setChapterSelection(next);
              }}
            >
              <input type="checkbox" checked={checked} readOnly />
              <span className="muted" style={{ width: 26, fontSize: 12 }}>
                {String(chapter.id).padStart(2, "0")}
              </span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {chapter.name}
              </span>
              <span className="muted" style={{ fontSize: 11.5 }}>
                {chapter.written_words?.toLocaleString()} w
              </span>
            </div>
          );
        })}
      </div>

      <div className="split">
        <div className="card" style={{ padding: 8 }}>
          <div className="muted" style={{ padding: "8px 8px 6px", fontSize: 12 }}>
            Outputs ({outputs.length})
          </div>
          {outputs.length === 0 && <div className="muted" style={{ padding: 8 }}>No translation outputs yet.</div>}
          {outputs.map((file) => (
            <div key={file.path} className="chapter-list-item" onClick={() => view(file.path)}>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {file.path}
              </span>
              <span className="muted" style={{ fontSize: 11.5 }}>
                {(file.size / 1024).toFixed(1)} KB
              </span>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  download(file.path);
                }}
              >
                ⤓
              </button>
            </div>
          ))}
        </div>

        <div>
          {viewContent ? (
            <div className="card">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <strong>{viewContent.path}</strong>
                <button onClick={() => setViewContent(null)}>Close</button>
              </div>
              <pre className="prose-view" style={{ whiteSpace: "pre-wrap", maxHeight: 560, overflow: "auto" }}>
                {viewContent.content}
              </pre>
            </div>
          ) : (
            <div className="card">
              <strong>Translation log</strong>
              <div ref={feedRef} className="event-feed" style={{ height: 300 }}>
                {translationEvent.map((line) => (
                  <div key={line.key}>
                    <span className="ev-time">{line.time}</span>
                    <span dangerouslySetInnerHTML={{ __html: line.html }} />
                  </div>
                ))}
                {translationEvent.length === 0 && <span className="muted">no translation events yet…</span>}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}