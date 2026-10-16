import { useEffect, useState } from "react";
import { api } from "../api";
import type { Book, SequelProposal } from "../types";

export default function SequelDialog({
  book,
  onDone,
  onClose,
  showToast,
}: {
  book: Book;
  onDone: (newBookId: string) => void;
  onClose: () => void;
  showToast: (m: string) => void;
}) {
  const [profiles, setProfiles] = useState<{ id: string; name: string; model: string }[]>([]);
  const [plannerId, setPlannerId] = useState<string>(book.planner_profile_id ?? "");
  const [idea, setIdea] = useState("");
  const [proposal, setProposal] = useState<SequelProposal | null>(null);
  const [keep, setKeep] = useState<Set<string>>(new Set());
  const [alive, setAlive] = useState<Record<string, boolean | null>>({});
  const [targetWords, setTargetWords] = useState<number>(book.target_words);
  const [includeDead, setIncludeDead] = useState(false);
  const [step, setStep] = useState<"input" | "review">("input");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .listProfiles()
      .then((r) => setProfiles(r.profiles))
      .catch(() => {});
  }, []);

  const prepare = async () => {
    if (!idea.trim()) {
      showToast("Give an idea for Band 2 first");
      return;
    }
    if (!plannerId) {
      showToast("Select a planner profile for the extraction");
      return;
    }
    setBusy(true);
    try {
      const result = await api.sequelPrepare(book.id, {
        idea: idea.trim(),
        planner_profile_id: plannerId,
      });
      const p = result.proposal;
      setProposal(p);
      setAlive(Object.fromEntries(p.characters.map((c) => [c.name, c.alive])));
      // Vorauswahl: lebend ✓, tot ✗, unklar ✗
      setKeep(new Set(p.characters.filter((c) => c.alive === true).map((c) => c.name)));
      if (p.extraction_error) showToast(p.extraction_error);
      setStep("review");
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setBusy(false);
    }
  };

  const toggleKeep = (name: string) => {
    setKeep((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const setAliveValue = (name: string, value: boolean | null) => {
    setAlive((prev) => ({ ...prev, [name]: value }));
    setProposal((prev) =>
      prev
        ? { ...prev, characters: prev.characters.map((c) => (c.name === name ? { ...c, alive: value } : c)) }
        : prev,
    );
  };

  const create = async () => {
    if (!proposal) return;
    setBusy(true);
    try {
      const result = await api.sequelCreate(book.id, {
        proposal,
        keep_character_names: Array.from(keep),
        idea: idea.trim(),
        target_words: targetWords,
        include_dead_as_flashbacks: includeDead,
      });
      showToast(`Band 2 created: ${result.book.title}`);
      onDone(result.book.id);
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ width: 760 }}>
        {step === "input" ? (
          <>
            <h3 style={{ marginTop: 0 }}>Fortsetzung vorbereiten — „Band 2"</h3>
            <div className="inline-note">
              Das System extrahiert aus Band 1 (letzte Kapitel + Codex): Ende, lebende Personen,
              offene Fäden — und baut daraus dein nahtlos anschließendes Band-2-Buch. Band 1 bleibt
              unverändert.
            </div>
            <div className="form-col" style={{ marginBottom: 12 }}>
              <label>Idee für Band 2 (Pflichtfeld)</label>
              <textarea
                rows={4}
                value={idea}
                onChange={(e) => setIdea(e.target.value)}
                placeholder="Was treibt Band 2 an? Ein neuer Konflikt, die Folgen des Endes, ein zurückgekehrter Gegenspieler…"
              />
            </div>
            <div className="form-row">
              <div className="form-col">
                <label>Planner-Profil (Extraktion, Temp 0)</label>
                <select value={plannerId} onChange={(e) => setPlannerId(e.target.value)}>
                  <option value="">— select profile —</option>
                  {profiles.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} ({p.model})
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-col">
                <label>Target length Band 2 (default = Band 1)</label>
                <input
                  type="number"
                  min={1000}
                  max={500000}
                  step={1000}
                  value={targetWords}
                  onChange={(e) => setTargetWords(Number(e.target.value) || book.target_words)}
                />
              </div>
            </div>
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 10 }}>
              <button onClick={onClose}>Cancel</button>
              <button className="btn-primary" disabled={busy} onClick={prepare}>
                {busy ? "Extracting…" : "Vorbereiten"}
              </button>
            </div>
          </>
        ) : (
          proposal && (
            <>
              <h3 style={{ marginTop: 0 }}>Vorschlag prüfen — Band 2</h3>
              {proposal.extraction_error && (
                <div className="error-banner">{proposal.extraction_error}</div>
              )}
              <div className="form-col" style={{ marginBottom: 12 }}>
                <label>Endzusammenfassung Band 1 (5-8 Sätze — editierbar)</label>
                <textarea
                  rows={5}
                  value={proposal.summary_ending}
                  onChange={(e) => setProposal({ ...proposal, summary_ending: e.target.value })}
                />
              </div>
              <div style={{ marginBottom: 12 }}>
                <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
                  Figuren — Voraussetzung vom Modell, korrigierbar (lebt/tot/unbekannt); Haken = kommt in den
                  Band-2-Codex
                </div>
                {proposal.characters.length === 0 && <div className="muted">Keine Codex-Figuren vorhanden.</div>}
                {proposal.characters.map((c) => (
                  <div
                    key={c.name}
                    className="chapter-list-item"
                    style={{ alignItems: "flex-start", cursor: "default" }}
                  >
                    <input type="checkbox" checked={keep.has(c.name)} onChange={() => toggleKeep(c.name)} />
                    <span style={{ width: 130, overflow: "hidden", textOverflow: "ellipsis" }}>{c.name}</span>
                    <select
                      value={alive[c.name] === null ? "unknown" : alive[c.name] ? "alive" : "dead"}
                      onChange={(e) =>
                        setAliveValue(c.name, e.target.value === "unknown" ? null : e.target.value === "alive")
                      }
                      style={{ width: 110 }}
                    >
                      <option value="alive">lebt ✓</option>
                      <option value="dead">tot ✗</option>
                      <option value="unknown">unbekannt</option>
                    </select>
                    <span className="muted" style={{ flex: 1, fontSize: 12 }}>
                      {c.reason || "—"}
                    </span>
                  </div>
                ))}
              </div>
              <div className="form-col" style={{ marginBottom: 12 }}>
                <label>Offene Handlungsfäden (zeilenweise — editierbar)</label>
                <textarea
                  rows={4}
                  value={proposal.open_threads.join("\n")}
                  onChange={(e) =>
                    setProposal({
                      ...proposal,
                      open_threads: e.target.value.split("\n").filter((line) => line.trim().length > 0),
                    })
                  }
                />
              </div>
              <div className="form-row">
                <div className="form-col">
                  <label>Titel Band 2</label>
                  <input
                    value={proposal.proposed_title}
                    onChange={(e) => setProposal({ ...proposal, proposed_title: e.target.value })}
                  />
                </div>
                <div className="form-col">
                  <label>
                    <input
                      type="checkbox"
                      checked={includeDead}
                      onChange={(e) => setIncludeDead(e.target.checked)}
                    />{" "}
                    Auch tote als Flashback-Kandidaten übernehmen
                  </label>
                </div>
              </div>
              <div className="form-col" style={{ marginBottom: 12 }}>
                <label>Story-Prompt Band 2 (Prämisse verdichtet + Auflösung Band 1 + deine Idee)</label>
                <textarea
                  rows={5}
                  value={proposal.story_prompt}
                  onChange={(e) => setProposal({ ...proposal, story_prompt: e.target.value })}
                />
              </div>
              <div className="inline-note">
                Nach dem Anlegen: Codex übernommen (ohne Tote), Author Notes mit Ende/Fäden/Idee gefüllt —
                dann läuft Band 2 über den normalen Planner-Flow. Narrative State startet leer.
              </div>
              <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 10 }}>
                <button onClick={() => setStep("input")}>← Zurück</button>
                <button className="btn-primary" disabled={busy} onClick={create}>
                  {busy ? "Creating…" : "Band 2 anlegen"}
                </button>
              </div>
            </>
          )
        )}
      </div>
    </div>
  );
}