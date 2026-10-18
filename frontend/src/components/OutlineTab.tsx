import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Book, OutlineChapter } from "../types";
import LiveStreamBox from "./LiveStreamBox";
import OutlineRefineCard from "./OutlineRefineCard";

export default function OutlineTab({
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
  const outline = book.outline;
  const frozen = outline?.frozen ?? false;
  const busy = ["generating_outline", "writing", "waiting_review"].includes(book.runner?.status ?? "");
  const hasStream = events.some((e) => e.type === "llm_delta");

  const [plannerProfileId, setPlannerProfileId] = useState<string>(book.planner_profile_id ?? "");
  const [profiles, setProfiles] = useState<{ id: string; name: string; model: string }[]>([]);

  const [premise, setPremise] = useState(outline?.premise ?? "");
  const [synopsis, setSynopsis] = useState(outline?.synopsis ?? "");
  const [acts, setActs] = useState(outline?.acts ?? "");
  const [chapters, setChapters] = useState<OutlineChapter[]>(outline?.chapters ?? []);
  const [dirtyChapters, setDirtyChapters] = useState(false);

  useEffect(() => {
    setPremise(outline?.premise ?? "");
    setSynopsis(outline?.synopsis ?? "");
    setActs(outline?.acts ?? "");
    setChapters(outline?.chapters ?? []);
    setDirtyChapters(false);
  }, [outline?.chapters?.length, outline?.frozen, book.id]);

  useEffect(() => {
    api
      .listProfiles()
      .then((r) => setProfiles(r.profiles))
      .catch(() => {});
  }, []);

  const stageInfo = (): string | null => {
    const planning = events.filter((e) => e.type?.startsWith("planner_stage"));
    const last = planning[planning.length - 1];
    if (!last) return null;
    if (last.type === "planner_stage_started") return `Planning: ${last.stage}…`;
    return `Finished: ${last.stage}`;
  };

  const totalWords = useMemo(
    () => chapters.reduce((sum, c) => sum + (Number(c.word_count) || 0), 0),
    [chapters],
  );

  const generate = async (stage?: string) => {
    if (!plannerProfileId) {
      showToast("Select a planner LLM profile first (tab: LLM Profiles)");
      setPlannerProfileId(profiles[0]?.id ?? "");
      return;
    }
    try {
      if (stage) {
        await api.regenerateStage(book.id, stage, plannerProfileId);
      } else {
        await api.generateOutline(book.id, plannerProfileId);
      }
      showToast(stage ? `Regenerating ${stage}…` : "Generating outline…");
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const abortGeneration = async () => {
    if (!window.confirm("Abort the outline generation now? The in-flight LLM call is cut; already completed stages are kept.")) return;
    try {
      await api.runAbort(book.id);
      showToast("Outline generation aborted — completed stages kept, change the profile and generate again");
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const saveOutlineText = async (key: "premise" | "synopsis" | "acts", value: string) => {
    await api.patchOutline(book.id, { [key]: value });
    showToast(`${key} saved`);
    refreshBook();
  };

  const saveChapters = async () => {
    await api.patchOutline(book.id, { chapters });
    setDirtyChapters(false);
    showToast("Chapters saved");
    refreshBook();
  };

  const freeze = async () => {
    if (!chapters.length) {
      showToast("Generate a chapter outline first");
      return;
    }
    try {
      await api.freezeOutline(book.id);
      showToast("Outline frozen — ready to write");
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const unfreeze = async () => {
    await api.patchOutline(book.id, { frozen: false });
    showToast("Outline unfrozen for editing");
    refreshBook();
  };

  const updateChapter = (index: number, patch: Partial<OutlineChapter>) => {
    setChapters((prev) => prev.map((c, i) => (i === index ? { ...c, ...patch } : c)));
    setDirtyChapters(true);
  };

  const hasDetailed = book.target_words >= 10000;

  return (
    <div>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <strong>Planner</strong>
          <span className="muted" style={{ fontSize: 12 }}>
            4 stages: premise → synopsis → acts → chapters (synopsis/acts only for ≥10K words)
          </span>
        </div>
        <div className="form-row" style={{ alignItems: "center" }}>
          <div className="form-col">
            <label>Planner LLM profile</label>
            <select value={plannerProfileId} onChange={(e) => setPlannerProfileId(e.target.value)}>
              <option value="">— select profile —</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.model})
                </option>
              ))}
            </select>
          </div>
          {!frozen && (
            <>
              <div className="form-col" style={{ justifyContent: "flex-end" }}>
                <button className="btn-primary" disabled={busy} onClick={() => generate()}>
                  {outline ? "Generate missing outline parts" : "Generate outline"}
                </button>
              </div>
              {hasDetailed && outline?.premise && (
                <div className="form-col" style={{ justifyContent: "flex-end" }}>
                  <button className="btn-ghost" disabled={busy} onClick={() => generate("synopsis")}>
                    Regenerate synopsis+acts
                  </button>
                </div>
              )}
              {outline?.premise && (
                <div className="form-col" style={{ justifyContent: "flex-end" }}>
                  <button className="btn-ghost" disabled={busy} onClick={() => generate("premise")}>
                    Regenerate all
                  </button>
                </div>
              )}
            </>
          )}
        </div>
        {stageInfo() && <div className="inline-note">{stageInfo()}</div>}
        {book.runner?.status === "generating_outline" && (
          <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
            <button className="btn-danger" onClick={abortGeneration}>
              ◼ Abort generation
            </button>
            <span className="muted" style={{ fontSize: 12 }}>
              stops the in-flight LLM call immediately; finished stages stay saved — switch the profile and rerun
            </span>
          </div>
        )}
        {(book.runner?.status === "generating_outline" || hasStream) && (
          <div style={{ marginTop: 10 }}>
            <LiveStreamBox events={events} active={book.runner?.status === "generating_outline"} height={200} />
          </div>
        )}
        {book.runner?.status === "generating_outline" && (
          <div className="inline-note">Outline generation is running — live progress in the Write tab's event feed or SSE console.</div>
        )}
      </div>

      {!outline && (
        <div className="card muted">
          No outline yet. Select a planner profile above and generate the outline. The outline is a
          <em> frozen plan</em>: the writing agent receives all of it every chapter, so review it carefully before freezing.
        </div>
      )}

      {outline && (
        <>
          <div className="card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <strong>Premise</strong>
              {!frozen && premise !== outline?.premise && (
                <button disabled={busy} onClick={() => saveOutlineText("premise", premise)}>
                  Save premise
                </button>
              )}
            </div>
            <textarea
              rows={4}
              readOnly={frozen}
              value={premise}
              onChange={(e) => setPremise(e.target.value)}
              style={{ marginTop: 8 }}
            />
          </div>

          {hasDetailed && (outline?.synopsis || synopsis) && (
            <div className="card">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <strong>Detailed synopsis</strong>
                {!frozen && synopsis !== outline?.synopsis && (
                  <button disabled={busy} onClick={() => saveOutlineText("synopsis", synopsis)}>
                    Save synopsis
                  </button>
                )}
              </div>
              <textarea rows={6} readOnly={frozen} value={synopsis} onChange={(e) => setSynopsis(e.target.value)} style={{ marginTop: 8 }} />
            </div>
          )}

          {hasDetailed && acts && (
            <div className="card">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <strong>Acts</strong>
                {!frozen && acts !== outline?.acts && (
                  <button disabled={busy} onClick={() => saveOutlineText("acts", acts)}>
                    Save acts
                  </button>
                )}
              </div>
              <textarea rows={6} readOnly={frozen} value={acts} onChange={(e) => setActs(e.target.value)} style={{ marginTop: 8 }} />
            </div>
          )}

          <div className="card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <strong>
                Chapter outline ({chapters.length} chapters · {totalWords.toLocaleString()} words targeted)
              </strong>
              <div style={{ display: "flex", gap: 8 }}>
                {dirtyChapters && !frozen && <button onClick={saveChapters}>Save chapters</button>}
                {frozen ? (
                  <button className="btn-ghost" onClick={unfreeze}>
                    Unfreeze to edit
                  </button>
                ) : (
                  <button className="btn-primary btn-good" onClick={freeze}>
                    Freeze outline
                  </button>
                )}
              </div>
            </div>

            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ color: "var(--text-dim)", fontSize: 12, textAlign: "left" }}>
                  <th style={{ width: 46, padding: 6 }}>#</th>
                  <th style={{ width: 220, padding: 6 }}>Name</th>
                  <th style={{ padding: 6 }}>Description</th>
                  <th style={{ width: 110, padding: 6 }}>Target words</th>
                </tr>
              </thead>
              <tbody>
                {chapters.map((chapter, index) => (
                  <tr key={index} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: 6 }} className="muted">
                      {chapter.id}
                    </td>
                    <td style={{ padding: 6 }}>
                      <input
                        value={chapter.name}
                        readOnly={frozen}
                        onChange={(e) => updateChapter(index, { name: e.target.value })}
                        style={{ width: "100%" }}
                      />
                    </td>
                    <td style={{ padding: 6 }}>
                      <textarea
                        rows={2}
                        value={chapter.description}
                        readOnly={frozen}
                        onChange={(e) => updateChapter(index, { description: e.target.value })}
                      />
                    </td>
                    <td style={{ padding: 6 }}>
                      <input
                        type="number"
                        value={chapter.word_count}
                        readOnly={frozen}
                        onChange={(e) => updateChapter(index, { word_count: Number(e.target.value) })}
                        style={{ width: 90 }}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!frozen && (
              <div className="inline-note">
                Edits change the plan the agent sees in every chapter. After freezing, the writing runner uses
                exactly this outline (with word targets scaled to the book target).
              </div>
            )}
          </div>

          <OutlineRefineCard book={book} events={events} refreshBook={refreshBook} showToast={showToast} />
        </>
      )}
    </div>
  );
}