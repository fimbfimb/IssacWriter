import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Book, ExtractVisualResult, LayoutData, LayoutIllustration } from "../types";

type CharacterStatus = "pending" | "running" | "ok" | "no_text" | "error";

export default function LayoutTab({
  book,
  showToast,
}: {
  book: Book;
  showToast: (m: string) => void;
}) {
  const [layout, setLayout] = useState<LayoutData | null>(null);
  const [stats, setStats] = useState<{ total_words: number; per_chapter: { chapter_id: number; chapter_name: string; words: number; paragraphs: number }[] } | null>(null);
  const [profiles, setProfiles] = useState<{ id: string; name: string; model: string }[]>([]);
  const [profileId, setProfileId] = useState<string>(book.planner_profile_id ?? "");
  const [busyPos, setBusyPos] = useState<Record<string, boolean>>({});
  const [imageVersion, setImageVersion] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [charStatus, setCharStatus] = useState<Record<string, CharacterStatus>>({});
  const [resultStatuses, setResultStatuses] = useState<Record<string, string> | null>(null);
  const [extractRunning, setExtractRunning] = useState(false);
  const [describeProgress, setDescribeProgress] = useState<string | null>(null);
  const [activityLog, setActivityLog] = useState<string[]>([]);
  const logRef = useRef<HTMLDivElement>(null);
  const saveTimer = useRef<number | null>(null);
  const fileInputs = useRef<Record<string, HTMLInputElement | null>>({});
  const coverInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    api
      .listProfiles()
      .then((r) => setProfiles(r.profiles))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [activityLog.length]);

  const refresh = () =>
    api
      .getLayout(book.id)
      .then((r) => {
        setLayout(r.layout);
        setStats(r.stats);
      })
      .catch((e) => showToast(e.message));

  useEffect(() => {
    refresh();
  }, [book.id]);

  const patchLocal = (patch: Partial<LayoutData>) => {
    setLayout((prev) => (prev ? { ...prev, ...patch } : prev));
    setDirty(true);
  };

  // debounced autosave of free-text edits
  useEffect(() => {
    if (!dirty || !layout) return;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(async () => {
      try {
        await api.putLayout(book.id, {
          style_directive: layout.style_directive,
          words_per_page: layout.words_per_page,
          images_every_n_pages: layout.images_every_n_pages,
          cover_image_path: layout.cover_image_path,
          characters: layout.characters,
          illustrations: layout.illustrations,
        });
        setDirty(false);
      } catch (err: any) {
        showToast(err.message);
      }
    }, 700);
    return () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
  }, [dirty, layout, book.id]);

  const computePositions = async () => {
    try {
      const result = await api.computePositions(book.id);
      setLayout(result.layout);
      setImageVersion((v) => v + 1);
      showToast("Illustrations-Positionen neu berechnet");
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const logLine = (line: string) =>
    setActivityLog((prev) => [...prev.slice(-200), `${new Date().toLocaleTimeString()} · ${line}`]);

  const codexCharacterNames = (book.codex?.characters ?? [])
    .map((c) => (c.name || "").trim())
    .filter(Boolean)
    .filter((name, index, list) => list.findIndex((n) => n.trim() === name) === index);

  const extractVisuals = async () => {
    if (!profileId) {
      showToast("Select a profile for the extraction");
      return;
    }
    const names = codexCharacterNames;
    if (!names.length) {
      showToast("No codex characters to extract");
      return;
    }
    setExtractRunning(true);
    setResultStatuses(null);
    setCharStatus(Object.fromEntries(names.map((name) => [name, "pending" as CharacterStatus])));
    logLine(`Extraktion gestartet — ${names.length} Figuren, Profil „${profiles.find((p) => p.id === profileId)?.name ?? "?"}“`);
    let done = 0;
    for (const name of names) {
      setCharStatus((prev) => ({ ...prev, [name]: "running" }));
      logLine(`Extrahiere ${name}…`);
      try {
        const { result } = await api.extractLayoutCharacter(book.id, name, { profile_id: profileId });
        setCharStatus((prev) => ({ ...prev, [name]: result.status }));
        if (result.status === "ok") {
          setLayout((prev) =>
            prev
              ? {
                  ...prev,
                  characters: [
                    ...prev.characters.filter((row) => row.name !== result.name),
                    { name: result.name, visual: result.visual ?? "" },
                  ].sort((a, b) => a.name.localeCompare(b.name)),
                }
              : prev,
          );
          setResultStatuses((prev) => ({ ...(prev ?? {}), [result.name]: `${result.visual?.length ?? 0} Zeichen aus dem Text` }));
          logLine(`✔ ${name} — visuelles Profil extrahiert`);
        } else if (result.status === "no_text") {
          setResultStatuses((prev) => ({ ...(prev ?? {}), [result.name]: result.detail ?? "kein Befund" }));
          logLine(`○ ${name} — kein Namensvorkommen im Manuskript`);
        } else {
          setResultStatuses((prev) => ({ ...(prev ?? {}), [result.name]: result.error ?? "Fehler" }));
          logLine(`✘ ${name} — ${result.error ?? "Fehler"}`);
        }
      } catch (err: any) {
        setCharStatus((prev) => ({ ...prev, [name]: "error" }));
        setResultStatuses((prev) => ({ ...(prev ?? {}), [name]: err.message }));
        logLine(`✘ ${name} — ${err.message}`);
      }
      done += 1;
      logLine(`Fortschritt: ${done}/${names.length}`);
    }
    setExtractRunning(false);
    logLine("Extraktion beendet");
    showToast("Visuelle Profile extrahiert (editierbar)");
  };

  const describeOne = async (entry: LayoutIllustration) => {
    if (!profileId) {
      showToast("Select a profile for the scene description");
      return;
    }
    setBusyPos((prev) => ({ ...prev, [entry.id]: true }));
    try {
      const result = await api.describeIllustration(book.id, entry.id, { profile_id: profileId });
      setLayout((prev) =>
        prev
          ? {
              ...prev,
              illustrations: prev.illustrations.map((e) => (e.id === entry.id ? result.illustration : e)),
            }
          : prev,
      );
      setImageVersion((v) => v + 1);
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setBusyPos((prev) => ({ ...prev, [entry.id]: false }));
    }
  };

  const describeAllOpen = async () => {
    const open = (layout?.illustrations ?? []).filter(
      (e) => !e.retired && !(e.scene || "").trim(),
    );
    if (!open.length) {
      showToast("No open positions");
      return;
    }
    for (let i = 0; i < open.length; i++) {
      setDescribeProgress(`Beschreibt Position ${open[i].id} (${i + 1}/${open.length})…`);
      logLine(`Position ${open[i].id} — Beschreibung erzeugen…`);
      await describeOne(open[i]);
      logLine(`Position ${open[i].id} fertig (${i + 1}/${open.length})`);
    }
    setDescribeProgress(null);
    showToast("Alle offenen Positionen beschrieben");
  };

  const uploadImage = async (entryId: string, file: File) => {
    try {
      const result = await api.uploadIllustrationImage(book.id, entryId, file);
      setLayout((prev) =>
        prev
          ? {
              ...prev,
              illustrations: prev.illustrations.map((e) => (e.id === entryId ? result.illustration : e)),
            }
          : prev,
      );
      setImageVersion((v) => v + 1);
      showToast("Bild hochgeladen");
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const removeImage = async (entryId: string) => {
    try {
      const result = await api.deleteIllustrationImage(book.id, entryId);
      setLayout((prev) =>
        prev
          ? {
              ...prev,
              illustrations: prev.illustrations.map((e) => (e.id === entryId ? result.illustration : e)),
            }
          : prev,
      );
      setImageVersion((v) => v + 1);
    } catch (err: any) {
      showToast(err.message);
    }
  };

  const editIllustration = (id: string, patch: Partial<LayoutIllustration>) => {
    setLayout((prev) =>
      prev
        ? { ...prev, illustrations: prev.illustrations.map((e) => (e.id === id ? { ...e, ...patch } : e)) }
        : prev,
    );
    setDirty(true);
  };

  const uploadCover = (file: File) => {
    api
      .uploadCover(book.id, file)
      .then(() => refresh().then(() => setImageVersion((v) => v + 1)))
      .then(() => showToast("Cover gespeichert"))
      .catch((err: any) => showToast(err.message));
  };

  const removeCover = () => {
    api
      .deleteCover(book.id)
      .then(() => refresh())
      .then(() => setImageVersion((v) => v + 1))
      .catch((err: any) => showToast(err.message));
  };

  if (!layout) return <div className="card muted">Loading layout…</div>;

  const chapterName = (chapterId: number | null) =>
    chapterId === null ? "—" : stats?.per_chapter?.find((c) => c.chapter_id === chapterId)?.chapter_name ?? `Kapitel ${chapterId + 1}`;

  return (
    <div>
      <div className="card">
        <strong>Illustrations-Setup</strong>
        <div className="inline-note">
          Positionen = kumulierte Wortposition: „1 Bild alle N Seiten" mit {layout.words_per_page} Wörtern/Seite →
          Slots alle {layout.words_per_page! * layout.images_every_n_pages!} Wörter. Bilder werden extern erzeugt und
          hier hochgeladen — in der Position landen sie automatisch im EPUB.
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="form-col" style={{ minWidth: 320 }}>
            <label>Globaler Stilstrang (in jeden Bild-Prompt)</label>
            <input
              value={layout.style_directive}
              onChange={(e) => patchLocal({ style_directive: e.target.value })}
              placeholder="gedeckte Palette, literarische Illustration, 1960er Triest"
            />
          </div>
          <div className="form-col" style={{ minWidth: 140 }}>
            <label>Wörter pro „Seite"</label>
            <input
              type="number"
              min={50}
              max={2000}
              value={layout.words_per_page}
              onChange={(e) => patchLocal({ words_per_page: Number(e.target.value) || 250 })}
            />
          </div>
          <div className="form-col" style={{ minWidth: 170 }}>
            <label>Bild alle N Seiten</label>
            <input
              type="number"
              min={1}
              max={50}
              value={layout.images_every_n_pages}
              onChange={(e) => patchLocal({ images_every_n_pages: Number(e.target.value) || 5 })}
            />
          </div>
          <div className="form-col" style={{ minWidth: 240 }}>
            <label>Profil für Szenen-/Figuren-Extraktion</label>
            <select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              <option value="">— select —</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.model})
                </option>
              ))}
            </select>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <button onClick={computePositions}>⟳ Positionen berechnen</button>
          <button onClick={extractVisuals} disabled={!profileId}>
            👥 Visuelle Profile extrahieren
          </button>
          <span className="muted" style={{ alignSelf: "center", fontSize: 12 }}>
            {stats ? `${stats.total_words.toLocaleString()} Wörter im Buch` : ""}
          </span>
        </div>
      </div>

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
          <strong>
            Visuelle Profile (Personen)
            {extractRunning && (
              <span className="chip" style={{ color: "var(--accent)", borderColor: "var(--accent)", marginLeft: 8 }}>
                extrahiert… {Object.values(charStatus).filter((s) => s === "ok" || s === "no_text" || s === "error").length}
                /{codexCharacterNames.length}
              </span>
            )}
          </strong>
          {activityLog.length > 0 && (
            <button onClick={() => setActivityLog([])}>Log leeren</button>
          )}
        </div>
        <div className="inline-note">
          Verarbeitungsbasis für die Bild-Prompts: Aussehen, Kleidung, Alter — vom LLM aus dem Manuskript
          extrahiert, hier editierbar. Manuelle Überarbeitung wird beim „Neu extrahieren" der Figur überschrieben.
          {extractRunning && " Die Figuren werden nacheinander durch das Profil geschickt — abgeschlossene erscheinen sofort unten."}
        </div>
        {extractRunning && (
          <div className="chapter-list-item" style={{ cursor: "default", background: "var(--accent-soft)", marginBottom: 4 }}>
            <span style={{ flex: 1, fontSize: 13 }}>
              {Object.entries(charStatus).find(([, status]) => status === "running")?.[0]
                ? `Am Zug: ${Object.entries(charStatus).find(([, status]) => status === "running")![0]}`
                : "Extraktion…"}
            </span>
          </div>
        )}
        {activityLog.length > 0 && (
          <div ref={logRef} className="event-feed" style={{ height: 120, marginBottom: 10 }}>
            {activityLog.map((line, index) => (
              <div key={index}>{line}</div>
            ))}
          </div>
        )}
        {codexCharacterNames.length === 0 && (layout.characters ?? []).length === 0 && (
          <div className="muted" style={{ padding: 6 }}>
            Keine Codex-Figuren — „Visuelle Profile extrahieren" oben.
          </div>
        )}
        {(layout.characters ?? []).length === 0 && codexCharacterNames.length > 0 && (
          <div className="muted" style={{ padding: 6 }}>
            {extractRunning ? null : <span>Noch keine Profile — „Visuelle Profile extrahieren“ oben.</span>}
          </div>
        )}
        {(layout.characters ?? []).map((character, index) => {
          const status = charStatus[character.name];
          const resultNote = resultStatuses?.[character.name];
          return (
            <div key={character.name} className="codex-row">
              <div style={{ width: 150 }}>
                <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{character.name}</div>
                {status && (
                  <div style={{ fontSize: 11, color: status === "ok" ? "var(--good)" : status === "running" ? "var(--accent)" : "var(--warn)" }}>
                    {status === "running" ? "läuft…" : status === "ok" ? "✓ extrahiert" : status === "no_text" ? "○ kein Befund" : "✘ Fehler"}
                    {status === "ok" && resultNote ? ` · ${resultNote}` : ""}
                    {status === "no_text" && resultNote ? ` · ${resultNote}` : ""}
                  </div>
                )}
              </div>
              <div style={{ flex: 1 }}>
                <textarea
                  rows={3}
                  value={character.visual}
                  placeholder="Aussehen, Kleidung, Alter, Besonderheiten…"
                  onChange={(e) => {
                    const next = [...layout.characters];
                    next[index] = { ...next[index], visual: e.target.value };
                    patchLocal({ characters: next });
                  }}
                />
              </div>
              <button
                title="Diese Figur neu extrahieren (überschreibt deine manuelle Bearbeitung)"
                disabled={extractRunning || busyPos[`char:${character.name}`]}
                onClick={async () => {
                  const key = `char:${character.name}`;
                  if (!profileId) {
                    showToast("Select a profile for the extraction");
                    return;
                  }
                  setBusyPos((prev) => ({ ...prev, [key]: true }));
                  setCharStatus((prev) => ({ ...prev, [character.name]: "running" }));
                  logLine(`Extrahiere ${character.name}…`);
                  try {
                    const { result } = await api.extractLayoutCharacter(book.id, character.name, { profile_id: profileId });
                    setCharStatus((prev) => ({ ...prev, [character.name]: result.status }));
                    if (result.status === "ok") {
                      setLayout((prev) =>
                        prev
                          ? {
                              ...prev,
                              characters: prev.characters.map((row) =>
                                row.name === result.name ? { name: result.name, visual: result.visual ?? "" } : row,
                              ),
                            }
                          : prev,
                      );
                      setResultStatuses((prev) => ({ ...(prev ?? {}), [result.name]: `${result.visual?.length ?? 0} Zeichen aus dem Text` }));
                      logLine(`✔ ${result.name} — visuelles Profil extrahiert`);
                    }
                  } catch (err: any) {
                    setCharStatus((prev) => ({ ...prev, [character.name]: "error" }));
                    setResultStatuses((prev) => ({ ...(prev ?? {}), [character.name]: err.message }));
                    logLine(`✘ ${character.name} — ${err.message}`);
                  } finally {
                    setBusyPos((prev) => ({ ...prev, [key]: false }));
                  }
                }}
              >
                ↻
              </button>
            </div>
          );
        })}
      </div>

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
          <strong>
            Illustrations-Positionen
            {describeProgress && (
              <span className="chip" style={{ color: "var(--accent)", borderColor: "var(--accent)", marginLeft: 8 }}>
                {describeProgress}
              </span>
            )}
          </strong>
          <button onClick={describeAllOpen} disabled={!profileId || !!describeProgress}>
            Alle offenen erzeugen
          </button>
        </div>
        {(layout.illustrations ?? []).length === 0 && (
          <div className="muted" style={{ padding: 6 }}>
            Noch keine Positionen — oben „Positionen berechnen".
          </div>
        )}
        {(layout.illustrations ?? []).map((entry) => {
          const hasImage = !!entry.image_path;
          return (
            <div
              key={entry.id}
              style={{
                border: "1px solid var(--border)",
                borderRadius: 8,
                padding: 10,
                marginBottom: 10,
                opacity: entry.retired ? 0.5 : 1,
                display: "grid",
                gridTemplateColumns: "130px 1fr",
                gap: 12,
              }}
            >
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {hasImage ? (
                  <img
                    src={api.layoutImageHref(book.id, entry.image_path!, String(imageVersion))}
                    alt={entry.scene || entry.id}
                    style={{ width: "100%", borderRadius: 6, border: "1px solid var(--border)" }}
                  />
                ) : (
                  <div
                    className="muted"
                    style={{
                      border: "1px dashed var(--border)",
                      borderRadius: 6,
                      textAlign: "center",
                      padding: "22px 6px",
                      fontSize: 12,
                    }}
                  >
                    kein Bild
                  </div>
                )}
                <input
                  ref={(el) => {
                    fileInputs.current[entry.id] = el;
                  }}
                  type="file"
                  accept=".jpg,.jpeg,.png,.webp"
                  hidden
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) uploadImage(entry.id, file);
                    e.target.value = "";
                  }}
                />
                <button onClick={() => fileInputs.current[entry.id]?.click()}>
                  {hasImage ? "Ersetzen" : "Bild hochladen"}
                </button>
                {hasImage && (
                  <button className="btn-danger" onClick={() => removeImage(entry.id)}>
                    Löschen
                  </button>
                )}
              </div>
              <div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginBottom: 6 }}>
                  <span className="chip">{entry.id}</span>
                  <span className="chip">
                    Kap. {entry.chapter_id !== null ? chapterName(entry.chapter_id) : "?"}
                    {entry.paragraph_index !== null ? ` ¶${entry.paragraph_index + 1}` : ""}
                  </span>
                  <span className="chip">
                    ab Wort {Number(entry.anchor_word_offset ?? 0).toLocaleString()}
                  </span>
                  {entry.anchor_valid === false && (
                    <span className="chip" style={{ color: "var(--bad)", borderColor: "var(--bad)" }}>
                      Position unklar — neu berechnen
                    </span>
                  )}
                  {entry.retired && <span className="chip">Slot entfallen</span>}
                  {entry.near_duplicate && (
                    <span className="chip" style={{ color: "var(--warn)", borderColor: "var(--warn)" }}>
                      ähnliche Szene in Nähe
                    </span>
                  )}
                  {!entry.retired && (
                    <button onClick={() => describeOne(entry)} disabled={!profileId || !!busyPos[entry.id]}>
                      {busyPos[entry.id] ? "Beschreibe…" : "Beschreibung erzeugen"}
                    </button>
                  )}
                </div>
                <div className="form-row">
                  <div className="form-col">
                    <label>Szene (was geschieht)</label>
                    <input value={entry.scene ?? ""} onChange={(e) => editIllustration(entry.id, { scene: e.target.value })} />
                  </div>
                  <div className="form-col">
                    <label>Ort</label>
                    <input value={entry.location ?? ""} onChange={(e) => editIllustration(entry.id, { location: e.target.value })} />
                  </div>
                  <div className="form-col">
                    <label>Einstellung (Zeit/Stimmung/Wetter)</label>
                    <input value={entry.setting ?? ""} onChange={(e) => editIllustration(entry.id, { setting: e.target.value })} />
                  </div>
                </div>
                <div className="form-col">
                  <label>Finaler Bild-Prompt (wird bei „Beschreibung erzeugen" zusammengesetzt, hier editierbar)</label>
                  <textarea rows={3} value={entry.prompt ?? ""} onChange={(e) => editIllustration(entry.id, { prompt: e.target.value })} />
                </div>
                {entry.people_in_scene && entry.people_in_scene.length > 0 && (
                  <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
                    Figuren in der Szene: {entry.people_in_scene.join(", ")}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="card">
        <strong>Cover (Titelseiten-Bild)</strong>
        <div className="inline-note">Optional: landet im EPUB direkt nach der Titelseite.</div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {layout.cover_image_path ? (
            <>
              <img
                src={api.layoutImageHref(book.id, layout.cover_image_path, String(imageVersion))}
                alt="Cover"
                style={{ height: 90, borderRadius: 6, border: "1px solid var(--border)" }}
              />
              <button className="btn-danger" onClick={removeCover}>
                Cover entfernen
              </button>
            </>
          ) : (
            <>
              <input
                ref={coverInput}
                type="file"
                accept=".jpg,.jpeg,.png,.webp"
                hidden
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) uploadCover(file);
                  e.target.value = "";
                }}
              />
              <button onClick={() => coverInput.current?.click()}>Cover hochladen</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}