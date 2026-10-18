import type { WizardState, WizardStepState } from "../types";

const STEP_LABELS: Record<number, string> = {
  1: "Outline",
  2: "Outline-Verfeinerung",
  3: "Kapitel schreiben",
  4: "Textverfeinerung",
  5: "Übersetzung",
  6: "Deutsch-Verfeinerung",
};

export function wizardStepLabel(step: number): string {
  return STEP_LABELS[step] ?? `Schritt ${step}`;
}

function stepPercent(row: WizardStepState): number | null {
  const progress = row.progress ?? { done: 0, total: null };
  if (row.status === "done" || row.status === "skipped") return 100;
  if (progress.total && progress.total > 0)
    return Math.min(100, Math.round((progress.done / progress.total) * 100));
  return row.status === "running" ? 4 : 0;
}

function activityText(row: WizardStepState): string {
  const activity = row.engine?.activity;
  if (!activity) return "";
  const since = Math.round((Date.now() / 1000 - (activity.since ?? 0)) / 1);
  return `${activity.role} · ${activity.detail}${Number.isFinite(since) ? ` (${since}s)` : ""}`;
}

export function overallPercent(state: WizardState): number {
  const active = state.steps_state.filter((row) => row.status !== "skipped");
  const rows = active.length ? active : state.steps_state;
  const percents = rows.map((row) => stepPercent(row) ?? 0);
  if (!percents.length) return 0;
  return Math.round(percents.reduce((sum, value) => sum + value, 0) / percents.length);
}

export default function WizardProgressBoard({
  state,
  busy,
  onPause,
  onResume,
  onStop,
  onRetryStep,
  onTogglePauseBetweenSteps,
}: {
  state: WizardState;
  busy: boolean;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onRetryStep: (step: number) => void;
  onTogglePauseBetweenSteps: () => void;
}) {
  const overall = overallPercent(state);
  const status = state.status;
  const running = status === "running" || status === "pausing";
  const pausable = running;
  const resumable = status === "paused" || status === "stopped" || status === "error" || status === "done";
  const stoppable = running || status === "paused" || status === "stopping";

  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <strong style={{ fontSize: 16 }}>Buch-Wizard</strong>
          <span className={`chip status-${status}`}>{status}</span>
          {state.status === "pausing" ? <span className="muted">pausiert am nächsten Abschluss-Punkt…</span> : null}
          {state.status === "stopping" ? <span className="muted">stoppt sauber am Checkpoint…</span> : null}
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            className={`chip${state.config.pause_between_steps ? " status-done" : ""} btn-ghost`}
            style={{ padding: "6px 12px" }}
            onClick={onTogglePauseBetweenSteps}
            disabled={busy || running}
            title="Wenn an: nach jedem Schritt Pause (Resume-Knopf). Wirkung pro Schritt-Neustart."
          >
            Pausenmodus {state.config.pause_between_steps ? "AN" : "aus"}
          </button>
          <button onClick={onPause} disabled={busy || !pausable}>⏸ Pause</button>
          <button className="btn-good" onClick={onResume} disabled={busy || !resumable}>▶ Resume</button>
          <button className="btn-danger" onClick={onStop} disabled={busy || !stoppable}>⏹ Stop</button>
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "10px 0 4px" }}>
        <div className="progress-outer" style={{ flex: 1 }}>
          <div className="progress-inner" style={{ width: `${overall}%`, transition: "width 0.4s" }} />
        </div>
        <strong style={{ minWidth: 46, textAlign: "right" }}>{overall}%</strong>
      </div>

      <div className="wizard-steps" style={{ marginTop: 8 }}>
        {state.steps_state.map((row) => {
          const percent = stepPercent(row);
          const activity = activityText(row);
          return (
            <div key={row.step} className={`wizard-step-row${state.current_step === row.step ? " active" : ""}`}>
              <span style={{ minWidth: 210, display: "inline-flex", gap: 8, alignItems: "center" }}>
                <strong style={{ fontSize: 12.5 }}>
                  S{row.step} · {wizardStepLabel(row.step)}
                </strong>
                <span className={`chip status-${row.status === "running" && state.status === "pausing" ? "paused" : row.status}`} style={{ marginRight: 0 }}>
                  {row.status}
                </span>
              </span>
              <div className="progress-outer" style={{ flex: 1, margin: 0 }}>
                <div className="progress-inner" style={{ width: `${percent ?? 0}%`, transition: "width 0.4s" }} />
              </div>
              <span className="muted" style={{ minWidth: 70, textAlign: "right", fontSize: 12 }}>
                {row.progress?.total ? `${row.progress.done}/${row.progress.total}` : `${row.progress?.done ?? 0}`}
              </span>
              <span className="muted wizard-step-activity" title={activity} style={{ minWidth: 230, fontSize: 12 }}>
                {activity || (row.error ? `⚠ ${row.error}` : "")}
              </span>
              {row.status === "error" ? (
                <button className="btn-danger btn-ghost" style={{ padding: "3px 10px", fontSize: 12 }} onClick={() => onRetryStep(row.step)} disabled={busy}>
                  ↻ Retry
                </button>
              ) : (
                <span style={{ minWidth: 66 }} />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}