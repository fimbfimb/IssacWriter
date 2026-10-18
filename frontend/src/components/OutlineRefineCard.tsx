import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Book, Profile } from "../types";
import ActivityChip from "./ActivityChip";

const BRIEF_WARNING = "Verfeinert jede Kapitelbeschreibung EINZELN zu einem vollständigen Kapitelbrief (Title/POV/Setting/Summary/Scenes-Beats/Key Revelations/Emotional Core/Cliffhanger) — im Kontext des ganzen Romans: Premisse, Synopsis, Akte, die Kapitelliste, der Codex, die bereits verfeinerten Vorgängerbriefe und der Ausblick aufs nächste Kapitel. Fakten bleiben: nichts wird erfunden, nichts weggelassen. Die Outline in book.json wird nie angetastet — jeder Brief landet als NEUE Version unter outline_refined/ (v0001, v0002, …).";

function formatEvent(event: any): { time: string; html: string } {
  const time = event.sent_at ? new Date(event.sent_at * 1000).toLocaleTimeString() : "";
  const safe = (text: string) => String(text).replace(/[<>&]/g, "");
  if (event.type === "outline_refine_started") {
    return {
      time,
      html: `Chapter-brief refinement started — <b>${safe(event.profile ?? "")}</b> · ${safe(event.language ?? "English")} · rounds ${event.refine_rounds ?? 1}${event.chapters?.length ? ` · chapters ${event.chapters.map((c: number) => c + 1).join(", ")}` : ""}`,
    };
  }
  if (event.type === "outline_refine_batch") {
    if (event.error) return { time, html: `<span class="ev-fail">✘ chapter ${event.batch_index}/${event.batches_total} failed: ${safe(event.error)}</span>` };
    if (event.note) return { time, html: safe(event.note) };
    return {
      time,
      html: `<span class="ev-ok">✔ chapter ${event.batch_index}/${event.batches_total} (id ${event.chapter_id})</span> — brief ok · ${event.words ?? 0} words · ${event.rounds ?? 1} round(s)`,
    };
  }
  if (event.type === "outline_refine_chapter") {
    return {
      time,
      html: `<span class="ev-ok">✔ Chapter ${event.chapter_id + 1} committed → outline_refined/ <b>v${String(event.version).padStart(4, "0")}</b></span> (${event.words ?? 0} words)`,
    };
  }
  if (event.type === "llm_call") {
    if (event.phase === "start")
      return { time, html: `⌛ <b>${safe(event.role ?? "")}</b> arbeitet… ${safe(event.detail ?? "")}` };
    if (event.phase === "done")
      return { time, html: `<span class="ev-ok">✔ ${safe(event.role ?? "")}</span> fertig in ${safe(String(event.elapsed_s ?? "?"))}s — ${safe(event.detail ?? "")}` };
    return { time, html: `<span class="ev-fail">✘ ${safe(event.role ?? "")} Fehler nach ${safe(String(event.elapsed_s ?? "?"))}s</span> — ${safe(event.detail ?? "")}` };
  }
  if (event.type === "outline_refine_output") return { time, html: safe((event.lines ?? []).slice(-2).join(" \u00b7 ")) };
  if (event.type === "outline_refine_done") {
    if (event.status === "done")
      return {
        time,
        html: `<span class="ev-ok">✔ Chapter briefs complete</span> — versions kept in <code>outline_refined/</code>${event.gaps ? ` <span style="color: var(--warn)">(⚠ ${event.gaps} chapters failed)</span>` : ""}`,
      };
    return { time, html: `<span style="color: var(--warn)">⚠ Chapter-brief refinement interrupted — saved briefs kept (resume possible)</span>` };
  }
  if (event.type === "outline_refine_error")
    return { time, html: `<span class="ev-fail">✘ Outline refine error: ${safe(event.error ?? "")}</span>` };
  return { time, html: safe(event.type ?? "") };
}

const CARD_EVENTS = ["outline_refine_started", "outline_refine_batch", "outline_refine_chapter", "outline_refine_output", "outline_refine_done", "outline_refine_error", "llm_call"];

