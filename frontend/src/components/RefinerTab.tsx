import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Book, ChapterRecord, Profile } from "../types";
import ActivityChip from "./ActivityChip";

const REFINE_WARNING = "Refinement rewrites the English original in stronger prose: a draft model rewrites each block freely, the strongest model refines it up to N rounds (stops early when text barely changes), then findings are only flagged for a human decision. The original manuscript under chapters/ is never touched — every finished chapter is committed as a NEW version file under refined/ (v0001, v0002, …). Nothing is overwritten.";

function formatRefineEvent(event: any): { time: string; html: string } {
  const time = event.sent_at ? new Date(event.sent_at * 1000).toLocaleTimeString() : "";
  const safe = (text: string) => String(text).replace(/[<>&]/g, "");
  if (event.type === "refine_started") {
    return {
      time,
      html: `Refinement started — <b>${safe(event.language ?? "English")}</b> · draft <b>${safe(event.draft ?? "")}</b> · refiner <b>${safe(event.refiner ?? "")}</b> · checker <b>${safe(event.checker ?? "— (length watchdog only)")}</b>${event.chapters?.length ? ` · chapters ${event.chapters.map((c: number) => c + 1).join(", ")}` : ""}`,
    };
  }
  if (event.type === "refine_batch") {
    if (event.error) return { time, html: `<span class="ev-fail">✘ block ${event.batch_index}/${event.batches_total} failed: ${safe(event.error)}</span>` };
    if (event.note) return { time, html: safe(event.note) };
    const stage = ({ draft: "draft", refine: "refined", check: "checked" } as Record<string, string>)[event.stage] ?? event.stage;
    const extras = [
      event.paragraphs ? `${event.paragraphs} ¶` : "",
      event.notes ? `${event.notes} notes` : "",
      event.flags ? `<span style="color: var(--warn)">⚠ ${event.flags} flag(s)</span>` : "",
    ].filter(Boolean).join(" · ");
    return {
      time,
      html: `<span class="ev-ok">✔ block ${event.batch_index}/${event.batches_total} (chapter ${event.chapter_id ?? "?"})</span> — ${safe(stage)}${extras ? ` · ${extras}` : ""}`,
    };
  }
  if (event.type === "refine_chapter") {
    return {
      time,
      html: `<span class="ev-ok">✔ Chapter ${event.chapter_id + 1} committed → refined/ <b>v${String(event.version).padStart(4, "0")}</b></span> (${event.words ?? 0} words)`,
    };
  }
  if (event.type === "refine_progress")
    return {
      time,
      html: `<span class="ev-ok">¶ ${event.paragraph}/${event.paragraphs_total} verfeinert</span> — ${event.rounds} Runde(n)${event.stopped_early ? " · Frühstopp" : ""}`,
    };
  if (event.type === "llm_call") {
    if (event.phase === "start")
      return { time, html: `⌛ <b>${safe(event.role ?? "")}</b> arbeitet… ${safe(event.detail ?? "")}` };
    if (event.phase === "done")
      return { time, html: `<span class="ev-ok">✔ ${safe(event.role ?? "")}</span> fertig in ${safe(String(event.elapsed_s ?? "?"))}s — ${safe(event.detail ?? "")}` };
    return { time, html: `<span class="ev-fail">✘ ${safe(event.role ?? "")} Fehler nach ${safe(String(event.elapsed_s ?? "?"))}s</span> — ${safe(event.detail ?? "")}` };
  }
  if (event.type === "refine_output") return { time, html: safe((event.lines ?? []).slice(-2).join(" \u00b7 ")) };
  if (event.type === "refine_done") {
    if (event.status === "done")
      return {
        time,
        html: `<span class="ev-ok">✔ Refinement complete</span> — versions kept in <code>refined/</code>${
          event.gaps ? ` <span style="color: var(--warn)">(⚠ ${event.gaps} blocks failed)</span>` : ""
        }${
          event.review_flags !== undefined
            ? ` · review: ${event.review_flags} flag(s), ${event.annotations} note(s)`
            : ""
        }`,
      };
    return { time, html: `<span style="color: var(--warn)">⚠ Refinement interrupted — saved stages kept (resume possible)</span>` };
  }
  if (event.type === "refine_error")
    return { time, html: `<span class="ev-fail">✘ Refinement error: ${safe(event.error ?? "")}</span>` };
  return { time, html: safe(event.type ?? "") };
}

