import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Book, Profile, WizardState, WizardStepKind, WizardStepConfig } from "../types";
import WizardProgressBoard, { wizardStepLabel, overallPercent } from "./WizardProgressBoard";
import WizardLivePane, { wizardTargetLanguage } from "./WizardLivePane";

const STEP_KINDS: WizardStepKind[] = [
  "outline",
  "outline_refine",
  "write",
  "text_refine",
  "translate",
  "translate_refine",
];

function defaultStepConfigs(): WizardStepConfig[] {
  return [
    { step: 1, kind: "outline", enabled: true, planner_profile_id: "" },
    { step: 2, kind: "outline_refine", enabled: true, candidate_profile_ids: [], judge_profile_id: "", parallel: false, refine_rounds: 1, similarity_stop: 0.95, context_prev: 3 },
    { step: 3, kind: "write", enabled: true, writer_profile_id: "" },
    { step: 4, kind: "text_refine", enabled: true, candidate_profile_ids: [], judge_profile_id: "", parallel: false, refine_rounds: 1, similarity_stop: 0.97, draft_temperature: 0.7, style_probe: "" },
    { step: 5, kind: "translate", enabled: true, draft_profile_id: "", refiner_profile_id: "", checker_profile_id: "", target_language: "German", batch_words: 250, style_probe: "" },
    { step: 6, kind: "translate_refine", enabled: true, candidate_profile_ids: [], judge_profile_id: "", parallel: false, refine_rounds: 1 },
  ];
}

type CodexMinRow = { name: string; aliases: string; description: string; role: string };

function emptyCodexRows(): { characters: CodexMinRow[]; settings: CodexMinRow[] } {
  return {
    characters: [{ name: "", aliases: "", description: "", role: "character" }],
    settings: [],
  };
}

function SingleProfileSelect({
  value,
  onChange,
  profiles,
  emptyLabel,
}: {
  value: string;
  onChange: (id: string) => void;
  profiles: Profile[];
  emptyLabel: string;
}) {
  return (
    <select value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">— {emptyLabel} —</option>
      {profiles.map((profile) => (
        <option key={profile.id} value={profile.id} title={`${profile.model}`}>
          {profile.name || profile.model} ({profile.model})
        </option>
      ))}
    </select>
  );
}

function CandidateSelect({
  ids,
  onChange,
  profiles,
  max = 6,
}: {
  ids: string[];
  onChange: (ids: string[]) => void;
  profiles: Profile[];
  max?: number;
}) {
  return (
    <>
      <select
        multiple
        size={Math.min(Math.max(profiles.length + 1, 3), 7)}
        value={ids}
        style={{ width: "100%" }}
        onChange={(event) =>
          onChange(Array.from(event.target.selectedOptions, (option) => option.value).slice(0, max))
        }
      >
        {profiles.map((profile) => (
          <option key={profile.id} value={profile.id} title={`${profile.model}`}>
            {profile.name || profile.model} ({profile.model})
          </option>
        ))}
      </select>
      <span className="muted" style={{ fontSize: 11 }}>
        {ids.length}/{max} gewählt — Strg/⌘-Klick für mehrere
      </span>
    </>
  );
}

