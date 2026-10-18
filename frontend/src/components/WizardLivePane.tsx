import { useEffect, useState } from "react";
import { api } from "../api";
import type { WizardState, WizardStepState } from "../types";
import LiveStreamBox from "./LiveStreamBox";

const STREAM_EVENTS = new Set([
  "wizard_started", "wizard_resumed", "wizard_paused", "wizard_stopped", "wizard_done",
  "wizard_step_started", "wizard_step_done", "wizard_step_error",
  "wizard_candidates",
  "llm_call", "llm_stream",
  "outline_refine_started", "outline_refine_batch", "outline_refine_output", "outline_refine_done", "outline_refine_error",
  "refine_started", "refine_batch", "refine_output", "refine_progress", "refine_chapter", "refine_done", "refine_error",
  "translation_started", "translation_batch", "translation_output", "translate_progress", "translation_done", "translation_error",
  "german_refine_started", "german_refine_batch", "german_refine_output", "german_refine_progress", "german_refine_chapter", "german_refine_done", "german_refine_error",
  "chapter_done", "chapter_started", "runner_stopped", "runner_error",
]);

function slugify(language: string): string {
  return (language || "german").trim().toLowerCase().replace(/\s+/g, "-");
}

function liveFileSpec(state: WizardState, row: WizardStepState | null): { kind: string; path: string; language: string } | null {
  if (!row) return null;
  const kind = row.kind;
  const translateSlug = slugify(wizardTargetLanguage(state));
  if (kind === "outline_refine") return { kind: "outline", path: "outline_refined/book_outline_refined.md", language: "" };
  if (kind === "text_refine") return { kind: "refine", path: "refines/book_refined_current.txt", language: "" };
  if (kind === "translate")
    return { kind: "translate", path: `${translateSlug}/book_german.txt`, language: translateSlug };
  if (kind === "translate_refine")
    return { kind: "german", path: "book_german_refined_current.txt", language: translateSlug };
  return null;
}

export function wizardTargetLanguage(state: WizardState): string {
  const step5 = (state.config?.steps ?? []).find((step) => step.kind === "translate");
  return step5?.target_language || "German";
}

