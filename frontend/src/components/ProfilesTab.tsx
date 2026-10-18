import { useEffect, useState } from "react";
import { api } from "../api";
import type { Profile } from "../types";

const PRESETS: { name: string; base_url: string; price_in_per_m: number; price_out_per_m: number }[] = [
  { name: "Ollama (local)", base_url: "http://localhost:11434/v1", price_in_per_m: 0, price_out_per_m: 0 },
  { name: "Ollama Cloud (shared key)", base_url: "https://ollama.com/v1", price_in_per_m: 0, price_out_per_m: 0 },
  { name: "LM Studio (local)", base_url: "http://localhost:1234/v1", price_in_per_m: 0, price_out_per_m: 0 },
  { name: "OpenRouter", base_url: "https://openrouter.ai/api/v1", price_in_per_m: 0, price_out_per_m: 0 },
  { name: "OpenAI", base_url: "https://api.openai.com/v1", price_in_per_m: 2.5, price_out_per_m: 10 },
  { name: "DeepSeek", base_url: "https://api.deepseek.com/v1", price_in_per_m: 0.27, price_out_per_m: 1.1 },
];

type EditableProfile = Partial<Profile> & { extra_params_json?: string; model_chips?: string[] };

export default function ProfilesTab({ showToast }: { showToast: (m: string) => void }) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [editing, setEditing] = useState<EditableProfile | null>(null);
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [chipLoading, setChipLoading] = useState(false);
  const [ollamaKeySet, setOllamaKeySet] = useState(false);
  const [ollamaKeyDraft, setOllamaKeyDraft] = useState("");
  const [ollamaKeyBusy, setOllamaKeyBusy] = useState(false);

  const refresh = () =>
    api
      .listProfiles()
      .then((r) => setProfiles(r.profiles))
      .catch((err) => showToast(err.message));

  useEffect(() => {
    refresh();
    api
      .settings()
      .then((s) => setOllamaKeySet(Boolean(s.ollama_cloud_api_key_set)))
      .catch(() => {});
  }, []);

  const isCloudBase = (base: string) => /ollama\.(com|ai)/i.test(base ?? "");

  const saveOllamaKey = async (key: string) => {
    setOllamaKeyBusy(true);
    try {
      const result = await api.saveOllamaKey(key);
      setOllamaKeySet(result.ollama_cloud_api_key_set);
      setOllamaKeyDraft("");
      showToast(key ? "Ollama Cloud key saved — applies to every cloud model immediately" : "Ollama Cloud key cleared");
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setOllamaKeyBusy(false);
    }
  };

  const startNew = () =>
    setEditing({
      name: "",
      base_url: "http://localhost:11434/v1",
      api_key: "",
      model: "",
      temperature: 0.7,
      max_output_tokens: 32768,
      price_in_per_m: 0,
      price_out_per_m: 0,
      thinking: "default",
      extra_params: {},
    });

  const saveProfile = async () => {
    if (!editing) return;
    let extraParams: Record<string, any> = {};
    const rawExtra = (editing.extra_params_json ?? "").trim();
    if (rawExtra) {
      try {
        extraParams = JSON.parse(rawExtra);
      } catch {
        showToast("Extra params must be valid JSON, e.g. {\"top_p\": 0.9}");
        return;
      }
      if (typeof extraParams !== "object" || Array.isArray(extraParams)) {
        showToast("Extra params must be a JSON object");
        return;
      }
    }
    const data: any = {
      name: editing.name ?? "",
      base_url: editing.base_url ?? "",
      api_key: editing.api_key ?? "",
      model: editing.model ?? "",
      temperature: Number(editing.temperature ?? 0.7),
      max_output_tokens: Number(editing.max_output_tokens ?? 32768),
      price_in_per_m: Number(editing.price_in_per_m ?? 0),
      price_out_per_m: Number(editing.price_out_per_m ?? 0),
      thinking: editing.thinking ?? "default",
      extra_params: extraParams,
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

  // model chips inside the edit dialog — works BEFORE saving (typed base URL +
  // api key, e.g. Ollama Cloud) and for saved profiles (uses the stored key)
  const fetchModelChips = async () => {
    if (!editing) return;
    setChipLoading(true);
    try {
      const typedKey = editing.api_key && editing.api_key !== "***" ? editing.api_key : null;
      const res = await api.llmModels({
        profile_id: editing.id || null,
        base_url: editing.base_url || null,
        api_key: typedKey,
      });
      setEditing({ ...editing, model_chips: res.models });
      showToast(`Found ${res.models.length} models — click to select`);
    } catch (err: any) {
      showToast(`Model list failed: ${err.message}`);
    } finally {
      setChipLoading(false);
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
            <strong>Ollama Cloud — API key (shared)</strong>
            <div className="inline-note">
              ONE key for <b>all</b> Ollama Cloud models (base URL ollama.com/ollama.ai) — no per-model key. Every
              request re-reads it fresh from here, so you can swap it mid-run: the running wizard/translation/etc. picks
              the new key up on its very next call. Individual profiles without a global key fall back to their stored
              key.
            </div>
          </div>
          <span className={`chip ${ollamaKeySet ? "status-done" : "status-error"}`}>
            {ollamaKeySet ? "🔑 key set" : "no key"}
          </span>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <input
            type="password"
            style={{ flex: 1, minWidth: 260 }}
            value={ollamaKeyDraft}
            onChange={(e) => setOllamaKeyDraft(e.target.value)}
            placeholder={ollamaKeySet ? "New key (saving empty = clear key)" : "Paste your ollama.com API key"}
          />
          <button className="btn-primary" disabled={ollamaKeyBusy} onClick={() => saveOllamaKey(ollamaKeyDraft.trim())}>
            {ollamaKeyBusy ? "Saving…" : "Save key"}
          </button>
          {ollamaKeySet && (
            <button disabled={ollamaKeyBusy} onClick={() => saveOllamaKey("")}>
              Clear
            </button>
          )}
        </div>
      </div>

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <strong>LLM profiles</strong>
            <div className="inline-note">
              Any OpenAI-compatible endpoint works: local Ollama/LM Studio (no key needed — any placeholder key is fine)
              or paid APIs. Ollama Cloud profiles use the shared key above — leave their API key empty. Prices per 1M
              tokens drive the cost statistics.
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
                {isCloudBase(profile.base_url) && (
                  <span className="chip" style={{ marginLeft: 6 }}>
                    ☁ shared key
                  </span>
                )}
                {profile.thinking && profile.thinking !== "default" && (
                  <span className="chip" style={{ marginLeft: 6 }}>
                    think: {profile.thinking}
                  </span>
                )}
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
                {isCloudBase(editing.base_url ?? "") && (
                  <div className="inline-note">
                    Ollama Cloud: the shared key from the top card applies automatically — this field is only an
                    optional override while no global key is set.
                  </div>
                )}
              </div>
              <div className="form-col">
                <label>Model name</label>
                <div style={{ display: "flex", gap: 6 }}>
                  <input value={editing.model ?? ""} onChange={(e) => setEditing({ ...editing, model: e.target.value })} />
                  <button onClick={fetchModelChips} disabled={chipLoading || !(editing.base_url ?? "").trim()}>
                    {chipLoading ? "lädt…" : "Modelle laden"}
                  </button>
                </div>
                {editing.model_chips?.length ? (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8, maxHeight: 150, overflowY: "auto" }}>
                    {editing.model_chips.map((m) => (
                      <button
                        key={m}
                        type="button"
                        className={`chip btn-ghost${editing.model === m ? " status-done" : ""}`}
                        style={{ padding: "4px 10px" }}
                        onClick={() => setEditing({ ...editing, model: m })}
                      >
                        {editing.model === m ? "✔ " : ""}
                        {m}
                      </button>
                    ))}
                  </div>
                ) : null}
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
            <div className="form-row">
              <div className="form-col">
                <label>Thinking (reasoning)</label>
                <select value={editing.thinking ?? "default"} onChange={(e) => setEditing({ ...editing, thinking: e.target.value })}>
                  <option value="default">Model default</option>
                  <option value="off">Off — no thinking (fastest)</option>
                  <option value="low">Low</option>
                  <option value="medium">Standard</option>
                  <option value="high">High (deep, slow, expensive)</option>
                </select>
              </div>
              <div className="form-col">
                <label>Extra params (JSON, provider-specific)</label>
                <textarea
                  rows={2}
                  value={editing.extra_params_json ?? JSON.stringify(editing.extra_params ?? {})}
                  onChange={(e) => setEditing({ ...editing, extra_params_json: e.target.value })}
                  placeholder='{"top_p": 0.9, "repeat_penalty": 1.1}'
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