function WizardConfigScreen({
  book,
  profiles,
  starting,
  onStart,
}: {
  book: Book;
  profiles: Profile[];
  starting: boolean;
  onStart: (payload: any) => void;
}) {
  const [title, setTitle] = useState(book.title || "");
  const [storyPrompt, setStoryPrompt] = useState(book.story_prompt || "");
  const [targetWords, setTargetWords] = useState<number>(book.target_words || 10000);
  const [genreNotes, setGenreNotes] = useState(book.genre_notes || "");
  const [attachExisting, setAttachExisting] = useState(false);
  const [characters, setCharacters] = useState<CodexMinRow[]>(
    (book.codex?.characters ?? []).length
      ? book.codex!.characters.map((row) => ({
          name: row.name ?? "",
          aliases: (row.aliases ?? []).join(", "),
          description: row.description ?? "",
          role: row.role ?? "character",
        }))
      : emptyCodexRows().characters,
  );
  const [settings, setSettings] = useState<CodexMinRow[]>(
    (book.codex?.settings ?? []).map((row) => ({
      name: row.name ?? "",
      aliases: (row.aliases ?? []).join(", "),
      description: row.description ?? "",
      role: "",
    })),
  );
  const [steps, setSteps] = useState<WizardStepConfig[]>(() => {
    const rows = defaultStepConfigs();
    // Schritte 5+6 (Übersetzung) sind Standard: AN — der Nutzer entscheidet
    // über den Toggle ("Danach übersetzen"), nicht über eine Buch-Sprache
    return rows;
  });
  const [pauseBetween, setPauseBetween] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const translateAfter = !!steps.find((row) => row.kind === "translate")?.enabled;

  const setTranslateAfter = (next: boolean) =>
    setSteps((prev) =>
      prev.map((row) =>
        row.kind === "translate" || row.kind === "translate_refine" ? { ...row, enabled: next } : row,
      ),
    );

  const patchStep = (kindOrStep: WizardStepKind | number, patch: Partial<WizardStepConfig>) =>
    setSteps((prev) =>
      prev.map((row) =>
        typeof kindOrStep === "number" ? (row.step === kindOrStep ? { ...row, ...patch } : row) : row.kind === kindOrStep ? { ...row, ...patch } : row,
      ),
    );

  const validate = (): string | null => {
    if (!attachExisting) {
      if (!title.trim()) return "Bitte einen Buchtitel angeben.";
      if (!storyPrompt.trim()) return "Bitte die Buchidee (Story-Prompt) angeben.";
    }
    const enabled = steps.filter((row) => row.enabled);
    if (!enabled.length) return "Bitte mindestens einen Schritt aktivieren.";
    for (const row of steps) {
      if (!row.enabled) continue;
      if (row.kind === "outline" && !row.planner_profile_id) return `Schritt 1: Planner-Profil wählen.`;
      if (row.kind === "write" && !row.writer_profile_id) return `Schritt 3: Writer-Profil wählen.`;
      if (row.kind === "translate") {
        if (!row.draft_profile_id || !row.refiner_profile_id) return "Schritt 5: Übersetzungs-Entwurfs- und Verfeinerungs-Profil wählen.";
      }
      if (row.kind === "outline_refine" || row.kind === "text_refine" || row.kind === "translate_refine") {
        if (!(row.candidate_profile_ids ?? []).length) return `Schritt ${row.step}: mindestens 1 Kandidaten-Profil wählen.`;
        if ((row.candidate_profile_ids ?? []).length > 6) return `Schritt ${row.step}: maximal 6 Kandidaten.`;
      }
    }
    const enabledKinds = new Set(enabled.map((row) => row.kind));
    const prerequisites: [WizardStepKind, WizardStepKind][] = [
      ["outline_refine", "outline"],
      ["write", "outline"],
      ["text_refine", "write"],
      ["translate", "write"],
      ["translate_refine", "translate"],
    ];
    for (const [kind, prerequisite] of prerequisites)
      if (enabledKinds.has(kind) && !enabledKinds.has(prerequisite))
        return `Schritt (${wizardStepLabel(STEP_KINDS.indexOf(kind) + 1)}) braucht den Schritt ${wizardStepLabel(STEP_KINDS.indexOf(prerequisite) + 1)} zuerst.`;
    return null;
  };

  const start = () => {
    const problem = validate();
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    const codexRow = (rows: CodexMinRow[]) =>
      rows
        .filter((row) => row.name.trim())
        .map((row) => ({
          name: row.name.trim(),
          aliases: row.aliases.split(",").map((alias) => alias.trim()).filter(Boolean),
          description: row.description.trim(),
          role: row.role.trim() || "character",
          color: "",
        }));
    const codex_min = { characters: codexRow(characters), settings: codexRow(settings.map((row) => (row.role = row.role || "", row))), lore: [] };
    const payload = {
      book_id: attachExisting ? book.id : null,
      book: attachExisting
        ? // the book's own title/idea/codex are used — nothing gets copied
          { title: book.title || "", story_prompt: book.story_prompt || "", language: "en",
            target_words: book.target_words || 0, genre_notes: "",
            codex_min: { characters: [], settings: [], lore: [] } }
        : {
            title: title.trim(),
            story_prompt: storyPrompt.trim(),
            language: "en", // Wizard-Bücher sind immer englisch — der Übersettings-Toggle steuert nur Schritte 5/6
            target_words: Math.max(100, Number(targetWords) || 10000),
            genre_notes: genreNotes.trim(),
            codex_min: { characters: codexRow(characters), settings: codexRow(settings), lore: [] },
          },
      steps: steps.map((row) => ({
        ...row,
        planner_profile_id: row.planner_profile_id || null,
        writer_profile_id: row.writer_profile_id || null,
        judge_profile_id: row.judge_profile_id || null,
        draft_profile_id: row.draft_profile_id || null,
        refiner_profile_id: row.refiner_profile_id || null,
        checker_profile_id: row.checker_profile_id || null,
        draft_temperature: row.kind === "text_refine" ? Number(row.draft_temperature ?? 0.7) : null,
        candidate_profile_ids: row.candidate_profile_ids ?? [],
      })),
      pause_between_steps: pauseBetween,
    };
    onStart(payload);
  };

  const hiddenTranslation = !translateAfter;

  return (
    <div>
      <div className="card">
        <strong style={{ fontSize: 15 }}>🧙 Buch-Wizard</strong>
        <p className="inline-note">
          Das Buch wird <b>immer auf Englisch</b> geplant und geschrieben. Danach wahlweise automatisch ins Deutsche
          übersetzt (Schritte 5 + 6): CHAIN-Kette Outline → Kappenverfeinerung → Schreiben → Textverfeinerung →
          [Übersetzung → Deutsch-Verfeinerung].
        </p>
        <label
          style={{
            display: "flex", gap: 9, alignItems: "center", margin: "2px 0 12px",
            border: "1px solid var(--border)", borderRadius: 8, padding: "10px 12px", background: "var(--bg-soft)",
            color: "var(--text)", cursor: "pointer",
          }}
        >
          <input
            type="checkbox"
            checked={attachExisting}
            onChange={(event) => setAttachExisting(event.target.checked)}
          />
          <span>
            <strong>An DIES Buch hängen</strong>
            <span className="muted" style={{ display: "block", fontSize: 12 }}>
              an = der Wizard läuft für das aktuelle Buch (Titel, Idee und Codex bleiben wie sie sind — Resume/Retry ohne
              neue Kopien). aus = es wird ein NEUES Buch aus den Eingaben unten angelegt.
            </span>
          </span>
        </label>

        <label
          style={{
            display: "flex", gap: 9, alignItems: "center", margin: "2px 0 12px",
            border: "1px solid var(--border)", borderRadius: 8, padding: "10px 12px", background: "var(--bg-soft)",
            color: "var(--text)", cursor: "pointer",
          }}
        >
          <input
            type="checkbox"
            checked={translateAfter}
            onChange={(event) => setTranslateAfter(event.target.checked)}
          />
          <span>
            <strong>Deutsch-Übersetzung danach</strong>
            <span className="muted" style={{ display: "block", fontSize: 12 }}>
              an = Schritt 5 (Übersetzung nach Deutsch) + Schritt 6 (Verfeinerung der deutschen Fassung) laufen nach dem
              Schreiben automatisch. aus = das Buch bleibt englisch, Schritte 5/6 werden übersprungen.
            </span>
          </span>
        </label>

        {!attachExisting ? (
          <>
            <div className="form-row">
              <div className="form-col">
                <label>Buchtitel (Pflicht)</label>
                <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Titel des Buchs" />
              </div>
              <div className="form-col">
                <label>Ziel-Wortzahl</label>
                <input
                  type="number"
                  min={300}
                  step={100}
                  value={targetWords}
                  onChange={(event) => setTargetWords(Number(event.target.value))}
                />
              </div>
            </div>
            <div className="form-col" style={{ marginBottom: 12 }}>
              <label>Idee / Story-Prompt (Pflicht)</label>
              <textarea
                value={storyPrompt}
                onChange={(event) => setStoryPrompt(event.target.value)}
                rows={4}
                placeholder="Premise, Ton, Umfang, besondere Anforderungen — so detailliert wie möglich"
              />
            </div>
            <div className="form-col" style={{ marginBottom: 12 }}>
              <label>Genre- und Stil-Vorgaben (optional)</label>
              <textarea value={genreNotes} onChange={(event) => setGenreNotes(event.target.value)} rows={2} />
            </div>
          </>
        ) : (
          <div className="inline-note" style={{ marginBottom: 12, color: "var(--text)" }}>
            Bindet den Wizard an <b>{book.title}</b> — es wird nichts neu angelegt und nichts kopiert. Bereits vorhandene
            Outline/Kapitel nutzt der Wizard als Fortsetzungsstand (Schritt 1 friert lediglich, Schritt 3 schreibt nur
            fehlende Kapitel).
          </div>
        )}
        <div className="form-col" style={{ marginBottom: 12 }}>
          <label>Idee / Story-Prompt (Pflicht)</label>
          <textarea
            value={storyPrompt}
            onChange={(event) => setStoryPrompt(event.target.value)}
            rows={4}
            placeholder="Premise, Ton, Umfang, besondere Anforderungen — so detailliert wie möglich"
          />
        </div>
        <div className="form-col" style={{ marginBottom: 12 }}>
          <label>Genre- und Stil-Vorgaben (optional)</label>
          <textarea value={genreNotes} onChange={(event) => setGenreNotes(event.target.value)} rows={2} />
        </div>

        <strong style={{ fontSize: 12.5 }}>Codex-Minimum (optional)</strong>
        <p className="inline-note">
          Minimale Einträge, die der Planner sofort verfügbar braucht (Kernfiguren + Welt/Setting).
        </p>
        {[...characters.map((row, index) => ({ row, index, kind: "characters" as const })), ...settings.map((row, index) => ({ row, index, kind: "settings" as const }))].map(
          ({ row, index, kind }) => (
            <div className="settings-row" key={`${kind}-${index}`}>
              <input placeholder={`Name (${kind === "characters" ? "Figur" : "Setting"})`} value={row.name} onChange={(event) => {
                const rows = kind === "characters" ? characters : settings;
                const next = rows.map((r, i) => (i === index ? { ...r, name: event.target.value } : r));
                kind === "characters" ? setCharacters(next) : setSettings(next);
              }} />
              <input placeholder="Aliasse (Komma)" value={row.aliases} onChange={(event) => {
                const rows = kind === "characters" ? characters : settings;
                const next = rows.map((r, i) => (i === index ? { ...r, aliases: event.target.value } : r));
                kind === "characters" ? setCharacters(next) : setSettings(next);
              }} />
              <input placeholder="Kurzbeschreibung" value={row.description} onChange={(event) => {
                const rows = kind === "characters" ? characters : settings;
                const next = rows.map((r, i) => (i === index ? { ...r, description: event.target.value } : r));
                kind === "characters" ? setCharacters(next) : setSettings(next);
              }} />
              {kind === "characters" ? (
                <input placeholder="Rolle" value={row.role} onChange={(event) => {
                  const next = characters.map((r, i) => (i === index ? { ...r, role: event.target.value } : r));
                  setCharacters(next);
                }} />
              ) : (
                <span className="muted">—</span>
              )}
              <button className="btn-danger btn-ghost" style={{ padding: "3px 8px" }} onClick={() => {
                if (kind === "characters") setCharacters(characters.filter((_, i) => i !== index));
                else setSettings(settings.filter((_, i) => i !== index));
              }}>
                ✕
              </button>
            </div>
          ),
        )}
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button className="btn-ghost" onClick={() => setCharacters([...characters, { name: "", aliases: "", description: "", role: "character" }])}>
              + Figur
          </button>
          <button className="btn-ghost" onClick={() => setSettings([...settings, { name: "", aliases: "", description: "", role: "" }])}>
              + Setting
          </button>
        </div>
      </div>

      <div className="card">
        <strong style={{ fontSize: 15 }}>Schritt-Konfiguration*</strong>
        <p className="inline-note">
          Reihenfolge fix: 1 Outline → 2 Kappenverfeinerung (Kandidaten + Judge) → 3 Schreiben → 4 Textverfeinerung →
          5 Übersetzung → 6 Deutsch-Verfeinerung. CHAIN: Schritt 4 nutzt die Versionen von Schritt 3, Schritt 5 nutzt
          die Versionen von Schritt 4, Schritt 6 nutzt die Übersetzung von Schritt 5.
        </p>

        {steps.map((row) => {
          const hidden = hiddenTranslation && (row.kind === "translate" || row.kind === "translate_refine");
          if (hidden)
            return (
              <div key={row.step} className="wizard-step-config" style={{ opacity: 0.55 }}>
                <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <input type="checkbox" checked={false} disabled />
                  <strong style={{ fontSize: 12.5 }}>
                    S{row.step} · {wizardStepLabel(row.step)}
                  </strong>
                  <span className="muted">skipped — keine Übersetzung gewünscht (Toggle oben)</span>
                </div>
              </div>
            );
          const candidateCount = (row.candidate_profile_ids ?? []).length;
          return (
            <div key={row.step} className="wizard-step-config">
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 8 }}>
                <input
                  type="checkbox"
                  checked={row.enabled}
                  onChange={(event) => patchStep(row.step, { enabled: event.target.checked })}
                />
                <strong style={{ fontSize: 12.5 }}>
                  S{row.step} · {wizardStepLabel(row.step)}
                </strong>
              </div>

              <div className="wizard-grid">
                {row.kind === "outline" ? (
                  <div className="form-col" style={{ maxWidth: 340 }}>
                    <label>Planner-Profil*</label>
                    <SingleProfileSelect value={row.planner_profile_id ?? ""} onChange={(id) => patchStep(row.step, { planner_profile_id: id })} profiles={profiles} emptyLabel=" Profil wählen" />
                  </div>
                ) : null}
                {row.kind === "write" ? (
                  <>
                    <div className="form-col" style={{ maxWidth: 340 }}>
                      <label>Writer-Profil*</label>
                      <SingleProfileSelect value={row.writer_profile_id ?? ""} onChange={(id) => patchStep(row.step, { writer_profile_id: id })} profiles={profiles} emptyLabel=" Profil wählen" />
                    </div>
                    <div className="form-col" style={{ flex: "1 1 200px", maxWidth: 280 }}>
                      <label>Laufmodus</label>
                      <input value="auto (Wizard erzwingt automatisch)" disabled />
                    </div>
                  </>
                ) : null}
                {row.kind === "translate" ? (
                  <>
                    <div className="form-col" style={{ maxWidth: 340 }}>
                      <label>Entwurfs-Profil*</label>
                      <SingleProfileSelect value={row.draft_profile_id ?? ""} onChange={(id) => patchStep(row.step, { draft_profile_id: id })} profiles={profiles} emptyLabel=" Profil wählen" />
                    </div>
                    <div className="form-col" style={{ maxWidth: 340 }}>
                      <label>Verfeinerungs-Profil*</label>
                      <SingleProfileSelect value={row.refiner_profile_id ?? ""} onChange={(id) => patchStep(row.step, { refiner_profile_id: id })} profiles={profiles} emptyLabel=" Profil wählen" />
                    </div>
                    <div className="form-col" style={{ maxWidth: 340 }}>
                      <label>Checker (optional)</label>
                      <SingleProfileSelect value={row.checker_profile_id ?? ""} onChange={(id) => patchStep(row.step, { checker_profile_id: id })} profiles={profiles} emptyLabel="kein Checker" />
                    </div>
                    <div className="form-col" style={{ flex: "1 1 140px", maxWidth: 160 }}>
                      <label>Zielsprache</label>
                      <input value={row.target_language ?? "German"} onChange={(event) => patchStep(row.step, { target_language: event.target.value })} />
                    </div>
                    <div className="form-col" style={{ flex: "1 1 120px", maxWidth: 140 }}>
                      <label>Wörter/Block</label>
                      <input type="number" min={50} step={25} value={row.batch_words ?? 250} onChange={(event) => patchStep(row.step, { batch_words: Number(event.target.value) })} />
                    </div>
                    <div className="form-col" style={{ flex: "1 1 260px", maxWidth: 400 }}>
                      <label>Stil-Probe (optional)</label>
                      <input value={row.style_probe ?? ""} onChange={(event) => patchStep(row.step, { style_probe: event.target.value })} />
                    </div>
                  </>
                ) : null}
                {row.kind !== "outline" && row.kind !== "write" && row.kind !== "translate" ? (
                  <div className="form-col" style={{ maxWidth: 340 }}>
                    <label>Kandidaten-Profile* {candidateCount >= 2 ? "(Ensemble)" : ""}</label>
                    <CandidateSelect ids={row.candidate_profile_ids ?? []} onChange={(ids) => patchStep(row.step, { candidate_profile_ids: ids })} profiles={profiles} />
                  </div>
                ) : null}
                {row.kind !== "outline" && row.kind !== "write" && row.kind !== "translate" ? (
                  <div className="form-col" style={{ maxWidth: 340 }}>
                    <label>Judge (optional)</label>
                    <SingleProfileSelect value={row.judge_profile_id ?? ""} onChange={(id) => patchStep(row.step, { judge_profile_id: id })} profiles={profiles} emptyLabel="kein Judge — Kandidat 1 gewinnt" />
                  </div>
                ) : null}
              </div>

              {row.kind === "outline_refine" || row.kind === "text_refine" || row.kind === "translate_refine" ? (
                <div className="wizard-grid">
                  <div className="form-col" style={{ flex: "1 1 150px", maxWidth: 150 }}>
                    <label>Verfeinerungsrunden</label>
                    <select value={String(row.refine_rounds ?? 1)} onChange={(event) => patchStep(row.step, { refine_rounds: Number(event.target.value) })}>
                      <option value="1">1 (nur Entwurf)</option>
                      <option value="2">2</option>
                      <option value="3">3</option>
                    </select>
                  </div>
                  <div className="form-col" style={{ flex: "1 1 150px", maxWidth: 150 }}>
                    <label>Semantik-Stopp (0.5–1.0)</label>
                    <input type="number" min={0.5} max={1} step={0.01} value={row.similarity_stop ?? 0.95} onChange={(event) => patchStep(row.step, { similarity_stop: Number(event.target.value) })} />
                  </div>
                  {row.kind === "outline_refine" ? (
                    <div className="form-col" style={{ flex: "1 1 160px", maxWidth: 160 }}>
                      <label>Vorige Briefe (Kontext)</label>
                      <input type="number" min={0} max={50} value={row.context_prev ?? 3} onChange={(event) => patchStep(row.step, { context_prev: Number(event.target.value) })} />
                    </div>
                  ) : null}
                  {row.kind === "text_refine" ? (
                    <>
                      <div className="form-col" style={{ flex: "1 1 150px", maxWidth: 150 }}>
                        <label>Entwurfs-Temperatur</label>
                        <input type="number" min={0} max={2} step={0.05} value={row.draft_temperature ?? 0.7} onChange={(event) => patchStep(row.step, { draft_temperature: Number(event.target.value) })} />
                      </div>
                      <div className="form-col" style={{ flex: "1 1 260px", maxWidth: 400 }}>
                        <label>Stil-Probe (optional)</label>
                        <input value={row.style_probe ?? ""} onChange={(event) => patchStep(row.step, { style_probe: event.target.value })} />
                      </div>
                    </>
                  ) : null}
                  <div className="form-col" style={{ flex: "1 1 220px", maxWidth: 280 }}>
                    <label>&nbsp;</label>
                    <label style={{ display: "flex", gap: 7, alignItems: "center", color: "var(--text)" }}>
                      <input
                        type="checkbox"
                        checked={!!row.parallel}
                        onChange={(event) => patchStep(row.step, { parallel: event.target.checked })}
                        disabled={candidateCount < 2}
                      />
                      Parallel-Aufrufe
                      {candidateCount < 2 ? <span className="muted">(erst ab 2 Kandidaten)</span> : null}
                    </label>
                  </div>
                </div>
              ) : null}
            </div>
          );
        })}

        <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
          <input type="checkbox" checked={pauseBetween} onChange={(event) => setPauseBetween(event.target.checked)} />
          Pause nach jedem Schritt (Standard: durchlaufend, nach jedem Schritt persistiert)
        </label>
      </div>

      {error ? <div className="error-banner">{error}</div> : null}
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <button className="btn-primary" onClick={start} disabled={starting}>
          {starting ? "Wizard startet…" : "🧙 Wizard-Buch anlegen & starten"}
        </button>
        <span className="muted" style={{ fontSize: 12 }}>
          legt ein neues Buch an; Profile mit fehlendem Model-Eintrag werden beim Start geprüft.
        </span>
      </div>
    </div>
  );
}

