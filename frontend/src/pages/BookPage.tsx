import { useCallback, useEffect, useState } from "react";
import { api, openEventStream } from "../api";
import type { Book } from "../types";
import OutlineTab from "../components/OutlineTab";
import CodexTab from "../components/CodexTab";
import WriteTab from "../components/WriteTab";
import StatsTab from "../components/StatsTab";
import ProfilesTab from "../components/ProfilesTab";
import TranslateTab from "../components/TranslateTab";
import RefinerTab from "../components/RefinerTab";
import LayoutTab from "../components/LayoutTab";
import SequelDialog from "../components/SequelDialog";
import WizardTab from "../components/WizardTab";
import ExportTab from "../components/ExportTab";

type TabName = "wizard" | "outline" | "write" | "refine" | "translate" | "codex" | "layout" | "stats" | "models" | "export";

export default function BookPage({
  bookId,
  onBack,
  onOpen,
  showToast,
  initialTab,
}: {
  bookId: string;
  onBack: () => void;
  onOpen: (id: string, tab?: string) => void;
  showToast: (m: string) => void;
  initialTab?: TabName | null;
}) {
  const [book, setBook] = useState<Book | null>(null);
  const [tab, setTab] = useState<TabName>(initialTab ?? "outline");
  const [events, setEvents] = useState<any[]>([]);
  const [runnerTick, setRunnerTick] = useState(0);
  const [sequelOpen, setSequelOpen] = useState(false);

  const refreshBook = useCallback(async () => {
    try {
      const result = await api.getBook(bookId);
      setBook(result.book);
    } catch (err: any) {
      showToast(err.message);
    }
  }, [bookId]);

  useEffect(() => {
    refreshBook();
  }, [bookId]);

  useEffect(() => {
    const close = openEventStream(bookId, (event) => {
      setEvents((prev) => [...prev.slice(-500), event]);
      setRunnerTick((t) => t + 1);
      if ([
        "outline_done", "chapter_done", "runner_stopped", "runner_error",
        "translation_done", "translation_error", "refine_done", "refine_error",
        "outline_refine_done", "outline_refine_error",
        "german_refine_done", "german_refine_error",
        "wizard_step_started", "wizard_step_done", "wizard_step_error",
        "wizard_paused", "wizard_resumed", "wizard_done", "wizard_stopped",
      ].includes(event.type)) {
        refreshBook();
      }
    });
    return () => close();
  }, [bookId]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      api.runStatus(bookId).then((r) => setBook((prev) => (prev ? { ...prev, runner: r.status } : prev))).catch(() => {});
    }, 4000);
    return () => window.clearInterval(timer);
  }, [bookId, runnerTick]);

  if (!book) return <div className="muted">Loading book…</div>;

  const runnerStatus = book.runner?.status ?? "idle";
  const words = book.words_written ?? 0;
  const coverage = Math.min(100, (words / Math.max(1, book.target_words)) * 100);

  return (
    <div>
      <div className="card" style={{ display: "flex", justifyContent: "space-between", gap: 18, alignItems: "center" }}>
        <div>
          <h2 style={{ margin: "0 0 6px" }}>{book.title}</h2>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <span className={`chip status-${runnerStatus}`}>{runnerStatus.replace("_", " ")}</span>
            <span className="chip">{book.language.toUpperCase()}</span>
            <span className="chip">
              {words.toLocaleString()} / {book.target_words.toLocaleString()} words
            </span>
            <span className="chip">{book.chapters_written ?? 0} chapters written</span>
            <span className="chip">{book.outline?.frozen ? "outline frozen" : "outline open"}</span>
            <span className="chip">{book.run_settings?.mode === "review" ? "review per chapter" : "full auto"}</span>
          </div>
          <div className="progress-outer" style={{ maxWidth: 420 }}>
            <div className="progress-inner" style={{ width: `${coverage.toFixed(1)}%` }} />
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-end" }}>
          <button onClick={onBack}>← Library</button>
          <button
            disabled={(book.chapters_written ?? 0) < 1 || ["writing", "waiting_review", "generating_outline"].includes(runnerStatus)
            }
            title={
              (book.chapters_written ?? 0) < 1
                ? "Band 2 needs at least one written chapter"
                : "Extract the ending of this book and prepare a seamless sequel"
            }
            onClick={() => setSequelOpen(true)}
          >
            ▶ Fortsetzung vorbereiten
          </button>
        </div>
      </div>

      {book.runner?.error && (
        <div className="error-banner">Runner error: {book.runner.error}</div>
      )}
      {book.last_error && !book.runner?.error && (
        <div className="error-banner">Last error: {book.last_error}</div>
      )}

      <div className="tabs">
        <button className={`tab ${tab === "wizard" ? "active" : ""}`} onClick={() => setTab("wizard")}>
          🧙 Wizard
        </button>
        <button className={`tab ${tab === "outline" ? "active" : ""}`} onClick={() => setTab("outline")}>
          Outline
        </button>
        <button className={`tab ${tab === "write" ? "active" : ""}`} onClick={() => setTab("write")}>
          Write
        </button>
        <button className={`tab ${tab === "refine" ? "active" : ""}`} onClick={() => setTab("refine")}>
          Refine
        </button>
        <button className={`tab ${tab === "translate" ? "active" : ""}`} onClick={() => setTab("translate")}>
          Translate
        </button>
        <button className={`tab ${tab === "layout" ? "active" : ""}`} onClick={() => setTab("layout")}>
          Layout
        </button>
        <button className={`tab ${tab === "codex" ? "active" : ""}`} onClick={() => setTab("codex")}>
          Codex
        </button>
        <button className={`tab ${tab === "stats" ? "active" : ""}`} onClick={() => setTab("stats")}>
          Statistics
        </button>
        <button className={`tab ${tab === "export" ? "active" : ""}`} onClick={() => setTab("export")}>
          📤 Export
        </button>
        <button className={`tab ${tab === "models" ? "active" : ""}`} onClick={() => setTab("models")}>
          LLM Profiles
        </button>
      </div>

      {tab === "wizard" && <WizardTab book={book} events={events} showToast={showToast} onOpen={onOpen} />}
      {tab === "outline" && <OutlineTab book={book} events={events} refreshBook={refreshBook} showToast={showToast} />}
      {tab === "write" && (
        <WriteTab book={book} events={events} refreshBook={refreshBook} showToast={showToast} />
      )}
      {tab === "refine" && (
        <RefinerTab book={book} events={events} refreshBook={refreshBook} showToast={showToast} />
      )}
      {tab === "translate" && (
        <TranslateTab book={book} events={events} refreshBook={refreshBook} showToast={showToast} />
      )}
      {tab === "layout" && <LayoutTab book={book} showToast={showToast} />}
      {tab === "codex" && <CodexTab book={book} refreshBook={refreshBook} showToast={showToast} />}
      {tab === "stats" && <StatsTab book={book} showToast={showToast} />}
      {tab === "export" && <ExportTab book={book} showToast={showToast} />}
      {tab === "models" && <ProfilesTab showToast={showToast} />}

      {sequelOpen && (
        <SequelDialog
          book={book}
          onClose={() => setSequelOpen(false)}
          onDone={(newId) => {
            setSequelOpen(false);
            onOpen(newId);
          }}
          showToast={showToast}
        />
      )}
    </div>
  );
}