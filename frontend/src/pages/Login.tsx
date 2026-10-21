import { useEffect, useState } from "react";
import { api, setCurrentUser } from "../api";
import type { StudioUser } from "../types";

function initial(name: string): string {
  const clean = (name || "").trim();
  return clean ? clean.charAt(0).toUpperCase() : "?";
}

// Passwordless intranet login: pick a user card (or create one) — identity
// only partitions data on the shared machine, it protects nothing.
export default function Login({ onPicked, showToast }: { onPicked: (user: StudioUser) => void; showToast: (m: string) => void }) {
  const [users, setUsers] = useState<StudioUser[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = () => api.listUsers().then((r) => setUsers(r.users)).catch((e) => showToast(e.message));

  useEffect(() => {
    refresh();
  }, []);

  const pick = (user: StudioUser) => {
    setCurrentUser(user); // persist BEFORE any request goes out — only picking state would keep sending no header
    onPicked(user);
  };

  const create = async () => {
    const clean = name.trim();
    if (!clean) return;
    setBusy(true);
    try {
      const result = await api.createUser(clean);
      setName("");
      pick(result.user);
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <h2 style={{ marginTop: 0 }}>Who is writing?</h2>
        <div className="muted" style={{ marginBottom: 16 }}>
          Pick your user to see your own library and LLM profiles. No password — this is a shared
          intranet machine, not a security wall.
        </div>

        <div className="login-users">
          {users.map((user) => (
            <button key={user.id} className="login-user" onClick={() => pick(user)}>
              <span className="login-avatar">{initial(user.name)}</span>
              <span className="login-user-name">{user.name}</span>
            </button>
          ))}
          {users.length === 0 && (
            <div className="muted" style={{ width: "100%" }}>
              No users yet — create the first one below.
            </div>
          )}
        </div>

        <div className="login-create">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()}
            placeholder="New user…"
          />
          <button className="btn-primary" disabled={busy || !name.trim()} onClick={create}>
            {busy ? "Creating…" : "Create user"}
          </button>
        </div>
      </div>
    </div>
  );
}