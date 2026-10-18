import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Book, ChapterRecord, Profile, TranslationStatus } from "../types";
import ReviewCard from "./ReviewCard";
import ActivityChip from "./ActivityChip";

const JUDGE_WARNING = "Use several candidate models and a separate judge model: every block is translated by each candidate, the judge picks the winning translation. Local Ollama candidates are unloaded right after each call (keep_alive: 0) so no model clogs VRAM.";

const AUTHORIAL_WARNING = "Authorial mode rewrites instead of transcribing: a draft model renders the block freely in the target language, the strongest model refines it up to N rounds (stops early when text barely changes), then findings are only flagged for a human decision — deliberate deviations are reported and never auto-corrected. Flags and notes land in review_list.md.";

function formatTranslationEvent(event: any): { time: string; html: string } {
  const time = event.sent_at ? new Date(event.sent_at * 1000).toLocaleTimeString() : "";
  const safe = (text: string) => String(text).replace(/[<>&]/g, "");
  if (event.type === "translation_started") {
    const sourceNote = event.source === "refined" ? ` · Quelle: <b>refined</b>` : "";
    if (event.mode === "authorial")
      return {
        time,
        html: `Translation started — <b>${safe(event.language)}</b> · mode <b>authorial</b> · draft <b>${safe(event.draft ?? "")}</b> · refiner <b>${safe(event.refiner ?? "")}</b> · checker <b>${safe(event.checker ?? "— (length watchdog only)")}</b>${sourceNote}`,
      };
    return {
      time,
      html: `Translation started — <b>${safe(event.language)}</b> · mode <b>${safe(event.mode ?? "tool")}</b>${
        event.candidates?.length ? ` · candidates: ${event.candidates.map((c: string) => safe(c)).join(", ")} · judge: ${safe(event.judge ?? "—")}` : ` · <b>${safe(event.profile_name ?? "")}</b> (${safe(event.model ?? "")})`
      }${event.test_num > 0 ? ` · test mode: first ${event.test_num} lines` : ""}${sourceNote}`,
    };
  }
  if (event.type === "translation_batch") {
    if (event.error) return { time, html: `<span class="ev-fail">✘ block ${event.batch_index}/${event.batches_total} failed: ${safe(event.error)}</span>` };
    if (event.note) return { time, html: safe(event.note) };
    if (event.stage) {
      const stage = ({ draft: "draft", refine: "refined", check: "checked" } as Record<string, string>)[event.stage] ?? event.stage;
      const extras = [
        event.notes ? `${event.notes} notes` : "",
        event.flags ? `<span style="color: var(--warn)">⚠ ${event.flags} flag(s)</span>` : "",
      ].filter(Boolean).join(" · ");
      return {
        time,
        html: `<span class="ev-ok">✔ block ${event.batch_index}/${event.batches_total} (chapter ${event.chapter_id ?? "?"})</span> — ${safe(stage)}${extras ? ` · ${extras}` : ""}`,
      };
    }
    return {
      time,
      html: `<span class="ev-ok">✔ block ${event.batch_index}/${event.batches_total} (chapter ${event.chapter_id ?? "?"})</span> — winner <b>${safe(event.winner ?? "")}</b>${event.reason ? `: ${safe(event.reason)}` : ""}`,
    };
  }
  if (event.type === "translate_progress")
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
  if (event.type === "translation_output") return { time, html: safe((event.lines ?? []).slice(-2).join(" \u00b7 ")) };
  if (event.type === "translation_done") {
    if (event.status === "done")
      return {
        time,
        html: `<span class="ev-ok">✔ Translation complete</span> — saved <code>${safe(event.output_file ?? "")}</code>${
          event.gaps ? ` <span style="color: var(--warn)">(⚠ ${event.gaps} blocks failed)</span>` : ""
        }${
          event.review_flags !== undefined
            ? ` · review: ${event.review_flags} flag(s), ${event.annotations} note(s)${event.review_file ? ` — <code>${safe(event.review_file)}</code>` : ""}`
            : ""
        }`,
      };
    return { time, html: `<span style="color: var(--warn)">⚠ Translation interrupted — partial output kept (resume possible)</span>` };
  }
  if (event.type === "translation_error")
    return { time, html: `<span class="ev-fail">✘ Translation error: ${safe(event.error ?? "")}</span>` };
  return { time, html: safe(event.type ?? "") };
}

