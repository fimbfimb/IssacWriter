import { useEffect, useState } from "react";
import { api } from "../api";
import type { Profile } from "../types";

const PRESETS: { name: string; base_url: string; price_in_per_m: number; price_out_per_m: number }[] = [
  { name: "Ollama (local)", base_url: "http://localhost:11434/v1", price_in_per_m: 0, price_out_per_m: 0 },
  { name: "LM Studio (local)", base_url: "http://localhost:1234/v1", price_in_per_m: 0, price_out_per_m: 0 },
  { name: "OpenRouter", base_url: "https://openrouter.ai/api/v1", price_in_per_m: 0, price_out_per_m: 0 },
  { name: "OpenAI", base_url: "https://api.openai.com/v1", price_in_per_m: 2.5, price_out_per_m: 10 },
  { name: "DeepSeek", base_url: "https://api.deepseek.com/v1", price_in_per_m: 0.27, price_out_per_m: 1.1 },
];

export default function ProfilesTab({ showToast }: { showToast: (m: string) => void }) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [editing, setEditing] = useState<Partial<Profile> | null>(null);
  const [models, setModels] = useState<Record<string, string[]>>({});

  const refresh = () =>
    api
      .listProfiles()
      .then((r) => setProfiles(r.profiles))
      .catch((err) => showToast(err.message));

  useEffect(() => {
    refresh();
  }, []);

  const startNew = () => setEditing({ name: "", base_url: "http://localhost:11434/v1", api_key: "", model: "", temperature: 0.7, max_output_tokens: 32768, price_in_per_m: 0, price_out_per_m: 0 });

  const saveProfile = async () => {
    if (!editing) return;
    const data = {
      name: editing.name ?? "",
      base_url: editing.base_url ?? "",
      api_key: editing.api_key ?? "",
      model: editing.model ?? "",
      temperature: Number(editing.temperature ?? 0.7),
      max_output_tokens: Number(editing.max_output_tokens ?? 32768),
      price_in_per_m: Number(editing.price_in_per_m ?? 0),
      price_out_per_m: Number(editing.price_out_per_m ?? 0),
    };
    try {
      if (editing.id) {
        await api.updateProfile(editing.id, data);
      } else {
        await api.createProfile(data);
      }
      setEditing(null);
      showToast("Profile saved");
      refresh();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const testModels = async (profile: Profile) => {
    try {
      const res = await api.profileModels(profile.id);
      setModels((prev) => ({ ...prev, [profile.id]: res.models }));
      showToast(`Found ${res.models.length} models (served from ${profile.name})`);
    } catch (err: any) {
      showToast(`Connection failed: ${err.message}`);
    }
  };

  const removeProfile = async (id: string) => {
    if (!window.confirm("Delete this profile?")) return;
    await api.deleteProfile(id).catch((err) => showToast(err.message));
    refresh();
  };

  return (
    <div>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <strong>LLM profiles</strong>
            <div className="inline-note">
              Any OpenAI-compatible endpoint works: local Ollama/LM Studio (no key needed — any placeholder key is fine)
              or paid APIs. Prices per 1M tokens drive the cost statistics.
            </div>
          </div>
          <button className="btn-primary" onClick={startNew}>
            + New profile
          </button>
        </div>

        <div style={{ marginTop: 10 }}>
          <div className="settings-row" style={{ borderBottomColor: "var(--border)", color: "var(--text-dim)", fontSize: 12 }}>
            <span>Name</span>
            <span>Base URL</span>
            <span>Model</span>
            <span>Price in $/1M</span>
            <span>Price out $/1M</span>
            <span>Actions</span>
          </div>
          {profiles.map((profile) => (
            <div key={profile.id} className="settings-row">
              <span>
                {profile.name} {profile.api_key_set ? <span style={{ color: "var(--good)", fontSize: 10 }}>🔑</span> : null}
              </span>
              <span className="muted" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis" }}>
                {profile.base_url}
              </span>
              <span>
                {profile.model}
                {models[profile.id] && (
                  <select
                    style={{ marginLeft: 6, fontSize: 11 }}
                    value={profile.model}
                    onChange={async (e) => {
                      await api.updateProfile(profile.id, { model: e.target.value });
                      refresh();
                    }}
                  >
                    {models[profile.id].map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                )}
              </span>
              <span className="muted">{profile.price_in_per_m}</span>
              <span className="muted">{profile.price_out_per_m}</span>
              <span style={{ display: "flex", gap: 6 }}>
                <button onClick={() => setEditing(profile)}>Edit</button>
                <button onClick={() => testModels(profile)}>Test</button>
                <button className="btn-danger" onClick={() => removeProfile(profile.id)}>
                  ✕
                </button>
              </span>
            </div>
          ))}
          {profiles.length === 0 && (
            <div className="muted" style={{ padding: 10 }}>
              No profiles yet — create one, e.g. Ollama for local models.
            </div>
          )}
        </div>
      </div>

      {editing && (
        <div className="modal-bg" onClick={() => setEditing(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginTop: 0 }}>{editing.id ? `Edit ${editing.name}` : "New LLM profile"}</h3>

            {!editing.id && (
              <div className="form-row">
                <div className="form-col">
                  <label>Preset</label>
                  <select
                    onChange={(e) => {
                      const preset = PRESETS[Number(e.target.value)];
                      if (preset) setEditing({ ...editing, name: preset.name, base_url: preset.base_url, price_in_per_m: preset.price_in_per_m, price_out_per_m: preset.price_out_per_m });
                    }}
                  >
                    <option value="">— choose preset —</option>
                    {PRESETS.map((preset, i) => (
                      <option key={preset.name} value={i}>
                        {preset.name}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            )}

            <div className="form-row">
              <div className="form-col">
                <label>Name</label>
                <input value={editing.name ?? ""} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
              </div>
              <div className="form-col">
                <label>Base URL (must end with /v1)</label>
                <input value={editing.base_url ?? ""} onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} />
              </div>
            </div>
            <div className="form-row">
              <div className="form-col">
                <label>API key {editing.api_key_set && <span className="muted">(leave empty to keep current)</span>}</label>
                <input
                  type="password"
                  value={editing.api_key ?? ""}
                  onChange={(e) => setEditing({ ...editing, api_key: e.target.value })}
                  placeholder="empty = keep stored key"
                />
              </div>
              <div className="form-col">
                <label>Model name</label>
                <input value={editing.model ?? ""} onChange={(e) => setEditing({ ...editing, model: e.target.value })} />
              </div>
            </div>
            <div className="form-row">
              <div className="form-col">
                <label>Temperature</label>
                <input
                  type="number"
                  step={0.1}
                  value={editing.temperature ?? 0.7}
                  onChange={(e) => setEditing({ ...editing, temperature: Number(e.target.value) })}
                />
              </div>
              <div className="form-col">
                <label>Max output tokens</label>
                <input
                  type="number"
                  value={editing.max_output_tokens ?? 32768}
                  onChange={(e) => setEditing({ ...editing, max_output_tokens: Number(e.target.value) })}
                />
              </div>
              <div className="form-col">
                <label>Price in $/1M</label>
                <input
                  type="number"
                  step={0.001}
                  value={editing.price_in_per_m ?? 0}
                  onChange={(e) => setEditing({ ...editing, price_in_per_m: Number(e.target.value) })}
                />
              </div>
              <div className="form-col">
                <label>Price out $/1M</label>
                <input
                  type="number"
                  step={0.001}
                  value={editing.price_out_per_m ?? 0}
                  onChange={(e) => setEditing({ ...editing, price_out_per_m: Number(e.target.value) })}
                />
              </div>
            </div>
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 8 }}>
              <button onClick={() => setEditing(null)}>Cancel</button>
              <button className="btn-primary" onClick={saveProfile}>
                Save profile
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}