function formatWizardEvent(event: any): { time: string; html: string } {
  const time = event.sent_at ? new Date(event.sent_at * 1000).toLocaleTimeString() : "";
  const safe = (text: string) => String(text ?? "").replace(/[<>&]/g, "");
  const label = (step: number) => (step === 1
    ? "Outline"
    : step === 2
      ? "Outline-Verfeinerung"
      : step === 3
        ? "Kapitel schreiben"
        : step === 4
          ? "Textverfeinerung"
          : step === 5
            ? "Übersetzung"
            : step === 6
              ? "Deutsch-Verfeinerung"
              : `Schritt ${step}`);
  if (event.type === "wizard_started")
    return { time, html: `🧙 Wizard gestartet — Buch <b>${safe(event.book ?? "")}</b>` };
  if (event.type === "wizard_resumed") return { time, html: `<span class="ev-ok">▶ Wizard fortgesetzt</span>` };
  if (event.type === "wizard_paused")
    return { time, html: `<span style="color: var(--warn)">⏸ Wizard pausiert${event.reason ? ` (${safe(event.reason)})` : ""}</span>` };
  if (event.type === "wizard_stopped") return { time, html: `<span style="color: var(--warn)">⏹ Wizard gestoppt</span>` };
  if (event.type === "wizard_done")
    return { time, html: `<span class="ev-ok">🏁 Wizard fertig</span> — ${safe(event.summary ?? "")}` };
  if (event.type === "wizard_step_started") return { time, html: `▶ Schritt ${event.step} (${safe(label(event.step))}) gestartet` };
  if (event.type === "wizard_step_done")
    return {
      time,
      html:
        event.status === "skipped"
          ? `<span class="muted">⊘ Schritt ${event.step} übersprungen</span>`
          : `<span class="ev-ok">✔ Schritt ${event.step} (${safe(label(event.step))}) abgeschlossen</span>${event.note ? ` · ${safe(event.note)}` : ""}`,
    };
  if (event.type === "wizard_step_error")
    return { time, html: `<span class="ev-fail">✘ Schritt ${event.step} Fehler:</span> ${safe(event.error ?? "")}` };
  if (event.type === "wizard_candidates")
    return {
      time,
      html: `<span class="ev-tool">⚖ Kandidaten</span> ${safe((event.candidates ?? []).join(", "))} → Gewinner <b>${safe(event.winner ?? "?")}</b>${event.reason ? ` — ${safe(event.reason)}` : ""}`,
    };
  if (event.type === "llm_call") {
    if (event.phase === "start")
      return { time, html: `⌛ <b>${safe(event.role ?? "")}</b> arbeitet… ${safe(event.detail ?? "")}` };
    if (event.phase === "done")
      return { time, html: `<span class="ev-ok">✔ ${safe(event.role ?? "")}</span> fertig in ${safe(String(event.elapsed_s ?? "?"))}s` };
    return { time, html: `<span class="ev-fail">✘ ${safe(event.role ?? "")} Fehler nach ${safe(String(event.elapsed_s ?? "?"))}s</span> — ${safe(event.detail ?? "")}` };
  }
  if (event.type === "chapter_done")
    return { time, html: `<span class="ev-ok">✔ Kapitel ${(event.chapter_id ?? 0) + 1} accepted</span> (${safe(String(event.words ?? "?"))} Wörter)` };
  if (event.type === "outline_done")
    return { time, html: `<span class="ev-ok">✔ Outline fertig</span> — ${event.chapters ?? 0} Kapitel${event.frozen ? " · gefroren" : ""}` };
  if (event.type === "outline_refine_chapter")
    return { time, html: `<span class="ev-ok">✔ Kapitel ${(event.chapter_id ?? 0) + 1} Brief committed</span> → outline_refined/ v${String(event.version ?? 1).padStart(4, "0")}` };
  if (event.type === "refine_chapter" || event.type === "german_refine_chapter")
    return { time, html: `<span class="ev-ok">✔ Kapitel ${(event.chapter_id ?? 0) + 1} committed</span> (${event.words ?? 0} Wörter)` };
  if (event.type === "translation_batch" || event.type === "outline_refine_batch" || event.type === "german_refine_batch" || event.type === "refine_batch") {
    if (event.error)
      return { time, html: `<span class="ev-fail">✘ Block ${event.batch_index}/${event.batches_total} fehlgeschlagen:</span> ${safe(event.error)}` };
    return { time, html: `<span class="ev-ok">✔ Block ${event.batch_index}/${event.batches_total}</span> (${safe(event.stage ?? "?")})` };
  }
  if (event.type === "translation_done") {
    return { time, html: `<span class="ev-ok">✔ Übersetzung fertig</span>${event.gaps ? ` <span style="color: var(--warn)">(⚠ ${event.gaps} Blöcke fehlgeschlagen)</span>` : ""}` };
  }
  if (event.type === "refine_done" || event.type === "german_refine_done")
    return { time, html: `<span class="ev-ok">✔ Verfeinerung fertig</span>${event.gaps ? ` <span style="color: var(--warn)">(⚠ ${event.gaps} Blöcke fehlgeschlagen)</span>` : ""}` };
  if (event.type === "refine_error" || event.type === "german_refine_error" || event.type === "translation_error" || event.type === "outline_refine_error")
    return { time, html: `<span class="ev-fail">✘ Fehler:</span> ${safe(event.error ?? "")}` };
  if (event.type === "translation_output" || event.type === "refine_output" || event.type === "outline_refine_output" || event.type === "german_refine_output")
    return { time, html: safe((event.lines ?? []).slice(-2).join(" · ")) };
  if (event.type === "refine_progress" || event.type === "translate_progress" || event.type === "german_refine_progress")
    return { time, html: `<span class="ev-ok">¶ ${safe(String(event.paragraph ?? ""))} verfeinert</span> — ${event.rounds ?? 0} Runde(n)${event.stopped_early ? " · Frühstopp" : ""}` };
  if (event.type === "runner_stopped") return { time, html: `<span style="color: var(--warn)">⏹ Runner gestoppt</span>${event.reason ? ` — ${safe(event.reason)}` : ""}` };
  if (event.type === "runner_error") return { time, html: `<span class="ev-fail">✘ Runner-Fehler</span> — ${safe(event.error ?? "")}` };
  return { time, html: safe(event.type ?? "") };
}