const TRANSLATION_EVENTS = ["translation_started", "translation_batch", "translate_progress", "translation_output", "translation_done", "translation_error", "llm_call"];

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
  const saved = book.translation_settings ?? {};
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileId, setProfileId] = useState<string>(saved.profile_id ?? book.writer_profile_id ?? "");
  const [mode, setMode] = useState<"tool" | "ensemble" | "authorial">((saved.mode as any) ?? "tool");
  const [candidates, setCandidates] = useState<Set<string>>(new Set(saved.candidate_profile_ids ?? []));
  const [judgeId, setJudgeId] = useState<string>(saved.judge_profile_id ?? "");
  const [draftId, setDraftId] = useState<string>(saved.draft_profile_id ?? "");
  const [refinerId, setRefinerId] = useState<string>(saved.refiner_profile_id ?? "");
  const [checkerId, setCheckerId] = useState<string>(saved.checker_profile_id ?? "");
  const [refineRounds, setRefineRounds] = useState<number>(saved.refine_rounds ?? 2);
  const [draftTemp, setDraftTemp] = useState<number>(saved.draft_temperature ?? 0.7);
  const [similarityStop, setSimilarityStop] = useState<number>(saved.similarity_stop ?? 0.97);
  const [ratioMin, setRatioMin] = useState<number>(saved.ratio_min ?? 0.85);
  const [ratioMax, setRatioMax] = useState<number>(saved.ratio_max ?? 1.45);
  const [batchWords, setBatchWords] = useState<number>(saved.batch_words ?? 0);
  const [checkMode, setCheckMode] = useState<string>(saved.check_mode ?? "essential");
  const [styleProbe, setStyleProbe] = useState<string>(saved.style_probe ?? "");
  const [unloadAfterCall, setUnloadAfterCall] = useState<boolean>(saved.unload_after_call ?? true);
  const [useRefined, setUseRefined] = useState<boolean>(saved.use_refined ?? false);
  const [targetLanguage, setTargetLanguage] = useState(saved.target_language ?? "German");
  const [chapters, setChapters] = useState<ChapterRecord[]>([]);
  const [chapterSelection, setChapterSelection] = useState<Set<number> | null>(null);
  const [singleTranslate, setSingleTranslate] = useState<boolean>(saved.single_translate ?? false);
  const [testMode, setTestMode] = useState<boolean>(saved.test_mode ?? false);
  const [testNum, setTestNum] = useState<number>(saved.test_num ?? 10);
  const [batchSize, setBatchSize] = useState<number>(saved.batch_size ?? 10);
  const [resume, setResume] = useState(false);
  const [tStatus, setTStatus] = useState<Record<string, any> | null>(book.translation ?? null);
  const [outputs, setOutputs] = useState<{ path: string; size: number; modified: number }[]>([]);
  const [viewContent, setViewContent] = useState<{ path: string; content: string } | null>(null);
  const [liveGerman, setLiveGerman] = useState<string | null>(null);
  const [chapterProgress, setChapterProgress] = useState<{
    chapters: {
      chapter_id: number;
      chapter_name: string;
      words_total: number;
      translated_words: number;
      batches_total: number;
      batches_done: number;
      paragraphs: number;
      review_flags: number;
      started: boolean;
      done: boolean;
    }[];
  } | null>(null);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setTStatus(book.translation ?? null);
  }, [book.translation?.status]);

  useEffect(() => {
    api.listProfiles().then((r) => {
      setProfiles(r.profiles);
      setProfileId((prev) => prev || (book.writer_profile_id ?? "") || r.profiles[0]?.id || "");
      setJudgeId((prev) => prev || (book.writer_profile_id ?? "") || r.profiles[0]?.id || "");
      setDraftId((prev) => prev || (book.writer_profile_id ?? "") || r.profiles[0]?.id || "");
      setRefinerId((prev) => prev || r.profiles[1]?.id || r.profiles[0]?.id || "");
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
    api.translateProgress(book.id).then(setChapterProgress).catch(() => {});
    api.getChapters(book.id).then((r) => setChapters(r.chapters)).catch(() => {});
    const timer = window.setInterval(() => {
      refreshStatus();
      refreshOutputs();
      api.translateProgress(book.id).then(setChapterProgress).catch(() => {});
    }, 3000);
    return () => window.clearInterval(timer);
  }, [book.id]);

  useEffect(() => {
    if (tStatus?.status === "done" || tStatus?.status === "stopped" || tStatus?.status === "error") refreshOutputs();
  }, [tStatus?.status]);

  const translationSettings = useMemo(
    () => ({
      mode,
      target_language: targetLanguage,
      profile_id: profileId,
      candidate_profile_ids: Array.from(candidates),
      judge_profile_id: judgeId,
      draft_profile_id: draftId,
      refiner_profile_id: refinerId,
      checker_profile_id: checkerId,
      unload_after_call: unloadAfterCall,
      use_refined: useRefined,
      single_translate: singleTranslate,
      test_mode: testMode,
      test_num: testNum,
      batch_size: batchSize,
      batch_words: batchWords,
      check_mode: checkMode,
      draft_temperature: draftTemp,
      refine_rounds: refineRounds,
      similarity_stop: similarityStop,
      ratio_min: ratioMin,
      ratio_max: ratioMax,
      style_probe: styleProbe,
    }),
    [mode, targetLanguage, profileId, candidates, judgeId, draftId, refinerId, checkerId, unloadAfterCall, useRefined, singleTranslate, testMode, testNum, batchSize, batchWords, checkMode, refineRounds, similarityStop, ratioMin, ratioMax, styleProbe, draftTemp],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.patchBook(book.id, { translation_settings: translationSettings }).catch(() => {});
    }, 800);
    return () => window.clearTimeout(timer);
  }, [book.id, JSON.stringify(translationSettings)]);

  const germanPath = useMemo(
    () => outputs.find((o) => o.path.endsWith("book_german.txt"))?.path ?? null,
    [outputs],
  );

  useEffect(() => {
    setLiveGerman(null);
    if (!germanPath) return;
    let cancelled = false;
    const load = async () => {
      try {
        const next = await api.translateFile(book.id, germanPath);
        if (!cancelled) setLiveGerman(next.content);
      } catch {
        if (!cancelled) setLiveGerman("");
      }
    };
    load();
    const timer = window.setInterval(load, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [germanPath, book.id]);

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [events.length]);

  useEffect(() => {
    if (!viewContent) return;
    const path = viewContent.path;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.translateFile(book.id, path);
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

  const openReviewFlags = useMemo(() => {
    const fromProgress = (chapterProgress?.chapters ?? []).reduce(
      (sum, ch) => sum + (ch.review_flags ?? 0),
      0,
    );
    return fromProgress;
  }, [chapterProgress]);

  const start = async (resumeRun: boolean, overrides: { chapterIds?: number[] | null } = {}) => {
    if (!(book.chapters_written ?? 0)) {
      showToast("Nothing written yet — write at least one chapter first");
      return;
    }
    if (mode === "tool" && !profileId) {
      showToast("Select an LLM profile (same profiles as book writing)");
      return;
    }
    if (mode === "authorial" && !draftId) {
      showToast("Select a draft profile (the best target-language writer)");
      return;
    }
    if (mode === "authorial" && !refinerId) {
      showToast("Select a refiner profile (the strongest available model)");
      return;
    }
    try {
      setResume(resumeRun);
      const chapterOverride = "chapterIds" in overrides ? overrides.chapterIds : undefined;
      const chapterSource =
        chapterOverride === undefined
          ? (chapterSelection !== null ? Array.from(chapterSelection) : null)
          : chapterOverride;
      const payload: any = {
        target_language: targetLanguage,
        mode,
        profile_id: profileId,
        chapter_ids: chapterSource,
        single_translate: singleTranslate,
        test_num: testMode ? testNum : 0,
        batch_size: batchSize,
        resume: resumeRun,
        use_refined: useRefined,
      };
      if (mode === "ensemble") {
        payload.candidate_profile_ids = Array.from(candidates);
        payload.judge_profile_id = judgeId || null;
        payload.unload_after_call = unloadAfterCall;
      } else if (mode === "authorial") {
        payload.draft_profile_id = draftId;
        payload.refiner_profile_id = refinerId;
        payload.checker_profile_id = checkerId || null;
        payload.unload_after_call = unloadAfterCall;
        payload.refine_rounds = refineRounds;
        payload.similarity_stop = similarityStop;
        payload.ratio_min = ratioMin;
        payload.ratio_max = ratioMax;
        payload.batch_words = batchWords || 0;
        payload.check_mode = checkMode === "strict" ? "strict" : "essential";
        payload.draft_temperature = draftTemp;
        payload.style_probe = styleProbe;
        payload.candidate_profile_ids = [];
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

  // wipes one chapter's translated batches so a resume run re-does exactly
  // this chapter with the CURRENT system (e.g. the structured tool protocol) —
  // then starts the whole book WITHOUT any chapter filter (a stray selection
  // must not silently exclude the chapter we just reset)
  const resetChapter = async (chapterId: number) => {
    if (busy) return;
    if (!window.confirm("Kapitel-Übersetzung zurücksetzen und mit dem aktuellen System neu übersetzen? Die bisherigen Entscheidungen/Notizen dieses Kapitels gehen verloren — der Rest des Buches bleibt.")) {
      return;
    }
    try {
      const result = await api.translateResetChapter(book.id, chapterId);
      showToast(`Kapitel zurückgesetzt (${result.reset_batches} Blöcke) — übersetze Buch weiter`);
      await refreshBook();
      await start(true, { chapterIds: null });
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
          with several models and a judge model picks the winner. Mode 3 rewrites: draft in the
          target language, strongest-model refinement, then flags for human review — good prose
          even when the English source is weak. All run beside the writing run — chapters and
          checkpoints stay untouched.
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="form-col" style={{ minWidth: 240 }}>
            <label>Mode</label>
            <select value={mode} onChange={(e) => setMode(e.target.value as any)} disabled={busy}>
              <option value="tool">Single model (bilingual_book_maker)</option>
              <option value="ensemble">Model ensemble + judge</option>
              <option value="authorial">Authorial rewrite (draft → refine → check)</option>
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
          <div className="form-col" style={{ minWidth: 250 }}>
            <label>
              <input
                type="checkbox"
                checked={useRefined}
                onChange={(e) => setUseRefined(e.target.checked)}
                disabled={busy}
              />{" "}
              Quelle: verfeinerte Fassung (refined/)
            </label>
            <div className="muted" style={{ fontSize: 11.5 }}>
              Übersetzt die AKTIVEN Refined-Versionen. Kapitel ohne Refined-Fassung fallen aufs
              Original zurück — das wird im Lauf-Log sichtbar gemeldet. Gibt es nirgends eine
              Refined-Fassung, startet die Übersetzung gar nicht.
            </div>
          </div>
          {mode !== "authorial" ? (
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
          ) : null}
          {mode !== "authorial" ? (
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
          ) : null}
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
            {!busy && (
              <button
                className="btn-primary"
                onClick={() => start(true)}
                title="Setzt denlauf fort: fertige Blöcke bleiben, angefangene/unfertige laufen weiter (nach einem Server-Neustart steht der Läufer auf idle — der Resume findet trotzdem die gespeicherten Blöcke)"
              >
                ⟳ Resume last run
              </button>
            )}
            {!busy && (
              <button
                className="btn-danger"
                onClick={async () => {
                  if (!window.confirm("ALLES löschen — komplette Übersetzung (alle Kapitel, alle Reports, alle Notizen) für die Sprache? Danach mit ▶ Start von Null neu.")) return;
                  try {
                    const r = await api.translateResetAll(book.id);
                    showToast(`Gelöscht: ${r.removed.join(", ") || "nichts vorhanden"}`);
                    refreshStatus();
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
        {mode === "authorial" && (
          <div style={{ marginTop: 12, borderTop: "1px solid rgba(128,140,160,0.25)", paddingTop: 12 }}>
            <div className="inline-note" style={{ marginTop: 0 }}>{AUTHORIAL_WARNING}</div>
            <div className="inline-note">
              The finished German streams into <b>Live output</b> above, block by block and stage by stage.
              Flag reasons land in the event feed below and in <code>review_list.md</code>. All settings on
              this tab are stored per book.
            </div>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
              <div className="form-col" style={{ minWidth: 250 }}>
                <label>Draft profile (best target-language writer, temp 0.7)</label>
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
                <label>Draft temperature (0.3 was best in test)</label>
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
                  onChange={(e) => setRatioMin(Number(e.target.value) || 0.85)}
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
                  onChange={(e) => setRatioMax(Number(e.target.value) || 1.45)}
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
            <div style={{ marginTop: 10 }}>
              <div className="form-col" style={{ minWidth: 280 }}>
                <label>Review checks</label>
                <select value={checkMode} onChange={(e) => setCheckMode(e.target.value)} disabled={busy}>
                  <option value="essential">Nur echte Fehler (Standard — keine Scores)</option>
                  <option value="strict">Streng: Längen-/Treue-Prüfung flaggen</option>
                </select>
                <div className="muted" style={{ fontSize: 11.5 }}>
                  „Nur echte Fehler” flaggt ausschließlich kaputte Ausgaben (z. B. fehlender Text). Der
                  Treue-Checker wird dann nicht mehr gerufen — Läufe sind schneller.
                </div>
              </div>
            </div>
            <div style={{ marginTop: 10 }}>
              <div className="form-col" style={{ maxWidth: 640 }}>
                <label>Style probe (target tone as an example, optional — form is free, content is not copied)</label>
                <textarea
                  rows={3}
                  value={styleProbe}
                  onChange={(e) => setStyleProbe(e.target.value)}
                  disabled={busy}
                  placeholder="Paste a paragraph whose German tone you want as the target tone…"
                />
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
          {statusValues?.source_mode === "refined" && (
            <span className="chip" title="Dieser Lauf liest die aktiven Refined-Versionen der abgeschlossenen Kapitel">
              Quelle: refined ({(statusValues.refined_used ?? []).length})
            </span>
          )}
          {(statusValues?.refined_fallback?.length ?? 0) > 0 && (
            <span className="chip" style={{ color: "var(--warn)", borderColor: "var(--warn)" }} title="Kapitel ohne Refined-Fassung — diese werden vom Original übersetzt">
              Original-Fallback: {(statusValues?.refined_fallback ?? []).slice(0, 6).map((c: number) => c + 1).join(", ")}
              {(statusValues?.refined_fallback ?? []).length > 6 ? ", …" : ""}
            </span>
          )}
        {statusValues?.error && <span style={{ color: "var(--danger)", fontSize: 12.5 }}>{statusValues.error}</span>}
        <ActivityChip
          activity={statusValues?.activity}
          lastActivity={statusValues?.last_activity}
          running={busy}
        />
      </div>
      </div>

      {(chapterProgress?.chapters?.length ?? 0) > 0 && (
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>Chapter progress</strong>
            {openReviewFlags > 0 && (
              <span className="chip" style={{ color: "var(--warn)", borderColor: "var(--warn)" }}>
                {openReviewFlags} open flags → Review
              </span>
            )}
          </div>
          <div className="chapter-list-item" style={{ fontWeight: "normal" }}>
            <span className="muted" style={{ width: 26 }}></span>
            <span style={{ flex: 1 }}></span>
            <span className="muted" style={{ fontSize: 11.5 }}>blocks</span>
            <span className="muted" style={{ fontSize: 11.5 }}>¶</span>
            <span className="muted" style={{ fontSize: 11.5 }}>flags</span>
          </div>
          {chapterProgress!.chapters.map((chapter) => {
            const percent =
              chapter.words_total > 0
                ? Math.min(100, Math.round((chapter.translated_words / chapter.words_total) * 100))
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
                <button
                  title="Kapitel-Übersetzung zurücksetzen und mit dem aktuellen System (strukturierter Output) neu übersetzen — nur dieses Kapitel, der Rest bleibt"
                  disabled={busy}
                  onClick={() => resetChapter(chapter.chapter_id)}
                  style={{ fontSize: 11.5, padding: "2px 8px" }}
                >
                  ⟳ neu
                </button>
              </div>
            );
          })}
        </div>
      )}

      <ReviewCard book={book} busyTranslation={busy} refreshBook={refreshBook} showToast={showToast} />

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
          <strong>Live output — German grows paragraph by paragraph</strong>
          <span className="muted" style={{ fontSize: 12 }}>updates automatically every 2.5 s</span>
        </div>
        {liveGerman ? (
          <pre className="prose-view" style={{ whiteSpace: "pre-wrap", maxHeight: 420, overflow: "auto" }}>
            {liveGerman}
          </pre>
        ) : (
          <div className="muted" style={{ padding: 10 }}>
            {liveGerman === null
              ? "Loading…"
              : "Nothing yet — after the first draft block of the run, the German text appears here paragraph by paragraph."}
          </div>
        )}
      </div>

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <strong>
            Chapters to translate{" "}
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