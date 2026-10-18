import { useEffect, useState } from "react";
import Library from "./pages/Library";
import BookPage from "./pages/BookPage";
import { api } from "./api";

export default function App() {
  const [bookId, setBookId] = useState<string | null>(null);
  const [bookTab, setBookTab] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.health().catch((err) => setError(`Backend unreachable: ${err.message}`));
  }, []);

  const showToast = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(null), 2600);
  };

  return (
    <div className="app-shell">
      <div className="topbar">
        <h1 onClick={() => setBookId(null)}>
          Story<span>Forge</span>
        </h1>
        <div className="muted">Narrative State Tracking Studio</div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {bookId ? (
        <BookPage
          bookId={bookId}
          initialTab={bookTab as any}
          onBack={() => setBookId(null)}
          onOpen={(id, tab) => {
            setBookTab(tab ?? null);
            setBookId(id);
          }}
          showToast={showToast}
        />
      ) : (
        <Library onOpen={(id) => setBookId(id)} showToast={showToast} />
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}