export default function OutlineRefineCard({
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
  const saved = book.outline_refine_settings ?? {};
  const outline = book.outline;
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileId, setProfileId] = useState<string>(saved.profile_id ?? "");
  const [refineRounds, setRefineRounds] = useState<number>(saved.refine_rounds ?? 1);
  const [similarityStop, setSimilarityStop] = useState<number>(saved.similarity_stop ?? 0.95);
  const [contextPrev, setContextPrev] = useState<number>(saved.context_prev ?? 3);
  const [styleProbe, setStyleProbe] = useState<string>(saved.style_probe ?? "");
  const [unloadAfterCall, setUnloadAfterCall] = useState<boolean>(saved.unload_after_call ?? true);
  const [chapterSelection, setChapterSelection] = useState<Set<number> | null>(null);
  const [rStatus, setRStatus] = useState<Record<string, any> | null>(book.outline_refine ?? null);
  const [progress, setProgress] = useState<{ chapters: any[] } | null>(null);
  const [versionChapters, setVersionChapters] = useState<any[]>([]);
  const [versionView, setVersionView] = useState<{ chapterId: number; chapter: string; version: number; content: string; created: string | null } | null>(null);
  const [outputs, setOutputs] = useState<{ path: string; size: number; modified: number }[]>([]);
  const useDescriptions = book.refined_descriptions_source ?? false;
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setRStatus(book.outline_refine ?? null);
  }, [book.outline_refine?.status]);

  useEffect(() => {
    api.listProfiles().then((r) => {
      setProfiles(r.profiles);
      setProfileId((prev) => prev || (book.planner_profile_id ?? "") || r.profiles[0]?.id || "");
    });
  }, []);

  const refreshStatus = () =>
    api.outlineRefineStatus(book.id).then((r) => setRStatus(r.status)).catch(() => {});
  const refreshProgress = () =>
    api.outlineRefineProgress(book.id).then(setProgress).catch(() => {});
  const refreshVersions = () =>
    api.outlineRefineVersions(book.id).then((r) => setVersionChapters(r.chapters)).catch(() => {});
  const refreshOutputs = () =>
    api.outlineRefineOutputs(book.id).then((r) => setOutputs(r.outputs)).catch(() => {});

  useEffect(() => {
    refreshStatus();
    refreshProgress();
    refreshVersions();
    refreshOutputs();
    const timer = window.setInterval(() => {
      refreshStatus();
      refreshProgress();
      refreshVersions();
      refreshOutputs();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [book.id]);

  const refineSettings = useMemo(
    () => ({
      profile_id: profileId,
      refine_rounds: refineRounds,
      similarity_stop: similarityStop,
      context_prev: contextPrev,
      style_probe: styleProbe,
      unload_after_call: unloadAfterCall,
    }),
    [profileId, refineRounds, similarityStop, contextPrev, styleProbe, unloadAfterCall],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.patchBook(book.id, { outline_refine_settings: refineSettings }).catch(() => {});
    }, 800);
    return () => window.clearTimeout(timer);
  }, [book.id, JSON.stringify(refineSettings)]);

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [events.length]);

  const feed = useMemo(
    () =>
      events
        .filter((e) => CARD_EVENTS.includes(e.type))
        .slice(-80)
        .map((event: any, i: number) => ({ ...formatEvent(event), key: i })),
    [events],
  );

  const statusValues = rStatus ?? book.outline_refine ?? null;
  const rState = (statusValues?.status ?? "idle") as string;
  const busy = rState === "running" || rState === "stopping";
  const outlineChapters = (outline?.chapters ?? []) as any[];
  const doneCount = (progress?.chapters ?? []).filter((c) => c.done).length;

  const start = async (resumeRun: boolean) => {
    if (!outlineChapters.length) {
      showToast("No chapter outline yet — generate the outline first");
      return;
    }
    if (!profileId) {
      showToast("Select an LLM profile (the strongest outline model)");
      return;
    }
    try {
      await api.outlineRefineStart(book.id, {
        profile_id: profileId,
        chapter_ids: chapterSelection !== null ? Array.from(chapterSelection) : null,
        resume: resumeRun,
        refine_rounds: refineRounds,
        similarity_stop: similarityStop,
        context_prev: contextPrev,
        unload_after_call: unloadAfterCall,
        style_probe: styleProbe,
      });
      showToast(resumeRun ? "Resuming chapter briefs" : "Chapter-brief refinement started");
      refreshStatus();
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const stop = async () => {
    try {
      await api.outlineRefineStop(book.id);
      showToast("Stopping — finished briefs are saved");
      refreshStatus();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const viewVersion = async (chapterId: number, chapterName: string, version: number) => {
    try {
      const payload = await api.outlineRefineVersion(book.id, chapterId, version);
      setVersionView({
        chapterId,
        chapter: chapterName,
        version,
        content: String(payload.description ?? ""),
        created: payload.created ?? null,
      });
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const selectVersion = async (chapterId: number, version: number) => {
    try {
      await api.outlineRefineSelectVersion(book.id, chapterId, version);
      showToast(`Chapter ${chapterId + 1}: active brief is now v${String(version).padStart(4, "0")}`);
      refreshVersions();
      refreshOutputs();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const download = async (path: string) => {
    try {
      const { content } = await api.outlineRefineFile(book.id, path);
      const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = path.split("/").pop() ?? "outline_refined.md";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const toggleWriterSource = async () => {
    try {
      await api.patchBook(book.id, { refined_descriptions_source: !useDescriptions });
      showToast(
        !useDescriptions
          ? "Writer übersetzt… nein: schreibt jetzt mit den aktiven Kapitelbriefen (nächste Run)"
          : "Writer schreibt wieder mit den Original-Kapitelbeschreibungen",
      );
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
        <strong>Chapter-brief refinement (outline → cinematic briefs)</strong>
        <button
          onClick={toggleWriterSource}
          className={useDescriptions ? "btn-good" : "btn-ghost"}
          title="Wenn an: der Writer nutzt die AKTIVEN Kapitelbriefe als Kapitelbeschreibung (Original bleibt unverändert in der Outline)"
        >
          {useDescriptions ? "✔ Writer liest Kapitelbriefe" : "Writer: Original-Beschreibungen"}
        </button>
      </div>
      <div className="inline-note">{BRIEF_WARNING}</div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        <div className="form-col" style={{ minWidth: 250 }}>
          <label>LLM profile (story architect)</label>
          <select value={profileId} onChange={(e) => setProfileId(e.target.value)} disabled={busy}>
            <option value="">— select —</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.model})
              </option>
            ))}
          </select>
        </div>
        <div className="form-col" style={{ minWidth: 130 }}>
          <label>Refine rounds (max)</label>
          <input type="number" min={1} max={3} value={refineRounds} onChange={(e) => setRefineRounds(Number(e.target.value) || 1)} disabled={busy} />
        </div>
        <div className="form-col" style={{ minWidth: 150 }}>
          <label>Early stop similarity</label>
          <input type="number" min={0.5} max={1} step={0.01} value={similarityStop} onChange={(e) => setSimilarityStop(Number(e.target.value) || 0.95)} disabled={busy} />
        </div>
        <div className="form-col" style={{ minWidth: 170 }}>
          <label>Context: preceding briefs (0 = all)</label>
          <input type="number" min={0} max={50} value={contextPrev} onChange={(e) => setContextPrev(Number(e.target.value) || 0)} disabled={busy} />
        </div>
        <div className="form-col" style={{ minWidth: 260 }}>
          <label>
            <input type="checkbox" checked={unloadAfterCall} onChange={(e) => setUnloadAfterCall(e.target.checked)} disabled={busy} />{" "}
            Unload Ollama models after every call
          </label>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {!busy && (
            <button className="btn-primary" onClick={() => start(false)}>
              ▶ Briefs verfeinern
            </button>
          )}
          {!busy && (
            <button className="btn-primary" onClick={() => start(true)} title="Fertige Briefe bleiben, Rest läuft weiter">
              ⟳ Resume
            </button>
          )}
          {!busy && (
            <button
              className="btn-danger"
              onClick={async () => {
                if (!window.confirm("ALLES löschen — alle Kapitelbriefe inkl. ALLER Versionen unter outline_refined/?")) return;
                try {
                  const r = await api.outlineRefineResetAll(book.id);
                  showToast(`Gelöscht: ${r.removed.join(", ") || "nichts vorhanden"}`);
                  refreshStatus();
                  refreshVersions();
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
      </div>
      <div style={{ marginTop: 10 }}>
        <div className="form-col" style={{ maxWidth: 640 }}>
          <label>Style probe (optional — gewünschte Brief-Sprache/Beispiel)</label>
          <textarea
            rows={2}
            value={styleProbe}
            onChange={(e) => setStyleProbe(e.target.value)}
            disabled={busy}
            placeholder="Optional: ein Beispiel-Kapitelbrief als Ton-Angabe…"
          />
        </div>
      </div>
      <div className="inline-note" style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <span className={`chip status-${rState}`}>{rState}</span>
        {statusValues?.profile_name && (
          <span className="chip">{statusValues.profile_name} · {statusValues.model}</span>
        )}
        {statusValues?.error && <span style={{ color: "var(--danger)", fontSize: 12.5 }}>{statusValues.error}</span>}
        <ActivityChip activity={statusValues?.activity} lastActivity={statusValues?.last_activity} running={busy} />
      </div>

      {(progress?.chapters?.length ?? 0) > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>Brief progress</strong>
            <span className="chip">
              {doneCount}/{progress!.chapters.length} Kapitelbriefe fertig
            </span>
          </div>
          <div className="chapter-list-item" style={{ fontWeight: "normal" }}>
            <span className="muted" style={{ width: 26 }}></span>
            <span style={{ flex: 1 }}></span>
            <span className="muted" style={{ fontSize: 11.5 }}>Wörter</span>
            <span className="muted" style={{ fontSize: 11.5 }}>Version</span>
          </div>
          {progress!.chapters.map((chapter) => (
            <div key={chapter.chapter_id} className="chapter-list-item">
              <span style={{ width: 26, fontSize: 13, textAlign: "center" }}>
                {chapter.done ? "✔" : chapter.started ? "…" : "—"}
              </span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {chapter.chapter_name}
              </span>
              <span className="muted" style={{ fontSize: 11.5, width: 70, textAlign: "right" }}>
                {chapter.brief_words ? `${chapter.brief_words.toLocaleString()} w` : ""}
              </span>
              <span className="muted" style={{ fontSize: 11.5, width: 60, textAlign: "right" }}>
                {chapter.active_version ? `v${String(chapter.active_version).padStart(4, "0")}` : "—"}
              </span>
            </div>
          ))}
        </div>
      )}

      {versionChapters.length > 0 && (
        <div style={{ marginTop: 12, borderTop: "1px solid rgba(128,140,160,0.25)", paddingTop: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>Kapitelbrief-Versionen</strong>
            <button onClick={() => download("outline_refined/book_outline_refined.md")} title="Aktive Versionen als eine Datei herunterladen">⤓ Alle (aktiv)</button>
          </div>
          {versionChapters.map((chapter) => {
            const active = chapter.active_version;
            return (
              <div key={chapter.chapter_id} style={{ borderBottom: "1px solid rgba(128,140,160,0.18)", padding: "6px 0" }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
                  <span style={{ minWidth: 26, fontSize: 12, color: "var(--muted)" }}>
                    {String(chapter.chapter_id).padStart(2, "0")}
                  </span>
                  <strong style={{ flex: 1 }}>{chapter.chapter_name}</strong>
                  <span className="muted" style={{ fontSize: 11.5 }}>
                    original {chapter.original_words} w · active {active ? `v${String(active).padStart(4, "0")}` : "—"}
                  </span>
                </div>
                <div style={{ marginTop: 4, display: "flex", flexWrap: "wrap", gap: 4 }}>
                  {chapter.versions.slice().reverse().map((v: any) => (
                    <span key={v.version} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      <button
                        style={{ fontSize: 11, padding: "2px 6px", fontWeight: active === v.version ? 700 : 400 }}
                        onClick={() => viewVersion(chapter.chapter_id, chapter.chapter_name, v.version)}
                        title={v.created ? new Date(v.created).toLocaleString() : ""}
                      >
                        v{String(v.version).padStart(4, "0")} · {v.words} w{active === v.version ? " — aktiv" : ""}
                      </button>
                      {active !== v.version && (
                        <button style={{ fontSize: 11, padding: "2px 6px" }} onClick={() => selectVersion(chapter.chapter_id, v.version)} title="Als aktive Fassung setzen (Writer liest diese dann)">
                          aktiv
                        </button>
                      )}
                    </span>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div style={{ marginTop: 12, borderTop: "1px solid rgba(128,140,160,0.25)", paddingTop: 10 }}>
        {versionView ? (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <strong style={{ fontSize: 13 }}>
                {versionView.chapter} — v{String(versionView.version).padStart(4, "0")}{versionView.created ? ` · ${new Date(versionView.created).toLocaleString()}` : ""}
              </strong>
              <div style={{ display: "flex", gap: 6 }}>
                <button onClick={() => download(`outline_refined/ch${String(versionView.chapterId).padStart(4, "0")}/v${String(versionView.version).padStart(4, "0")}.json`)}>⤓</button>
                <button onClick={() => setVersionView(null)}>Close</button>
              </div>
            </div>
            <pre className="prose-view" style={{ whiteSpace: "pre-wrap", maxHeight: 420, overflow: "auto" }}>
              {versionView.content}
            </pre>
          </div>
        ) : (
          <>
            <strong style={{ fontSize: 13 }}>Refine log</strong>
            <div ref={feedRef} className="event-feed" style={{ height: 240 }}>
              {feed.map((line: any) => (
                <div key={line.key}>
                  <span className="ev-time">{line.time}</span>
                  <span dangerouslySetInnerHTML={{ __html: line.html }} />
                </div>
              ))}
              {feed.length === 0 && <span className="muted">no events yet…</span>}
            </div>
            {outputs.length > 0 && (
              <div style={{ marginTop: 8 }}>
                <div className="muted" style={{ fontSize: 12 }}>Outputs ({outputs.length})</div>
                {outputs.slice(-6).reverse().map((file) => (
                  <div key={file.path} className="chapter-list-item" style={{ fontSize: 11.5 }}>
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{file.path}</span>
                    <span className="muted">{(file.size / 1024).toFixed(1)} KB</span>
                    <button style={{ fontSize: 11, padding: "2px 6px" }} onClick={() => download(file.path)}>⤓</button>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {outlineChapters.length > 0 && (
        <div style={{ marginTop: 12, borderTop: "1px solid rgba(128,140,160,0.25)", paddingTop: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>
              Kapitel wählen{" "}
              <span className="chip" style={{ color: chapterSelection ? "var(--warn)" : "var(--good)" }}>
                {chapterSelection ? `${chapterSelection.size} von ${outlineChapters.length} gewählt` : "alle"}
              </span>
            </strong>
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={() => setChapterSelection(null)}>All</button>
              <button onClick={() => setChapterSelection(new Set())}>None</button>
            </div>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 6 }}>
            {outlineChapters.map((chapter: any) => {
              const cid = Number(chapter.id);
              const hasDesc = String(chapter.description ?? "").trim().length > 0;
              return (
                <label key={cid} className="chip" style={{ cursor: hasDesc ? "pointer" : "not-allowed", opacity: hasDesc ? 1 : 0.4 }}>
                  <input
                    type="checkbox"
                    checked={chapterSelection === null || chapterSelection.has(cid)}
                    onChange={() => {
                      const next = new Set(chapterSelection ?? outlineChapters.map((c: any) => Number(c.id)));
                      if (next.has(cid)) next.delete(cid);
                      else next.add(cid);
                      setChapterSelection(next.size === outlineChapters.length ? null : next);
                    }}
                    disabled={busy || !hasDesc}
                  />{" "}
                  {String(cid + 1).padStart(2, "0")} {chapter.name}
                </label>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}