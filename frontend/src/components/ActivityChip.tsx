import { useEffect, useState } from "react";

export default function ActivityChip({
  activity,
  lastActivity,
  running,
}: {
  activity: { role: string; detail: string; since: number } | null | undefined;
  lastActivity: number | null | undefined;
  running: boolean;
}) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setTick((x) => x + 1), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  if (!running) return null;
  const now = Date.now() / 1000;
  if (activity?.since) {
    const secs = Math.max(0, Math.floor(now - activity.since));
    const mmss = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    return (
      <span
        className="chip"
        style={{ color: "var(--accent)", borderColor: "var(--accent)" }}
        title="Ein Modell generiert gerade — die Antwort landet bei Stufenabschluss im Live-Output"
      >
        ⌛ {activity.role} arbeitet · {activity.detail} · {mmss}
      </span>
    );
  }
  if (lastActivity) {
    const secs = Math.max(0, Math.round(now - lastActivity));
    const label = secs < 90 ? `${secs}s` : `${Math.round(secs / 60)} min`;
    return (
      <span className="chip" title="Zeit seit der letzten abgeschlossenen Aktion des Laufes">
        letzte Aktivität vor {label}
      </span>
    );
  }
  return null;
}