export default function WizardLivePane({
  book,
  state,
  events,
}: {
  book: { id: string; title: string };
  state: WizardState;
  events: any[];
}) {
  const activeRow =
    state.steps_state.find((row) => row.step === state.current_step) ??
    state.steps_state.find((row) => row.status === "running") ??
    null;
  const spec = liveFileSpec(state, activeRow);
  const [tail, setTail] = useState<string | null>(null);
  const [tailError, setTailError] = useState<string | null>(null);

  useEffect(() => {
    if (!spec) {
      setTail(null);
      return;
    }
    let alive = true;
    let cancelled = false;
    const pull = async () => {
      try {
        let response = null;
        if (spec.kind === "outline") response = await api.outlineRefineFile(book.id, spec.path);
        else if (spec.kind === "refine") response = await api.refineFile(book.id, spec.path);
        else if (spec.kind === "translate")
          response = await api.translateFile(book.id, spec.path).catch(() =>
            api.translateFile(book.id, `${spec.language}/book_bilingual.txt`),
          );
        else response = await api.germanRefineFile(book.id, spec.path, spec.language).catch(() =>
          api.germanRefineFile(book.id, "book_german_refined.txt", spec.language),
        );
        if (alive && !cancelled && response) {
          setTail(response.content);
          setTailError(null);
        }
      } catch (err: any) {
        if (alive && !cancelled) setTailError(err?.message ?? "no output yet");
      }
    };
    pull();
    const timer = window.setInterval(pull, 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [book.id, spec?.kind, spec?.path, spec?.language]);

  const feed = events.filter((event) => STREAM_EVENTS.has(event.type)).slice(-220);

  return (
    <div>
      {activeRow && (activeRow.kind === "outline" || activeRow.kind === "write") ? (
        <LiveStreamBox events={events} active={activeRow.status === "running"} height={220} maxChars={7000} />
      ) : (
        <div className="stringstream">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
            <strong>Live-Ausgabe{spec ? <span className="muted"> · {spec.path}</span> : null}</strong>
            <span className="muted" style={{ fontSize: 11.5 }}>
              {activeRow?.status === "running" ? "running" : "idle"}
            </span>
          </div>
          <div className="prose-view" style={{ maxHeight: 220, fontSize: 13, padding: "14px 18px" }}>
            {tail ? tail.slice(-9000) : null}
            {!tail ? (
              <span className="muted">{tailError ? `noch keine Ausgabe (${tailError})` : "wartet auf erste Ausgabe…"}</span>
            ) : null}
          </div>
        </div>
      )}

      <div style={{ marginTop: 12 }}>
        <strong className="muted" style={{ fontSize: 12 }}>Protokoll</strong>
        <div className="event-feed" style={{ height: 240, marginTop: 6 }}>
          {feed.length === 0 ? <span className="muted">keine Ereignisse</span> : null}
          {feed
            .slice()
            .reverse()
            .map((event, index) => {
              const formatted = formatWizardEvent(event);
              return (
                <div key={index} style={{ marginBottom: 2 }}>
                  <span className="ev-time">{formatted.time}</span>
                  <span dangerouslySetInnerHTML={{ __html: formatted.html }} />
                </div>
              );
            })}
        </div>
      </div>
    </div>
  );
}