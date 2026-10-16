import { useEffect, useRef, useState } from "react";

type Segment = { k: string; t: string };

export default function LiveStreamBox({
  events,
  active,
  maxChars = 9000,
  height = 260,
}: {
  events: any[];
  active: boolean;
  maxChars?: number;
  height?: number;
}) {
  const segmentsRef = useRef<Segment[]>([]);
  const consumedRef = useRef(0);
  const [, force] = useState(0);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const timer = window.setInterval(() => force((v) => v + 1), 350);
    return () => window.clearInterval(timer);
  }, []);

  if (events.length < consumedRef.current) {
    segmentsRef.current = [];
    consumedRef.current = 0;
  }
  for (const event of events.slice(consumedRef.current)) {
    if (event.type === "llm_delta") {
      const chunks = (event.chunks ?? {}) as Record<string, string>;
      for (const kind of ["reasoning", "content", "tool_args"]) {
        const text = chunks[kind];
        if (!text) continue;
        const segments = segmentsRef.current;
        const last = segments[segments.length - 1];
        if (last && last.k === kind) {
          last.t += text;
        } else {
          segments.push({ k: kind, t: text });
        }
      }
      let total = segmentsRef.current.reduce((sum, seg) => sum + seg.t.length, 0);
      while (total > maxChars && segmentsRef.current.length > 1) {
        total -= segmentsRef.current[0].t.length;
        segmentsRef.current = segmentsRef.current.slice(1);
      }
      if (total > maxChars) {
        segmentsRef.current = [{ k: "content", t: "…" + segmentsRef.current[segmentsRef.current.length - 1].t.slice(-(maxChars - 60)) }];
      }
    }
  }
  consumedRef.current = events.length;

  const lastEvent = events[events.length - 1];
  const lastDelta = [...events].reverse().find((e) => e.type === "llm_delta" && e.sent_at);
  const quietSeconds = lastDelta?.sent_at ? Math.round(Date.now() / 1000 - lastDelta.sent_at) : null;
  const lastActivity = lastEvent?.sent_at ? `${Math.round(Date.now() / 1000 - lastEvent.sent_at)}s` : "—";
  const streamed = segmentsRef.current.reduce((sum, seg) => sum + seg.t.length, 0);

  useEffect(() => {
    if (boxRef.current && active) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  });

  const label =
    events
      .filter((e) => e.type === "llm_stream")
      .slice(-1)[0]?.label ??
    null;

  return (
    <div className="stringstream">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
        <strong>
          LLM output — live {label ? <span className="muted">· {label}</span> : null}
        </strong>
        <span className="muted" style={{ fontSize: 11.5 }}>
          {streamed.toLocaleString()} Zeichen gesamt · letzte Aktivität vor {lastActivity}
          {quietSeconds != null && quietSeconds > 45 && active ? ` · still seit ${quietSeconds}s` : ""}
          {active ? " · running" : " · idle"}
        </span>
      </div>
      <div
        ref={boxRef}
        className="event-feed"
        style={{ height, whiteSpace: "pre-wrap", fontFamily: "Consolas, monospace" }}
      >
        {segmentsRef.current.map((seg, i) =>
          seg.k === "reasoning" ? (
            <span key={i} style={{ color: "#8892a5", fontStyle: "italic" }}>
              ⟦denkt⟧ {seg.t}
            </span>
          ) : seg.k === "tool_args" ? (
            <span key={i}>{seg.t}</span>
          ) : (
            <span key={i} style={{ color: "#cfe3ff" }}>
              {seg.t}
            </span>
          ),
        )}
        {segmentsRef.current.length === 0 && (
          <span className="muted">
            waiting for the first token… (keep-alive every 15 s proves the server is alive)
          </span>
        )}
      </div>
    </div>
  );
}