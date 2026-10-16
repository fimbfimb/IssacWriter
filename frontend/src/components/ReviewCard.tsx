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
  const [openRowId, setOpenRowId] = useState<string | null>(null);
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [working, setWorking] = useState(false);
  const [onlyOpen, setOnlyOpen] = useState(true);

  const refresh = () =>
    api
      .translateReview(book.id)
      .then((r) => setReview(r))
      .catch((e) => showToast(e.message));

  useEffect(() => {
    refresh();
  }, [book.id]);

  const openFlags = review?.open_flags ?? 0;
  const rows = useMemo(() => review?.chapters ?? [], [review]);
  const visible = useMemo(() => {
    if (!onlyOpen) return rows;
    return rows
      .map((chapter) => ({
        ...chapter,
        rows: chapter.rows.filter((row) => row.type === "annotation" || !row.decision),
      }))
      .filter((chapter) => chapter.rows.length > 0);
  }, [rows, onlyOpen]);

  // annotation rows are info rows: they also need the "understood" click to disappear
  const decide = async (
    row: ReviewRow,
    action: "accepted" | "fixed" | "dismissed",
  ) => {
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

  const openAnnotations = useMemo(() => {
    const rows: ReviewRow[] = [];
    for (const chapter of review?.chapters ?? []) {
      for (const row of chapter.rows) {
        if (row.type === "annotation" && !row.decision) rows.push(row);
      }
    }
    return rows;
  }, [review]);

  const markAllAnnotationsRead = async () => {
    setWorking(true);
    try {
      for (const row of openAnnotations) {
        await api.translateReviewDecide(book.id, {
          batch_index: row.batch_index,
          chapter_id: row.chapter_id,
          type: row.type,
          paragraph: row.paragraph,
          detail: row.detail,
          action: "accepted",
          text: null,
        });
      }
      await refresh();
      await refreshBook();
      showToast("Alle Anmerkungen als verstanden markiert");
    } catch (err: any) {
      showToast(err.message);
      await refresh();
    } finally {
      setWorking(false);
    }
  };

  const isPlaceholderText =
    (row: ReviewRow) =>
      row.text_de &&
      row.text_de.trim().length <= 60 &&
      /^\[[^\[\]\n]{1,40}\]$/.test(row.text_de.trim());

  if (!review) return null;
  if (review.total_flags === 0 && review.open_flags === 0 && !review.hint) return null;

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
        <strong>
          Review — flags with a decision{" "}
          <span style={{ color: openFlags ? "var(--warn)" : "var(--good)", marginLeft: 6 }}>
            {openFlags} open
          </span>
        </strong>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {openAnnotations.length > 0 && (
            <button onClick={markAllAnnotationsRead} disabled={busyTranslation || working}>
              Alle Anmerkungen verstanden ({openAnnotations.length})
            </button>
          )}
          <label style={{ fontSize: 12, color: "var(--text-dim)", display: "flex", alignItems: "center", gap: 4 }}>
            <input type="checkbox" checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} /> only open
          </label>
          <button onClick={() => refresh()}>Refresh</button>
        </div>
      </div>
      <div className="inline-note">
        {review.hint ??
          "Each flag: context view, then decide. Correction writes the new text directly into all outputs. Lock while a translation pass is running."}
        {review.mode === "ensemble" && " · Ensemble report: read-only view."}
        {busyTranslation && " · translation is running — decisions are locked"}
      </div>

      {visible.length === 0 && openFlags === 0 && (
        <div className="muted" style={{ padding: 8 }}>
          {review.report_path ? "All flags processed. Nothing left open." : "No report yet."}
        </div>
      )}

      {visible.map((chapter) => (
        <div key={chapter.chapter_id} style={{ marginTop: 8 }}>
          <div className="muted" style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 2 }}>
            {chapter.chapter_name}
          </div>
          {chapter.rows.map((row) => {
            const decided = !!row.decision;
            const open = openRowId === row.id;
            const locked = !row.editable || busyTranslation || working;
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
                      setOpenRowId(null);
                      return;
                    }
                    setOpenRowId(row.id);
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
                      {row.type === "annotation" && row.paragraph === null && row.text_de === ""
                        ? "Note (no paragraph affected)"
                        : "German paragraph"}
                      {row.paragraph === null && row.type !== "annotation" && " — flag affects the whole batch"}
                    </div>
                    {row.type !== "annotation" && row.text_de && (
                      <textarea
                        rows={Math.min(8, Math.max(3, Math.ceil((row.text_de.length || 1) / 90)))}
                        value={edited[row.id] ?? row.text_de}
                        onChange={(e) => setEdited((prev) => ({ ...prev, [row.id]: e.target.value }))}
                      />
                    )}
                    {row.type === "annotation" ? (
                      <button
                        className="btn-primary"
                        disabled={locked || !!row.decision && onlyOpen}
                        onClick={() => decide(row, "accepted")}
                      >
                        Verstanden
                      </button>
                    ) : (
                      <div style={{ display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap" }}>
                        <button disabled={locked} onClick={() => decide(row, "accepted")}>
                          ✔ Akzeptiert
                        </button>
                        <button
                          className="btn-danger"
                          disabled={locked}
                          onClick={() => decide(row, "fixed")}
                        >
                          ✎ Korrigieren
                        </button>
                        <button disabled={locked} onClick={() => decide(row, "dismissed")}>
                          Verwerfen
                        </button>
                        <button
                          className="btn-ghost"
                          disabled={locked}
                          title="Führt Entwurf → Verfeinerung → Prüfung für diesen Block erneut aus (gleiche Modelle, wie im Report). Überschreibt Alt-Fassung samt Flags."
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
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}