function WizardConfigEditor({
  wizard,
  profiles,
  busy,
  onSave,
}: {
  wizard: WizardState;
  profiles: Profile[];
  busy: boolean;
  onSave: (patch: { steps: Record<string, any>[] }) => void;
}) {
  const [draft, setDraft] = useState<Record<number, Partial<WizardStepConfig>>>({});
  const lastConfigRef = useRef<string>("");

  const configJson = JSON.stringify(wizard.config.steps);
  useEffect(() => {
    // reset the draft only when the SAVED config actually changed (own save or
    // another patch) — normal wizard polling must never wipe user edits
    if (lastConfigRef.current && lastConfigRef.current !== configJson) setDraft({});
    lastConfigRef.current = configJson;
  }, [configJson]);

  const merged = (step: number): Partial<WizardStepConfig> => ({
    ...(wizard.config.steps.find((row) => row.step === step) ?? { step, kind: STEP_KINDS[step - 1] } as WizardStepConfig),
    ...(draft[step] ?? {}),
  });

  const setField = (step: number, key: string, value: any) =>
    setDraft((prev) => ({ ...prev, [step]: { ...(prev[step] ?? {}), [key]: value } }));

  const stepRows = wizard.config.steps;
  const dirtySteps = stepRows
    .filter((row) => Object.keys(draft[row.step] ?? {}).length > 0)
    .map((row) => row.step);

  const save = () => {
    const patchSteps = dirtySteps.map((step) => ({ step, ...draft[step] }));
    if (!patchSteps.length) return;
    onSave({ steps: patchSteps }); // draft is dropped by the config-change effect once persisted
  };

  const singleField = (
    step: number,
    key: keyof WizardStepConfig,
    label: string,
  ) => (
    <div className="form-col" style={{ minWidth: 220 }}>
      <label>{label}</label>
      <SingleProfileSelect
        value={String((merged(step) as any)[key] ?? "")}
        onChange={(id) => setField(step, key as string, id)}
        profiles={profiles}
        emptyLabel="kein Profil gesetzt"
      />
    </div>
  );

  const candidateFields = (step: number) => {
    const row = merged(step);
    return (
      <>
        <div className="form-col" style={{ minWidth: 240 }}>
          <label>Kandidaten (Schritt läuft über alle, Judge gewinnt)</label>
          <CandidateSelect
            ids={(row as any).candidate_profile_ids ?? []}
            onChange={(ids) => setField(step, "candidate_profile_ids", ids)}
            profiles={profiles}
          />
        </div>
        {singleField(step, "judge_profile_id" as keyof WizardStepConfig, "Judge")}
        <div className="form-col" style={{ flex: "1 1 150px", maxWidth: 150 }}>
          <label>Runden</label>
          <input
            type="number"
            min={1}
            max={3}
            step={1}
            value={(row as any).refine_rounds ?? ""}
            onChange={(e) => setField(step, "refine_rounds", e.target.value === "" ? "" : Number(e.target.value))}
            onBlur={(e) => {
              const raw = Number(e.target.value);
              if (!raw || raw < 1) setField(step, "refine_rounds", 1);
            }}
          />
        </div>
        <div className="form-col" style={{ flex: "1 1 260px", maxWidth: 360 }}>
          <label>Parallel-Blöcke</label>
          <input
            type="number"
            min={1}
            max={8}
            step={1}
            value={(row as any).parallel_blocks ?? ""}
            onChange={(e) => setField(step, "parallel_blocks", e.target.value === "" ? "" : Number(e.target.value))}
            onBlur={(e) => {
              const raw = Number(e.target.value);
              if (!raw || raw < 1) setField(step, "parallel_blocks", 1);
            }}
          />
          <div className="inline-note" style={{ fontSize: 10.5 }}>
            1 = seriell (Tonanker aus dem verfeinerten Vorgängerblock). Mehr als 1 = Kapitel gleichzeitig (je Worker
            ein Kapitel) — der Tonanker kommt dann aus der Original-Prosa. Eingaben werden beim Speichern begrenzt
            (Runden 1-3, Blöcke 1-8).
          </div>
        </div>
      </>
    );
  };

  const fieldsFor = (step: number, kind: WizardStepKind) => {
    switch (kind) {
      case "outline":
        return <>{singleField(step, "planner_profile_id", "Planner-Profil")}</>;
      case "outline_refine":
        return <>{candidateFields(step)}</>;
      case "write":
        return <>{singleField(step, "writer_profile_id", "Writer-Profil")}</>;
      case "text_refine":
        return <>{candidateFields(step)}</>;
      case "translate":
        return (
          <>
            {singleField(step, "draft_profile_id", "Draft-Profil")}
            {singleField(step, "refiner_profile_id", "Refiner-Profil")}
            {singleField(step, "checker_profile_id", "Checker-Profil")}
          </>
        );
      case "translate_refine":
        return <>{candidateFields(step)}</>;
      default:
        return null;
    }
  };

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <div>
          <strong style={{ fontSize: 15 }}>⚙ Konfiguration ändern — LLMs pro Schritt</strong>
          <div className="inline-note">
            Wirkt beim (Neu-)Start des Schritts — nie mitten im Schritt. Für bereits <b>erledigte</b> Schritte: nach
            dem Speichern oben in der Schrittzeile ↻ <b>Retry</b> drücken (neue Versionen landen zusätzlich, alte
            bleiben). Pausieren/Stoppen vor dem Speichern, solange der Wizard läuft.
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {dirtySteps.length > 0 && (
            <button onClick={() => setDraft({})} disabled={busy}>
              Verwerfen
            </button>
          )}
          <button className="btn-primary" disabled={busy || dirtySteps.length === 0} onClick={save}>
            {dirtySteps.length ? `Speichern (Schritt ${dirtySteps.join(", ")})` : "Keine Änderungen"}
          </button>
        </div>
      </div>
      <div className="wizard-steps" style={{ marginTop: 8 }}>
        {stepRows.map((row) => {
          const stepStatus = wizard.steps_state.find((state) => state.step === row.step)?.status;
          const dirty = (draft[row.step] ?? {}) as Record<string, unknown>;
          const isDirty = Object.keys(dirty).length > 0;
          return (
            <div key={row.step} className="wizard-config-step">
              <div className="wizard-config-head">
                <strong style={{ fontSize: 12.5 }}>
                  S{row.step} · {wizardStepLabel(row.step)}
                  {isDirty ? " *" : ""}
                </strong>
                <span className="muted" style={{ fontSize: 11 }}>
                  {row.kind}
                  {stepStatus === "done" ? " · erledigt — Retry nötig" : ""}
                </span>
              </div>
              <div className="wizard-grid">{fieldsFor(row.step, row.kind)}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function WizardTab({
  book,
  onOpen,
  showToast,
  events,
}: {
  book: Book;
  onOpen: (id: string, tab?: string) => void;
  showToast: (message: string) => void;
  events: any[];
}) {
  const [wizard, setWizard] = useState<WizardState | null | undefined>(undefined);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const [lastHistoryCount, setLastHistoryCount] = useState(0);

  useEffect(() => {
    api.listProfiles().then((result) => setProfiles(result.profiles)).catch(() => {});
  }, []);

  const load = () =>
    api.wizardState(book.id).then((result) => setWizard(result.wizard_state)).catch(() => setWizard(null));

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 2000);
    return () => window.clearInterval(timer);
  }, [book.id]);

  const running = wizard?.status === "running" || wizard?.status === "pausing" || wizard?.status === "stopping";
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(load, 1200);
    return () => window.clearInterval(timer);
  }, [running, book.id]);

  useEffect(() => {
    const history = wizard?.history ?? [];
    if (history.length > lastHistoryCount) {
      const latest = history[history.length - 1];
      if (latest && (lastHistoryCount > 0 || history.length > 1)) showToast(`Wizard: ${latest.event}`);
      setLastHistoryCount(history.length);
    }
  }, [wizard?.updated]);

  const control = async (action: () => Promise<any>) => {
    setBusy(true);
    try {
      const result = await action();
      if (result?.wizard_state !== undefined) setWizard(result.wizard_state);
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setBusy(false);
    }
  };

  const start = async (payload: any) => {
    setStarting(true);
    try {
      const created = await api.wizardCreate(payload);
      showToast(`Wizard-Buch "${created.book.title}" angelegt`);
      onOpen(created.book.id, "wizard");
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setStarting(false);
    }
  };

  if (wizard === undefined) return <div className="muted">Wizard lädt…</div>;
  if (wizard === null)
    return <WizardConfigScreen book={book} profiles={profiles} starting={starting} onStart={start} />;

  return (
    <div>
      <WizardProgressBoard
        state={wizard}
        busy={busy}
        onPause={() => control(() => api.wizardPause(book.id))}
        onResume={() => control(() => api.wizardResume(book.id))}
        onStop={() => control(() => api.wizardStop(book.id))}
        onRetryStep={(step) => control(() => api.wizardRetryStep(book.id, step))}
        onTogglePauseBetweenSteps={() => {
          control(() => api.wizardPatchConfig(book.id, { pause_between_steps: !wizard.config.pause_between_steps }));
        }}
      />
      <WizardConfigEditor
        wizard={wizard}
        profiles={profiles}
        busy={busy}
        onSave={(patch) => control(() => api.wizardPatchConfig(book.id, patch))}
      />
      <div className="card">
        <WizardLivePane book={{ id: book.id, title: book.title }} state={wizard} events={events} />
      </div>
      <div className="card">
        <strong style={{ fontSize: 13 }}>Verlauf</strong>
        <div style={{ marginTop: 6, display: "grid", gap: 3, maxHeight: 240, overflowY: "auto" }}>
          {wizard.history
            .slice()
            .reverse()
            .slice(0, 60)
            .map((entry, index) => (
              <div key={index} style={{ fontSize: 12.5 }}>
                <span className="muted">{entry.at.slice(11, 19)}</span> {entry.event}
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}