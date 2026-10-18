import { useEffect, useState } from "react";
import { api } from "../api";
import type { BookSummary, WizardBookSummary, WizardBookStep } from "../types";

const WIZARD_STEP_LABELS: Record<string, string> = {
  outline: "Outline",
  outline_refine: "Outline refinement",
  write: "Write chapters",
  text_refine: "Text refinement",
  translate: "Translate",
  translate_refine: "Translation refinement",
};

// reuse the runner-status chip colors for wizard states
const WIZARD_CHIP_CLASS: Record<string, string> = {
  running: "status-writing",
  pausing: "status-paused",
  paused: "status-paused",
  stopping: "status-stopped",
  stopped: "status-stopped",
  done: "status-done",
  error: "status-error",
};

function stepPercent(step: WizardBookStep): number {
  if (step.status === "done" || step.status === "skipped") return 100;
  const progress = step.progress;
  if (progress?.total && progress.total > 0)
    return Math.min(100, Math.round(((progress.done || 0) / progress.total) * 100));
  return step.status === "running" ? 4 : 0;
}

// average step completion across all non-skipped steps (same doctrine as the Wizard tab board)
function wizardOverallPercent(wizard: WizardBookSummary): number {
  const active = wizard.steps.filter((s) => s.status !== "skipped");
  const rows = active.length ? active : wizard.steps;
  if (!rows.length) return 0;
  return Math.round(rows.reduce((sum, s) => sum + stepPercent(s), 0) / rows.length);
}

function progressText(step: WizardBookStep | undefined): string {
  if (!step) return "";
  const progress = step.progress;
  if (progress?.total != null) return `${progress.done || 0}/${progress.total}`;
  return `${progress?.done ?? 0}`;
}

export default function Library({ onOpen, showToast }: { onOpen: (id: string) => void; showToast: (m: string) => void }) {
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    title: "",
    story_prompt: "",
    genre_notes: "",
    language: "en",
    target_words: 20000,
  });
  const [busy, setBusy] = useState(false);

  const refresh = () => api.listBooks().then((r) => setBooks(r.books)).catch((e) => showToast(e.message));

  useEffect(() => {
    refresh();
    // the wizard advances between polls — keep the cards' stage/step info live
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, []);

  const create = async () => {
    if (!form.title.trim() || !form.story_prompt.trim()) {
      showToast("Title and story prompt are required");
      return;
    }
    setBusy(true);
    try {
      const result = await api.createBook(form);
      setCreating(false);
      setForm({ title: "", story_prompt: "", genre_notes: "", language: "en", target_words: 20000 });
      onOpen(result.book.id);
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string, event: React.MouseEvent) => {
    event.stopPropagation();
    if (!window.confirm("Delete this book and all chapters? This cannot be undone.")) return;
    try {
      await api.deleteBook(id);
      refresh();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <div className="muted">
          {books.length} book{books.length === 1 ? "" : "s"} in library
        </div>
        <button className="btn-primary" onClick={() => setCreating(true)}>
          + New Book
        </button>
      </div>

      <div className="book-grid">
        {books.map((book) => {
          const wizard = book.wizard;
          const current = wizard?.steps.find((s) => s.step === wizard.current_step);
          const currentLabel = current ? WIZARD_STEP_LABELS[current.kind] ?? `Step ${current.step}` : null;
          const overall = wizard ? wizardOverallPercent(wizard) : 0;
          return (
            <div key={book.id} className="book-card" onClick={() => onOpen(book.id)}>
              <h3>{book.title}</h3>
              <div className="muted" style={{ fontSize: 12 }}>
                {book.chapters_written} chapters · {book.words_written.toLocaleString()} /{" "}
                {book.target_words.toLocaleString()} words · {book.language.toUpperCase()}
              </div>
              <div className="progress-outer">
                <div
                  className="progress-inner"
                  style={{
                    width: `${Math.min(100, (book.words_written / Math.max(1, book.target_words)) * 100).toFixed(1)}%`,
                  }}
                />
              </div>
              {wizard ? (
                <>
                  <div className="muted" style={{ fontSize: 12, marginTop: 6, display: "flex", alignItems: "center", gap: 6 }}>
                    <span>🧙 {wizard.steps_done}/{wizard.steps_total} steps</span>
                    {current && current.status !== "done" && (
                      <span style={{ color: "var(--accent)" }}>
                        · {currentLabel}
                        {current.status === "running" ? ` (${progressText(current)})` : ""}
                      </span>
                    )}
                  </div>
                  <div className="progress-outer" title={`Wizard ${overall}%`}>
                    <div className="progress-inner" style={{ width: `${overall}%`, backgroundColor: "var(--accent)" }} />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span className={`chip status-idle ${WIZARD_CHIP_CLASS[wizard.status] ?? ""}`}>
                      🧙 {wizard.status.replace("_", " ")}
                    </span>
                    <button className="btn-ghost btn-danger" onClick={(e) => remove(book.id, e)}>
                      Delete
                    </button>
                  </div>
                </>
              ) : (
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className={`chip status-${book.status}`}>{book.status.replace("_", " ")}</span>
                  <button className="btn-ghost btn-danger" onClick={(e) => remove(book.id, e)}>
                    Delete
                  </button>
                </div>
              )}
            </div>
          );
        })}
        {books.length === 0 && (
          <div className="card muted">
            No books yet. Create your first book: give it a story prompt, pick a language and a target length,
            then generate a frozen outline and let the NstAgent write chapter by chapter.
          </div>
        )}
      </div>

      {creating && (
        <div className="modal-bg" onClick={() => setCreating(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginTop: 0 }}>Create a new book</h3>
            <div className="form-col" style={{ marginBottom: 12 }}>
              <label>Title</label>
              <input
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                placeholder="The Harbor Mystery"
              />
            </div>
            <div className="form-col" style={{ marginBottom: 12 }}>
              <label>Story prompt (premise, or a full instruction)</label>
              <textarea
                rows={6}
                value={form.story_prompt}
                onChange={(e) => setForm({ ...form, story_prompt: e.target.value })}
                placeholder="A detective story set in a foggy harbor city: inspector Marta Voss hunts a smuggler whose ledger of debts implicates her own father. Tone: noir, dry humor."
              />
            </div>
            <div className="form-col" style={{ marginBottom: 12 }}>
              <label>Style / constraint notes (optional, become part of the story contract)</label>
              <textarea
                rows={3}
                value={form.genre_notes}
                onChange={(e) => setForm({ ...form, genre_notes: e.target.value })}
                placeholder="Close third person, literary but propulsive; short chapters allowed; no explicit violence."
              />
            </div>
            <div className="form-row">
              <div className="form-col">
                <label>Language</label>
                <select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })}>
                  <option value="en">English</option>
                  <option value="de">Deutsch</option>
                </select>
              </div>
              <div className="form-col">
                <label>
                  Target length: {form.target_words.toLocaleString()} words
                </label>
                <input
                  type="range"
                  min={5000}
                  max={150000}
                  step={5000}
                  value={form.target_words}
                  onChange={(e) => setForm({ ...form, target_words: Number(e.target.value) })}
                />
              </div>
            </div>
            <div className="inline-note">
              10K words ≈ 10 chapters · 20K ≈ 15 · 50K ≈ 25 · 100K ≈ 40. You can extend the codex (characters,
              settings) before generating the outline.
            </div>
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
              <button onClick={() => setCreating(false)}>Cancel</button>
              <button className="btn-primary" disabled={busy} onClick={create}>
                {busy ? "Creating…" : "Create book"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}