import { useEffect, useState } from "react";
import { api } from "../api";
import type { Book, Codex, CodexEntry, Profile, StateCollection } from "../types";

const EMPTY_ENTRY: CodexEntry = { name: "", aliases: [], description: "", role: "", color: "" };

function randomColor() {
  const palette = ["#7c9cff", "#3ecf8e", "#f5a623", "#ff6b6b", "#b48cff", "#4dd0e1", "#ffb74d", "#f06292"];
  return palette[Math.floor(Math.random() * palette.length)];
}

function EntryEditor({
  entries,
  onChange,
  kind,
}: {
  entries: CodexEntry[];
  onChange: (next: CodexEntry[]) => void;
  kind: "characters" | "settings" | "lore";
}) {
  const update = (index: number, patch: Partial<CodexEntry>) => {
    onChange(entries.map((e, i) => (i === index ? { ...e, ...patch } : e)));
  };
  const remove = (index: number) => onChange(entries.filter((_, i) => i !== index));
  const add = () => onChange([...entries, { ...EMPTY_ENTRY, color: randomColor() }]);

  return (
    <div>
      {entries.map((entry, index) => (
        <div key={index} className="codex-row">
          {kind !== "lore" && (
            <>
              <div className="color-dot" style={{ background: entry.color || "#39415a" }} />
              <div style={{ width: 170, display: "flex", flexDirection: "column", gap: 5 }}>
                <input
                  placeholder={kind === "characters" ? "Name" : "Setting name"}
                  value={entry.name}
                  onChange={(e) => update(index, { name: e.target.value })}
                />
                <input
                  placeholder="Aliases (comma separated)"
                  value={(entry.aliases ?? []).join(", ")}
                  onChange={(e) =>
                    update(index, { aliases: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
                  }
                />
              </div>
              <div style={{ width: 150, display: "flex", flexDirection: "column", gap: 5 }}>
                {kind === "characters" && (
                  <input placeholder="Role (e.g. protagonist)" value={entry.role ?? ""} onChange={(e) => update(index, { role: e.target.value })} />
                )}
                {kind === "characters" && (
                  <input
                    type="color"
                    value={entry.color || "#7c9cff"}
                    onChange={(e) => update(index, { color: e.target.value })}
                    style={{ padding: 2, height: 32 }}
                  />
                )}
              </div>
            </>
          )}
          {kind === "lore" && (
            <div style={{ width: 320 }}>
              <input placeholder="Lore item name / rule" value={entry.name} onChange={(e) => update(index, { name: e.target.value })} />
            </div>
          )}
          <div style={{ flex: 1 }}>
            <textarea
              rows={2}
              placeholder={
                kind === "characters"
                  ? "Who is this person, what do they want, how do they speak?"
                  : kind === "settings"
                    ? "What this place looks like, who lives there, its role in the story."
                    : "A world rule or fact that must stay true in every chapter."
              }
              value={entry.description}
              onChange={(e) => update(index, { description: e.target.value })}
            />
          </div>
          <button className="btn-ghost btn-danger" onClick={() => remove(index)}>
            ✕
          </button>
        </div>
      ))}
      <button className="btn-ghost" onClick={add} style={{ marginTop: 8 }}>
        + Add {kind === "characters" ? "character" : kind === "settings" ? "setting" : "lore item"}
      </button>
    </div>
  );
}

export default function CodexTab({
  book,
  refreshBook,
  showToast,
}: {
  book: Book;
  refreshBook: () => Promise<void>;
  showToast: (m: string) => void;
}) {
  const [codex, setCodex] = useState<Codex>(book.codex ?? { characters: [], settings: [], lore: [] });
  const [state, setState] = useState<StateCollection | null>(null);
  const [dirty, setDirty] = useState(false);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [extracting, setExtracting] = useState(false);

  useEffect(() => setCodex(book.codex ?? { characters: [], settings: [], lore: [] }), [book.id]);
  useEffect(() => {
    api
      .getState(book.id)
      .then((r) => setState(r.state))
      .catch(() => {});
    api
      .listProfiles()
      .then((r) => setProfiles(r.profiles))
      .catch(() => {});
  }, [book.id, book.chapters_written]);

  const extractionProfileId = book.planner_profile_id ?? profiles[0]?.id ?? "";

  const extractFromOutline = async () => {
    if (!book.outline?.premise) {
      showToast("No outline yet — generate it first (Outline tab)");
      return;
    }
    if (!extractionProfileId) {
      showToast("Create an LLM profile first (tab: LLM Profiles)");
      return;
    }
    setExtracting(true);
    try {
      const result = await api.extractCodex(book.id, extractionProfileId);
      setCodex(result.codex);
      setDirty(false);
      const addedNames = result.added_names?.characters?.added ?? [];
      showToast(
        `Extraction done: +${result.added.characters} characters, +${result.added.settings} settings${
          addedNames.length ? ` (${addedNames.join(", ")})` : " (all already present)"
        } — now edit appearance, voice, goals`,
      );
      refreshBook();
    } catch (err: any) {
      showToast(`Extraction failed: ${err.message}`);
    } finally {
      setExtracting(false);
    }
  };

  const save = async () => {
    try {
      await api.replaceCodex(book.id, codex);
      setDirty(false);
      showToast("Codex saved");
      refreshBook();
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const section = (kind: "characters" | "settings" | "lore", title: string, hint: string) => (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <strong>{title}</strong>
        <span className="muted" style={{ fontSize: 12 }}>
          {hint}
        </span>
      </div>
      <EntryEditor
        entries={codex[kind]}
        onChange={(next) => {
          setCodex({ ...codex, [kind]: next });
          setDirty(true);
        }}
        kind={kind}
      />
    </div>
  );

  return (
    <div>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <strong>Story Codex</strong>
            <div className="inline-note">
              The codex is injected into the planner prompts so your characters and settings shape the outline. Names and
              aliases also power the mention statistics in the Statistics tab.
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              disabled={extracting || !book.outline?.premise}
              title="Let the planner LLM read the outline and create codex entries for every character and setting it uses. Your own entries are never touched."
              onClick={extractFromOutline}
            >
              {extracting ? "Extracting…" : "✦ Extract from outline"}
            </button>
            <button className="btn-primary" disabled={!dirty} onClick={save}>
              {dirty ? "Save codex" : "Saved"}
            </button>
          </div>
        </div>
      </div>

      {section("characters", "Characters", "with aliases, role and color — used for the presence timeline")}
      {section("settings", "Settings & locations", "places the story visits — used for the settings timeline")}
      {section("lore", "World lore", "hard facts and rules the writing agent must never contradict")}

      {state && (
        <div className="card">
          <strong>Live narrative state</strong>
          <div className="inline-note">
            Maintained by the writing agent (NstAgent): overwritten character snapshots, established past events, and
            open obligations. This is what keeps every chapter consistent.
          </div>
          <div className="split" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
            <div>
              <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                Character snapshots ({state.character_states.length})
              </div>
              {state.character_states.map((entry) => (
                <div key={entry.id} style={{ marginBottom: 8, fontSize: 12.5 }}>
                  <strong>{entry.name}</strong>
                  <div className="muted">{entry.description}</div>
                </div>
              ))}
              {state.character_states.length === 0 && <div className="muted">none yet</div>}
            </div>
            <div>
              <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                Past events ({state.past_events.length})
              </div>
              {state.past_events
                .slice()
                .reverse()
                .slice(0, 12)
                .map((entry) => (
                  <div key={entry.id} style={{ marginBottom: 6, fontSize: 12.5 }}>
                    <span className="muted">#{entry.id} {entry.key ?? ""}</span> — {entry.description}
                  </div>
                ))}
              {state.past_events.length > 12 && <div className="muted">…{state.past_events.length - 12} more</div>}
            </div>
            <div>
              <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                Open obligations ({state.future_requirements.length})
              </div>
              {state.future_requirements.map((entry) => (
                <div key={entry.id} style={{ marginBottom: 6, fontSize: 12.5 }}>
                  <span className="chip">{entry.key}</span> {entry.description}
                </div>
              ))}
              {state.future_requirements.length === 0 && <div className="muted">none open</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}