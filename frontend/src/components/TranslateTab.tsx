import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Book, Profile, TranslationStatus } from "../types";

function formatTranslationEvent(event: any): { time: string; html: string } {
  const time = event.sent_at ? new Date(event.sent_at * 1000).toLocaleTimeString() : "";
  const safe = (text: string) => String(text).replace(/[<>&]/g, "");
  if (event.type === "translation_started")
    return {
      time,
      html: `Translation started — <b>${safe(event.language)}</b> with <b>${safe(event.profile_name ?? "")}</b> (${
        event.profile_name ? safe(event.model ?? "") : ""
      })${event.test_num > 0 ? ` · test mode: first ${event.test_num} lines` : ""}`,
    };
  if (event.type === "translation_output") return { time, html: safe((event.lines ?? []).slice(-2).join(" \u00b7 ")) };
  if (event.type === "translation_done")
    return event.status === "done"
      ? {
          time,
          html: `<span class="ev-ok">✔ Translation complete</span> — saved <code>${safe(event.output_file ?? "")}</code>`,
        }
      : {
          time,
          html: `<span style="color: var(--warn)">⚠ Translation interrupted — partial output kept (resume possible)</span>`,
        };
  if (event.type === "translation_error")
    return { time, html: `<span class="ev-fail">✘ Translation error: ${safe(event.error ?? "")}</span>` };
  return { time, html: safe(event.type ?? "") };
}

const TRANSLATION_EVENTS = ["translation_started", "translation_output", "translation_done", "translation_error"];

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
  const [targetLanguage, setTargetLanguage] = useState("German");
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
    });
  }, []);

  const refreshStatus = () =>
    api.translateStatus(book.id).then((r) => setTStatus(r.status)).catch(() => {});

  const refreshOutputs = () =>
    api.translateOutputs(book.id).then((r) => setOutputs(r.outputs)).catch(() => {});

  useEffect(() => {
    refreshStatus();
    refreshOutputs();
    const timer = window.setInterval(refreshStatus, 3000);
    return () => window.clearInterval(timer);
  }, [book.id]);

  useEffect(() => {
    if (tStatus?.status === "done" || tStatus?.status === "stopped" || tStatus?.status === "error") refreshOutputs();
  }, [tStatus?.status]);

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [events.length]);

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
      await api.translateStart(book.id, {
        target_language: targetLanguage,
        profile_id: profileId,
        single_translate: singleTranslate,
        test_num: testMode ? testNum : 0,
        batch_size: batchSize,
        resume: resumeRun,
      });
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
        <strong>Bilingual translation (powered by bilingual_book_maker, MIT)</strong>
        <div className="inline-note">
          Translates the written chapters into a side-by-side bilingual edition. Runs as its own
          process beside the writing run — the writer keeps its chapters and checkpoints untouched.
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
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
          <div className="form-col" style={{ minWidth: 180 }}>
            <label>Target language</label>
            <input value={targetLanguage} onChange={(e) => setTargetLanguage(e.target.value)} disabled={busy} />
          </div>
          <div className="form-col" style={{ minWidth: 140 }}>
            <label>Batch size (lines/request)</label>
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
            {!busy && (tState === "stopped" || tState === "error") && (
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