const REFINE_EVENTS = ["refine_started", "refine_batch", "refine_chapter", "refine_progress", "refine_output", "refine_done", "refine_error", "llm_call"];

type VersionInfo = {
  version: number;
  created: string | null;
  words: number;
  review_flags: number;
  models: Record<string, string | null>;
  run_started_at: number | null;
};

export default function RefinerTab({
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
  const saved = book.refine_settings ?? {};
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [draftId, setDraftId] = useState<string>(saved.draft_profile_id ?? "");
  const [refinerId, setRefinerId] = useState<string>(saved.refiner_profile_id ?? "");
  const [checkerId, setCheckerId] = useState<string>(saved.checker_profile_id ?? "");
  const [refineRounds, setRefineRounds] = useState<number>(saved.refine_rounds ?? 2);
  const [draftTemp, setDraftTemp] = useState<number>(saved.draft_temperature ?? 0.7);
  const [similarityStop, setSimilarityStop] = useState<number>(saved.similarity_stop ?? 0.97);
  const [ratioMin, setRatioMin] = useState<number>(saved.ratio_min ?? 0.75);
  const [ratioMax, setRatioMax] = useState<number>(saved.ratio_max ?? 1.5);
  const [batchWords, setBatchWords] = useState<number>(saved.batch_words ?? 0);
  const [checkMode, setCheckMode] = useState<string>(saved.check_mode ?? "essential");
  const [styleProbe, setStyleProbe] = useState<string>(saved.style_probe ?? "");
  const [unloadAfterCall, setUnloadAfterCall] = useState<boolean>(saved.unload_after_call ?? true);
  const [chapters, setChapters] = useState<ChapterRecord[]>([]);
  const [chapterSelection, setChapterSelection] = useState<Set<number> | null>(null);
  const [rStatus, setRStatus] = useState<Record<string, any> | null>(book.refine ?? null);
  const [outputs, setOutputs] = useState<{ path: string; size: number; modified: number }[]>([]);
  const [viewContent, setViewContent] = useState<{ path: string; content: string } | null>(null);
  const [liveRefined, setLiveRefined] = useState<string | null>(null);
  const [versionChapters, setVersionChapters] = useState<any[]>([]);
  const [versionView, setVersionView] = useState<{ chapterId: number; chapter: string; version: number; content: string; created: string | null } | null>(null);
  const [chapterProgress, setChapterProgress] = useState<{
    chapters: {
      chapter_id: number;
      chapter_name: string;
      words_total: number;
      refined_words: number;
      batches_total: number;
      batches_done: number;
      paragraphs: number;
      review_flags: number;
      started: boolean;
      done: boolean;
      versions: number[];
      active_version: number | null;
    }[];
  } | null>(null);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setRStatus(book.refine ?? null);
  }, [book.refine?.status]);

  useEffect(() => {
    api.listProfiles().then((r) => {
      setProfiles(r.profiles);
      setDraftId((prev) => prev || (book.writer_profile_id ?? "") || r.profiles[0]?.id || "");
      setRefinerId((prev) => prev || r.profiles[1]?.id || r.profiles[0]?.id || "");
    });
  }, []);

  const refreshStatus = () =>
    api.refineStatus(book.id).then((r) => setRStatus(r.status)).catch(() => {});

  const refreshOutputs = () =>
    api.refineOutputs(book.id).then((r) => setOutputs(r.outputs)).catch(() => {});

  const refreshVersions = () =>
    api.refineVersions(book.id).then((r) => setVersionChapters(r.chapters)).catch(() => {});

  useEffect(() => {
    refreshStatus();
    refreshOutputs();
    refreshVersions();
    api.refineProgress(book.id).then(setChapterProgress).catch(() => {});
    api.getChapters(book.id).then((r) => setChapters(r.chapters)).catch(() => {});
    const timer = window.setInterval(() => {
      refreshStatus();
      refreshOutputs();
      refreshVersions();
      api.refineProgress(book.id).then(setChapterProgress).catch(() => {});
    }, 3000);
    return () => window.clearInterval(timer);
  }, [book.id]);

  useEffect(() => {
    if (rStatus?.status === "done" || rStatus?.status === "stopped" || rStatus?.status === "error") {
      refreshOutputs();
      refreshVersions();
    }
  }, [rStatus?.status]);

  const refineSettings = useMemo(
    () => ({
      draft_profile_id: draftId,
      refiner_profile_id: refinerId,
      checker_profile_id: checkerId,
      unload_after_call: unloadAfterCall,
      batch_words: batchWords,
      check_mode: checkMode,
      draft_temperature: draftTemp,
      refine_rounds: refineRounds,
      similarity_stop: similarityStop,
      ratio_min: ratioMin,
      ratio_max: ratioMax,
      style_probe: styleProbe,
    }),
    [draftId, refinerId, checkerId, unloadAfterCall, batchWords, checkMode, refineRounds, similarityStop, ratioMin, ratioMax, styleProbe, draftTemp],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.patchBook(book.id, { refine_settings: refineSettings }).catch(() => {});
    }, 800);
    return () => window.clearTimeout(timer);
  }, [book.id, JSON.stringify(refineSettings)]);

  const livePath = useMemo(
    () => outputs.find((o) => o.path.endsWith("book_refined_current.txt"))?.path ?? null,
    [outputs],
  );

  useEffect(() => {
    setLiveRefined(null);
    if (!livePath) return;
    let cancelled = false;
    const load = async () => {
      try {
        const next = await api.refineFile(book.id, livePath);
        if (!cancelled) setLiveRefined(next.content);
      } catch {
        if (!cancelled) setLiveRefined("");
      }
    };
    load();
    const timer = window.setInterval(load, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [livePath, book.id]);

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [events.length]);

  useEffect(() => {
    if (!viewContent) return;
    const path = viewContent.path;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.refineFile(book.id, path);
        setViewContent((prev) => (prev && prev.path === path ? next : prev));
      } catch {
        /* file gone mid-run — keep last content */
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [viewContent?.path, book.id]);

  useEffect(() => {
    api.getChapters(book.id).then((r) => setChapters(r.chapters)).catch(() => {});
  }, [book.id, book.chapters_written || 0]);

  const refineEvent = useMemo(
    () =>
      events
        .filter((e) => REFINE_EVENTS.includes(e.type))
        .slice(-100)
        .map((event, i) => ({ ...formatRefineEvent(event), key: i })),
    [events],
  );

  const statusValues = rStatus ?? book.refine ?? null;
  const rState = (statusValues?.status ?? "idle") as string;
  const busy = rState === "running" || rState === "stopping";

  const openFlags = (chapterProgress?.chapters ?? []).reduce((sum, ch) => sum + (ch.review_flags ?? 0), 0);

  const start = async (resumeRun: boolean) => {
    if (!(book.chapters_written ?? 0)) {
      showToast("Nothing written yet — write at least one chapter first");
      return;
    }
    if (!draftId) {
      showToast("Select a draft profile");
      return;
    }
    if (!refinerId) {
      showToast("Select a refiner profile (the strongest available model)");
      return;
    }
    try {
      await api.refineStart(book.id, {
        language: "English",
        draft_profile_id: draftId,
        refiner_profile_id: refinerId,
        checker_profile_id: checkerId || null,
        unload_after_call: unloadAfterCall,
        chapter_ids: chapterSelection !== null ? Array.from(chapterSelection) : null,
        resume: resumeRun,
        batch_words: batchWords || 0,
        refine_rounds: refineRounds,
        similarity_stop: similarityStop,
        ratio_min: ratioMin,
        ratio_max: ratioMax,
        draft_temperature: draftTemp,
        style_probe: styleProbe,
        check_mode: checkMode === "strict" ? "strict" : "essential",
      });
      showToast(resumeRun ? "Resuming refinement" : "Refinement started");
      refreshStatus();
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const stop = async () => {
    try {
      await api.refineStop(book.id);
      showToast("Stopping — finished stages are saved");
      refreshStatus();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const view = async (path: string) => {
    try {
      setViewContent(await api.refineFile(book.id, path));
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const download = async (path: string, name?: string) => {
    try {
      const { content } = await api.refineFile(book.id, path);
      const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name ?? path.split("/").pop() ?? "refined.txt";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const viewVersion = async (chapterId: number, chapterName: string, version: number) => {
    try {
      const payload = await api.refineVersion(book.id, chapterId, version);
      setVersionView({
        chapterId,
        chapter: chapterName,
        version,
        content: String(payload.content ?? ""),
        created: payload.created ?? null,
      });
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const selectVersion = async (chapterId: number, version: number) => {
    try {
      await api.refineSelectVersion(book.id, chapterId, version);
      showToast(`Chapter ${chapterId + 1}: active version is now v${String(version).padStart(4, "0")}`);
      refreshVersions();
      refreshOutputs();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  return (
    <div>
      <div className="card">
        <strong>English prose refiner (draft → refine → check)</strong>
        <div className="inline-note">
          Rewrites the original manuscript into stronger prose instead of translating it. Runs beside
          the writing run. The original chapters stay untouched — refined chapters land as versioned
          files under <code>refined/</code>, one new file per run (never overwritten).
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="form-col" style={{ minWidth: 250 }}>
            <label>Draft profile (free rewrite, temp 0.7)</label>
            <select value={draftId} onChange={(e) => setDraftId(e.target.value)} disabled={busy}>
              <option value="">— select —</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.model})
                </option>
              ))}
            </select>
          </div>
          <div className="form-col" style={{ minWidth: 250 }}>
            <label>Refiner profile (strongest model, temp 0.2)</label>
            <select value={refinerId} onChange={(e) => setRefinerId(e.target.value)} disabled={busy}>
              <option value="">— select —</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.model})
                </option>
              ))}
            </select>
          </div>
          <div className="form-col" style={{ minWidth: 250 }}>
            <label>Fidelity checker (separate model, temp 0 — optional)</label>
            <select value={checkerId} onChange={(e) => setCheckerId(e.target.value)} disabled={busy}>
              <option value="">— none (length watchdog only) —</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.model})
                </option>
              ))}
            </select>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginTop: 10 }}>
          <div className="form-col" style={{ minWidth: 130 }}>
            <label>Refine rounds (max)</label>
            <input
              type="number"
              min={1}
              max={6}
              value={refineRounds}
              onChange={(e) => setRefineRounds(Number(e.target.value) || 2)}
              disabled={busy}
            />
          </div>
          <div className="form-col" style={{ minWidth: 160 }}>
            <label>Draft temperature (default 0.7 — 0.3 was best in test)</label>
            <input
              type="number"
              min={0}
              max={2}
              step={0.1}
              value={draftTemp}
              onChange={(e) => setDraftTemp(Number(e.target.value) || 0)}
              disabled={busy}
            />
          </div>
          <div className="form-col" style={{ minWidth: 150 }}>
            <label>Early stop similarity</label>
            <input
              type="number"
              min={0.5}
              max={1}
              step={0.01}
              value={similarityStop}
              onChange={(e) => setSimilarityStop(Number(e.target.value) || 0.97)}
              disabled={busy}
            />
          </div>
          <div className="form-col" style={{ minWidth: 110 }}>
            <label>Length ratio min</label>
            <input
              type="number"
              min={0.1}
              max={2}
              step={0.05}
              value={ratioMin}
              onChange={(e) => setRatioMin(Number(e.target.value) || 0.75)}
              disabled={busy}
            />
          </div>
          <div className="form-col" style={{ minWidth: 110 }}>
            <label>Length ratio max</label>
            <input
              type="number"
              min={0.5}
              max={5}
              step={0.05}
              value={ratioMax}
              onChange={(e) => setRatioMax(Number(e.target.value) || 1.5)}
              disabled={busy}
            />
          </div>
          <div className="form-col" style={{ minWidth: 150 }}>
            <label>Words per draft block (min, 0 = auto 250)</label>
            <input
              type="number"
              min={0}
              max={5000}
              value={batchWords}
              onChange={(e) => setBatchWords(Number(e.target.value) || 0)}
              disabled={busy}
              placeholder="auto (250)"
            />
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
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginTop: 10 }}>
          <div className="form-col" style={{ minWidth: 280 }}>
            <label>Review checks</label>
            <select value={checkMode} onChange={(e) => setCheckMode(e.target.value)} disabled={busy}>
              <option value="essential">Nur echte Fehler (Standard — keine Scores)</option>
              <option value="strict">Streng: Längen-/Treue-Prüfung flaggen</option>
            </select>
          </div>
          <div className="form-col" style={{ maxWidth: 640 }}>
            <label>Style probe (target tone as an example, optional)</label>
            <textarea
              rows={2}
              value={styleProbe}
              onChange={(e) => setStyleProbe(e.target.value)}
              disabled={busy}
              placeholder="Paste a paragraph whose tone you want as the target tone…"
            />
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          {!busy && (
            <button className="btn-primary" onClick={() => start(false)}>
              ▶ Start refinement
            </button>
          )}
          {!busy && (
            <button
              className="btn-primary"
              onClick={() => start(true)}
              title="Setzt den Lauf fort: fertige Blöcke bleiben, unfertige laufen weiter"
            >
              ⟳ Resume last run
            </button>
          )}
          {!busy && (
            <button
              className="btn-danger"
              onClick={async () => {
                if (!window.confirm("ALLES löschen — komplette Verfeinerung inkl. ALLER Versionen unter refined/? Danach mit ▶ Start von Null neu.")) return;
                try {
                  const r = await api.refineResetAll(book.id);
                  showToast(`Gelöscht: ${r.removed.join(", ") || "nichts vorhanden"}`);
                  refreshStatus();
                  refreshVersions();
                  refreshOutputs();
                  refreshBook();
                } catch (err: any) {
                  showToast(err.message);
                }
              }}
            >
              🧹 Alles löschen
            </button>
          )}
          {busy && (
            <button className="btn-danger" onClick={stop}>
              ⏹ Stop
            </button>
          )}
        </div>
        <div className="inline-note" style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <span className={`chip status-${rState}`}>{rState}</span>
          {statusValues?.language && <span className="chip">{statusValues.language}</span>}
          {statusValues?.profile_name && (
            <span className="chip">
              {statusValues.profile_name} · {statusValues.model}
            </span>
          )}
          {statusValues?.error && <span style={{ color: "var(--danger)", fontSize: 12.5 }}>{statusValues.error}</span>}
          <ActivityChip
            activity={statusValues?.activity}
            lastActivity={statusValues?.last_activity}
            running={busy}
          />
        </div>
        <div className="inline-note" style={{ marginTop: 8 }}>
          {REFINE_WARNING}
        </div>
      </div>

      {(chapterProgress?.chapters?.length ?? 0) > 0 && (
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>Chapter progress</strong>
            {openFlags > 0 && (
              <span className="chip" style={{ color: "var(--warn)", borderColor: "var(--warn)" }}>
                {openFlags} open flags → review_list.md
              </span>
            )}
          </div>
          <div className="chapter-list-item" style={{ fontWeight: "normal" }}>
            <span className="muted" style={{ width: 26 }}></span>
            <span style={{ flex: 1 }}></span>
            <span className="muted" style={{ fontSize: 11.5 }}>blocks</span>
            <span className="muted" style={{ fontSize: 11.5 }}>¶</span>
            <span className="muted" style={{ fontSize: 11.5 }}>flags</span>
            <span className="muted" style={{ fontSize: 11.5 }}>version</span>
          </div>
          {chapterProgress!.chapters.map((chapter) => {
            const percent =
              chapter.words_total > 0
                ? Math.min(100, Math.round((chapter.refined_words / chapter.words_total) * 100))
                : null;
            return (
              <div key={chapter.chapter_id} className="chapter-list-item">
                <span style={{ width: 26, fontSize: 13, textAlign: "center" }}>
                  {chapter.done ? "✔" : chapter.started ? "…" : "—"}
                </span>
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {chapter.chapter_name}
                </span>
                <span style={{ width: 90, height: 6, background: "rgba(128,140,160,0.25)", borderRadius: 3, overflow: "hidden", display: "inline-block" }}>
                  <span
                    style={{
                      display: "block",
                      height: "100%",
                      width: `${percent ?? 0}%`,
                      background: percent === 100 ? "var(--ok, #4ade80)" : "var(--accent, #7c9cff)",
                      borderRadius: 3,
                    }}
                  />
                </span>
                <span className="muted" style={{ fontSize: 11.5, width: 44, textAlign: "right" }}>
                  {percent === null ? "" : `${percent}%`}
                </span>
                <span className="muted" style={{ fontSize: 11.5, width: 48, textAlign: "right" }}>
                  {chapter.batches_done}/{chapter.batches_total}
                </span>
                <span className="muted" style={{ fontSize: 11.5, width: 48, textAlign: "right" }}>
                  {chapter.paragraphs}
                </span>
                <span style={{ fontSize: 11.5, width: 48, textAlign: "right", color: chapter.review_flags ? "var(--warn)" : "var(--muted)" }}>
                  {chapter.review_flags}
                </span>
                <span className="muted" style={{ fontSize: 11.5, width: 60, textAlign: "right" }}>
                  {chapter.active_version ? `v${String(chapter.active_version).padStart(4, "0")}` : "—"}
                </span>
              </div>
            );
          })}
        </div>
      )}

      <div className="card">
        <strong>Refined chapter versions</strong>
        <div className="inline-note">
          Each finished chapter run adds one new version file — older versions stay and are never
          rewritten. The <b>active</b> version is what <code>book_refined.txt</code> /{" "}
          <code>manuscript_refined.md</code> contain.
        </div>
        {versionChapters.length === 0 && (
          <div className="muted" style={{ padding: 8 }}>No refined versions yet — start a refinement first.</div>
        )}
        {versionChapters.map((chapter) => {
          const active = chapter.active_version;
          return (
            <div key={chapter.chapter_id} style={{ borderBottom: "1px solid rgba(128,140,160,0.18)", padding: "8px 0" }}>
              <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
                <span style={{ minWidth: 26, fontSize: 12, color: "var(--muted)" }}>
                  {String(chapter.chapter_id).padStart(2, "0")}
                </span>
                <strong style={{ flex: 1 }}>{chapter.chapter_name}</strong>
                <span className="muted" style={{ fontSize: 11.5 }}>
                  original {chapter.original_words?.toLocaleString()} w · {chapter.versions.length} version(s) · active{" "}
                  {active ? `v${String(active).padStart(4, "0")}` : "—"}
                </span>
              </div>
              {chapter.versions.length > 0 && (
                <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 4 }}>
                  {chapter.versions.slice().reverse().map((v: VersionInfo) => (
                    <div key={v.version} className="chapter-list-item" style={{ padding: "4px 6px" }}>
                      <span style={{ fontSize: 12.5, fontWeight: active === v.version ? 700 : 400 }}>
                        v{String(v.version).padStart(4, "0")}
                        {active === v.version && <span style={{ color: "var(--muted)", fontWeight: 400 }}> — active</span>}
                      </span>
                      <span className="muted" style={{ fontSize: 11.5, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {v.created ? new Date(v.created).toLocaleString() : "—"}
                        {v.models?.refiner ? ` · ${v.models.refiner}` : ""}
                        {v.review_flags ? ` · ⚠ ${v.review_flags} flag(s)` : " · ✓"}
                      </span>
                      <span className="muted" style={{ fontSize: 11.5, width: 70, textAlign: "right" }}>
                        {v.words?.toLocaleString()} w
                      </span>
                      <button style={{ fontSize: 11.5, padding: "2px 8px" }} onClick={() => viewVersion(chapter.chapter_id, chapter.chapter_name, v.version)}>
                        ansehen
                      </button>
                      {active !== v.version && (
                        <button style={{ fontSize: 11.5, padding: "2px 8px" }} onClick={() => selectVersion(chapter.chapter_id, v.version)} title="Diese Version als aktive Fassung setzen (book_refined.txt wird neu aufgebaut)">
                          aktiv setzen
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
          <strong>Live output — refined prose grows paragraph by paragraph</strong>
          <span className="muted" style={{ fontSize: 12 }}>updates automatically every 2.5 s</span>
        </div>
        {liveRefined ? (
          <pre className="prose-view" style={{ whiteSpace: "pre-wrap", maxHeight: 420, overflow: "auto" }}>
            {liveRefined}
          </pre>
        ) : (
          <div className="muted" style={{ padding: 10 }}>
            {liveRefined === null
              ? "Loading…"
              : "Nothing yet — after the first draft block of the run, the refined text appears here."}
          </div>
        )}
      </div>

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <strong>
            Chapters to refine{" "}
            <span className="chip" style={{ color: chapterSelection ? "var(--warn)" : "var(--good)" }}>
              {chapterSelection ? `${chapterSelection.size} von ${chapters.length || "?"} gewählt` : "alle"}
            </span>
          </strong>
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
          {outputs.length === 0 && <div className="muted" style={{ padding: 8 }}>No refine outputs yet.</div>}
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
          ) : versionView ? (
            <div className="card">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <strong>
                  {versionView.chapter} — v{String(versionView.version).padStart(4, "0")}{versionView.created ? ` · ${new Date(versionView.created).toLocaleString()}` : ""}
                </strong>
                <div style={{ display: "flex", gap: 6 }}>
                  <button onClick={() => download(`refined/ch${String(versionView.chapterId).padStart(4, "0")}/v${String(versionView.version).padStart(4, "0")}.json`)}>⤓</button>
                  <button onClick={() => setVersionView(null)}>Close</button>
                </div>
              </div>
              <pre className="prose-view" style={{ whiteSpace: "pre-wrap", maxHeight: 560, overflow: "auto" }}>
                {versionView.content}
              </pre>
            </div>
          ) : (
            <div className="card">
              <strong>Refinement log</strong>
              <div ref={feedRef} className="event-feed" style={{ height: 300 }}>
                {refineEvent.map((line) => (
                  <div key={line.key}>
                    <span className="ev-time">{line.time}</span>
                    <span dangerouslySetInnerHTML={{ __html: line.html }} />
                  </div>
                ))}
                {refineEvent.length === 0 && <span className="muted">no refinement events yet…</span>}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}