import { useEffect, useState } from "react";
import Library from "./pages/Library";
import Login from "./pages/Login";
import BookPage from "./pages/BookPage";
import { api, currentUser, setCurrentUser } from "./api";
import type { StudioUser } from "./types";

export default function App() {
  const [user, setUser] = useState<StudioUser | null>(() => currentUser());
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

  const logout = () => {
    setCurrentUser(null);
    setUser(null);
    setBookId(null);
  };

  const initial = user?.name ? user.name.charAt(0).toUpperCase() : "?";

  return (
    <div className="app-shell">
      <div className="topbar">
        <h1 onClick={() => setBookId(null)}>
          Isaac<span>Writer</span>
        </h1>
        <div className="muted">Narrative State Tracking Studio</div>
        {user && (
          <div className="topbar-user">
            <span className="login-avatar" title={user.name}>
              {initial}
            </span>
            <span>{user.name}</span>
            <button className="btn-ghost" onClick={logout}>
              Logout
            </button>
          </div>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}

      {!user ? (
        <Login onPicked={setUser} showToast={showToast} />
      ) : bookId ? (
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