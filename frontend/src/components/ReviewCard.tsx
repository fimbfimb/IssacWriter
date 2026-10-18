import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Book, ReviewResponse, ReviewRow } from "../types";

const BADGE_STYLE: Record<string, string> = {
  length: "var(--warn)",
  refiner: "var(--bad)",
  fidelity: "var(--bad)",
  check: "var(--warn)",
  structure: "var(--accent)",
  annotation: "var(--muted)",
};

export default function ReviewCard({
  book,
  busyTranslation,
  refreshBook,
  showToast,
}: {
  book: Book;
  busyTranslation: boolean;
  refreshBook: () => Promise<void>;
  showToast: (m: string) => void;
}) {
  const [review, setReview] = useState<ReviewResponse | null>(null);
  const [openFlagRowId, setOpenFlagRowId] = useState<string | null>(null);
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [working, setWorking] = useState(false);
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [annotationsOpen, setAnnotationsOpen] = useState(false);
  const [openAnnotationChapters, setOpenAnnotationChapters] = useState<Set<number>>(new Set());
  const [editRowId, setEditRowId] = useState<string | null>(null);
  const [editDrafts, setEditDrafts] = useState<Record<string, string>>({});

  const refresh = () =>
    api
      .translateReview(book.id)
      .then((r) => setReview(r))
      .catch((e) => showToast(e.message));

  useEffect(() => {
    refresh();
    // poll while a translation runs: reset/retranslate/resume land new flags in
    // the report continuously, so the card must update itself without a click
    if (!busyTranslation) return;
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, [book.id, busyTranslation]);

  const setRead = async (ids: string[]) => {
    setWorking(true);
    try {
      const result = await api.translateAnnotationsRead(book.id, ids);
      setReview(result.review);
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setWorking(false);
    }
  };

  // direct in-place German edit right below the annotation — writes into all
  // outputs (manuscript.md, book_german.txt, bilingual) and marks the note read
  const saveFix = async (
    row: ReviewRow,
    paragraph: number,
    text: string,
  ) => {
    setWorking(true);
    try {
      const result = await api.translateFixParagraph(book.id, {
        batch_index: row.batch_index,
        paragraph,
        text,
      });
      setReview(result.review);
      await refreshBook();
      showToast("Text übernommen — alle Ausgaben neu geschrieben");
      if (row.decision === null || row.decision?.action !== "fixed") {
        await setRead([row.id]);
      }
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setWorking(false);
    }
  };

  const draftKey = (row: ReviewRow, key: number | string) => `${row.id}:${key}`;

  // a batch with missing German placeholder text or structural flags can be
  // re-translated with the same chain from the report's profiles
  const retranslate = async (row: ReviewRow) => {
    setWorking(true);
    try {
      const result = await api.translateRetranslateBatch(book.id, row.batch_index);
      setReview(result.review);
      await refreshBook();
      showToast("Block retranslated — fresh flags replace the old ones");
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setWorking(false);
    }
  };

  const isPlaceholderText = (row: ReviewRow) =>
    row.text_de &&
    row.text_de.trim().length <= 60 &&
    /^\[[^\[\]\n]{1,40}\]$/.test(row.text_de.trim());

  // ALL hooks stay above the early return — a conditional hook count would
  // crash the whole tab exactly when the first data arrives
  const flagChapters = useMemo(() => {
    return (review?.chapters ?? [])
      .map((chapter) => ({
        ...chapter,
        rows: chapter.rows.filter((row) => row.type !== "annotation" && (!onlyOpen || !row.decision)),
      }))
      .filter((chapter) => chapter.rows.length > 0);
  }, [review, onlyOpen]);

  const annotationChapters = useMemo(() => {
    return (review?.chapters ?? [])
      .map((chapter) => ({
        ...chapter,
        rows: chapter.rows.filter((row) => row.type === "annotation"),
      }))
      .filter((chapter) => chapter.rows.length > 0);
  }, [review]);
  const openAnnotations = useMemo(
    () => annotationChapters.flatMap((chapter) => chapter.rows.filter((row) => !row.decision)),
    [annotationChapters],
  );
  const readAnnotations = useMemo(
    () => annotationChapters.flatMap((chapter) => chapter.rows.filter((row) => row.decision)),
    [annotationChapters],
  );

  if (!review) return null;

  const decide = async (row: ReviewRow, action: "accepted" | "fixed" | "dismissed") => {
    setWorking(true);
    try {
      const result = await api.translateReviewDecide(book.id, {
        batch_index: row.batch_index,
        chapter_id: row.chapter_id,
        type: row.type,
        paragraph: row.paragraph,
        detail: row.detail,
        action,
        text: action === "fixed" ? (edited[row.id] ?? "").trim() || row.text_de : null,
      });
      setReview(result.review);
      setEdited((prev) => ({ ...prev, [row.id]: "" }));
      await refreshBook();
      showToast(
        action === "fixed"
          ? "Correction saved — outputs rewritten (manuscript.md etc.)"
          : action === "accepted"
            ? "Flag accepted"
            : "Flag dismissed",
      );
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setWorking(false);
    }
  };

  const toggleAnnotationChapter = (chapterId: number) => {
    setOpenAnnotationChapters((prev) => {
      const next = new Set(prev);
      if (next.has(chapterId)) next.delete(chapterId);
      else next.add(chapterId);
      return next;
    });
  };

  const locked = busyTranslation || working;

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
        <strong>
          Review — flags with a decision{" "}
          <span style={{ color: review.open_flags ? "var(--warn)" : "var(--good)", marginLeft: 6 }}>
            {review.open_flags} open
          </span>
        </strong>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label style={{ fontSize: 12, color: "var(--text-dim)", display: "flex", alignItems: "center", gap: 4 }}>
            <input type="checkbox" checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} /> only open
          </label>
          <button onClick={() => refresh()}>Refresh</button>
        </div>
      </div>
      <div className="inline-note">
        {review.hint ??
          "Flags (length/fidelity/…) need your decision. Annotations are model info — read-marking is instant, even during a run."}
        {review.mode === "ensemble" && " · Ensemble report: read-only view."}
        {busyTranslation && " · translation is running — flag decisions are locked, annotations stay clickable"}
      </div>

      {/* ------------------------------- FLAGS ------------------------------- */}
      {flagChapters.map((chapter) => (
        <div key={chapter.chapter_id} style={{ marginTop: 8 }}>
          <div className="muted" style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 2 }}>
            {chapter.chapter_name}
          </div>
          {chapter.rows.map((row) => {
            const decided = !!row.decision;
            const open = openFlagRowId === row.id;
            return (
              <div
                key={row.id}
                style={{
                  borderBottom: "1px solid var(--border)",
                  padding: "7px 4px",
                  opacity: decided ? 0.65 : 1,
                }}
              >
                <div
                  style={{ display: "flex", gap: 8, alignItems: "baseline", cursor: "pointer" }}
                  onClick={() => {
                    if (open) {
                      setOpenFlagRowId(null);
                      return;
                    }
                    setOpenFlagRowId(row.id);
                    setEdited((prev) => ({
                      ...prev,
                      [row.id]: row.decision?.action === "fixed" ? row.decision?.edited_text ?? row.text_de ?? "" : row.text_de ?? "",
                    }));
                  }}
                >
                  <span className="chip" style={{ color: BADGE_STYLE[row.type] ?? "var(--text-dim)" }}>
                    {row.type}
                  </span>
                  <span className="muted" style={{ fontSize: 11.5, minWidth: 34, textAlign: "right" }}>
                    {row.paragraph !== null ? `¶${row.paragraph + 1}` : "Batch"}
                  </span>
                  <span style={{ flex: 1, fontSize: 13 }}>{row.detail}</span>
                  {decided && (
                    <span className="chip" style={{ color: "var(--good)" }}>
                      {row.decision!.action}
                      {row.history_count > 1 ? ` (${row.history_count}×)` : ""}
                    </span>
                  )}
                  <span className="muted" style={{ fontSize: 11 }}>{open ? "▲" : "▼"}</span>
                </div>
                {open && (
                  <div style={{ padding: "8px 0 4px 44px" }}>
                    {row.source_en && (
                      <>
                        <div className="muted" style={{ fontSize: 11.5 }}>English original (read-only)</div>
                        <pre className="prose-view" style={{ maxHeight: 120, fontSize: 13, margin: "2px 0 8px" }}>
                          {row.source_en}
                        </pre>
                      </>
                    )}
                    <div className="muted" style={{ fontSize: 11.5 }}>
                      {row.paragraph === null && " — flag affects the whole batch"}
                    </div>
                    {row.text_de && (
                      <textarea
                        rows={Math.min(8, Math.max(3, Math.ceil((row.text_de.length || 1) / 90)))}
                        value={edited[row.id] ?? row.text_de}
                        onChange={(e) => setEdited((prev) => ({ ...prev, [row.id]: e.target.value }))}
                      />
                    )}
                    <div style={{ display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap" }}>
                      <button disabled={locked} onClick={() => decide(row, "accepted")}>
                        ✔ Akzeptiert
                      </button>
                      <button className="btn-danger" disabled={locked} onClick={() => decide(row, "fixed")}>
                        ✎ Korrigieren
                      </button>
                      <button disabled={locked} onClick={() => decide(row, "dismissed")}>
                        Verwerfen
                      </button>
                      <button
                        className="btn-ghost"
                        disabled={locked}
                        title="Entwurf → Verfeinerung → Prüfung für diesen Block erneut ausführen (gleiche Modelle wie im Report). Überschreibt Alt-Fassung samt Flags."
                        onClick={() => retranslate(row)}
                      >
                        ⟳ Block neu übersetzen
                      </button>
                      {isPlaceholderText(row) && (
                        <span className="chip" style={{ color: "var(--bad)", borderColor: "var(--bad)" }}>
                          deutscher Text fehlt (Platzhalter)
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ))}

      {/* --------------------------- ANNOTATIONS ---------------------------- */}
      {annotationChapters.length > 0 && (
        <div
          style={{
            marginTop: 12,
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: "8px 10px",
          }}
        >
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <button
              onClick={() => setAnnotationsOpen((v) => !v)}
              disabled={working}
              title="Anmerkungen = bewusste Änderungen, die das Modell beim Übersetzen gemacht hat. Rein informativ — nicht zu entscheiden."
            >
              {annotationsOpen ? "▾" : "▸"} Anmerkungen ({openAnnotations.length} offen · {readAnnotations.length} erledigt)
            </button>
            {openAnnotations.length > 0 && (
              <button onClick={() => setRead(openAnnotations.map((row) => row.id))} disabled={working}>
                Alle verstanden ({openAnnotations.length})
              </button>
            )}
            <span className="muted" style={{ fontSize: 12 }}>
              default zu — pro Kapitel aufklappen
            </span>
          </div>

          {annotationsOpen && (
            <div style={{ marginTop: 6 }}>
              {annotationChapters.map((chapter) => {
                const expanded = openAnnotationChapters.has(chapter.chapter_id);
                const counts = chapter.rows.reduce((acc, row) => (row.decision ? acc : acc + 1), 0);
                return (
                  <div key={chapter.chapter_id} style={{ borderTop: "1px solid var(--border)", padding: "6px 0" }}>
                    <div
                      style={{ display: "flex", gap: 8, alignItems: "baseline", cursor: "pointer" }}
                      onClick={() => toggleAnnotationChapter(chapter.chapter_id)}
                    >
                      <span className="muted" style={{ fontSize: 11 }}>
                        {expanded ? "▲" : "▼"}
                      </span>
                      <span style={{ fontWeight: 600, fontSize: 12.5 }}>{chapter.chapter_name}</span>
                      <span className="muted" style={{ fontSize: 11.5 }}>
                        {counts} offen / {chapter.rows.length} gesamt
                      </span>
                      {!expanded && counts > 0 && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setRead(chapter.rows.filter((row) => !row.decision).map((row) => row.id));
                          }}
                          disabled={working}
                          style={{ fontSize: 11.5, padding: "2px 8px" }}
                        >
                          Kapitel komplett verstanden
                        </button>
                      )}
                    </div>
                    {expanded &&
                      chapter.rows.map((row) => {
                        const editing = editRowId === row.id;
                        const paragraphsToEdit =
                          row.paragraph !== null
                            ? [{ index: row.paragraph, source: row.source_en, text: row.text_de }]
                            : row.paragraphs ?? [];
                        return (
                          <div
                            key={row.id}
                            style={{
                              padding: "4px 4px 4px 26px",
                              borderBottom: "1px solid var(--border)",
                              opacity: row.decision ? 0.55 : 1,
                            }}
                          >
                            <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                              <span className="muted" style={{ fontSize: 11, minWidth: 30, textAlign: "right" }}>
                                {row.paragraph !== null ? `¶${row.paragraph + 1}` : "Batch"}
                              </span>
                              <span style={{ flex: 1, fontSize: 12.5 }}>{row.detail}</span>
                              {row.decision ? (
                                <span className="chip" style={{ color: "var(--good)" }}>
                                  {row.decision.action === "read" ? "verstanden ✓" : row.decision.action}
                                </span>
                              ) : (
                                <>
                                  <button
                                    onClick={() => {
                                      if (editing) {
                                        setEditRowId(null);
                                        return;
                                      }
                                      setEditRowId(row.id);
                                      setEditDrafts((prev) => {
                                        const next = { ...prev };
                                        for (const p of paragraphsToEdit) {
                                          next[draftKey(row, p.index)] =
                                            next[draftKey(row, p.index)] ?? p.text ?? "";
                                        }
                                        return next;
                                      });
                                    }}
                                    disabled={working}
                                    style={{ fontSize: 11.5, padding: "2px 8px" }}
                                  >
                                    {editing ? "▲" : "✎ ändern"}
                                  </button>
                                  <button
                                    onClick={() => setRead([row.id])}
                                    disabled={working}
                                    style={{ fontSize: 11.5, padding: "2px 8px" }}
                                  >
                                    Verstanden
                                  </button>
                                </>
                              )}
                            </div>
                            {editing && (
                              <div style={{ margin: "6px 0 8px 38px" }}>
                                {row.source_en && (
                                  <div className="muted" style={{ fontSize: 11 }}>Original (read-only)</div>
                                )}
                                {row.source_en && row.paragraph !== null && (
                                  <pre className="prose-view" style={{ maxHeight: 90, fontSize: 12, margin: "2px 0 6px" }}>
                                    {row.source_en}
                                  </pre>
                                )}
                                {paragraphsToEdit.length === 0 ? (
                                  <div className="muted" style={{ fontSize: 11.5 }}>
                                    Keine Paragraphen in diesem Block bearbeitbar (Stage nicht abgeschlossen).
                                  </div>
                                ) : (
                                  paragraphsToEdit.map((p) => (
                                    <div key={p.index} style={{ marginBottom: 8 }}>
                                      {row.paragraph === null && (
                                        <div className="muted" style={{ fontSize: 11 }}>
                                          Absatz {p.index + 1}
                                          {row.source_en && ""}
                                        </div>
                                      )}
                                      {row.paragraph === null && p.source && (
                                        <pre className="prose-view" style={{ maxHeight: 80, fontSize: 11.5, margin: "2px 0 4px" }}>
                                          {p.source}
                                        </pre>
                                      )}
                                      <textarea
                                        rows={Math.min(8, Math.max(3, Math.ceil((p.text.length || 1) / 90)))}
                                        value={editDrafts[draftKey(row, p.index)] ?? p.text ?? ""}
                                        onChange={(e) =>
                                          setEditDrafts((prev) => ({ ...prev, [draftKey(row, p.index)]: e.target.value }))
                                        }
                                      />
                                      <button
                                        className="btn-primary"
                                        disabled={working}
                                        onClick={() => saveFix(row, p.index, editDrafts[draftKey(row, p.index)] ?? p.text)}
                                      >
                                        Übernehmen
                                      </button>
                                    </div>
                                  ))
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {flagChapters.length === 0 && review.open_flags === 0 && (
        <div className="muted" style={{ padding: 8 }}>
          {review.report_path ? "Keine offenen Flags — nur Anmerkungen vorhanden." : "No report yet."}
        </div>
      )}
    </div>
  );
}