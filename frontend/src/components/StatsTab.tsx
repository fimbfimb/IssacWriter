import { useEffect, useMemo, useRef, useState } from "react";
import Chart from "chart.js/auto";
import type { ChartConfiguration } from "chart.js";
import { api } from "../api";
import type { Book, Stats } from "../types";

function useChart(id: string, config: ChartConfiguration | null) {
  const ref = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  useEffect(() => {
    if (!ref.current || !config) return;
    chartRef.current?.destroy();
    chartRef.current = new Chart(ref.current, config);
    return () => chartRef.current?.destroy();
  }, [id, JSON.stringify(config)]);
  return ref;
}

function PresenceTimeline({
  title,
  rows,
  maxChapters,
  showRoles,
}: {
  title: string;
  rows: Stats["characters"] | Stats["settings"];
  maxChapters: number;
  showRoles: boolean;
}) {
  if (!rows.length) {
    return (
      <div className="card">
        <strong>{title}</strong>
        <div className="muted" style={{ fontSize: 12 }}>
          No entries yet â€” add characters/settings in the Codex tab.
        </div>
      </div>
    );
  }
  const axisTicks: number[] = [];
  const step = maxChapters > 40 ? 5 : maxChapters > 20 ? 2 : 1;
  for (let ch = 0; ch <= maxChapters; ch += step) axisTicks.push(ch);
  const maxMentions = Math.max(1, ...rows.flatMap((row) => Object.values(row.mentions ?? {}).map(Number)));

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <strong>{title}</strong>
        <span className="muted" style={{ fontSize: 12 }}>
          â–² first appearance Â· â–¼ last chapter Â· bar intensity = mentions
        </span>
      </div>
      <div className="timeline" style={{ marginTop: 10 }}>
        <div className="timeline-row">
          <div className="timeline-label" />
          <div className="timeline-cells" />
        </div>
        {rows.map((row) => {
          const color = row.color || "#7c9cff";
          const entries = Object.entries(row.mentions ?? {}).map(([ch, n]) => [Number(ch), Number(n)] as const);
          const maxCh = entries.length ? Math.max(...entries.map(([ch]) => ch)) : -1;
          const first = row.first_chapter;
          return (
            <div key={row.name} className="timeline-row">
              <div className="timeline-label" title={row.name + (showRoles && (row as any).role ? ` â€” ${(row as any).role}` : "")}>
                {color && <span className="legend-swatch" style={{ background: color }} />}
                {row.name}
                <span className="muted" style={{ marginLeft: 4, fontSize: 10.5 }}>
                  {row.total_mentions}Ã—
                </span>
              </div>
              <div className="timeline-cells">
                {Array.from({ length: Math.max(maxChapters, 1) }, (_, ch) => {
                  const n = row.mentions?.[String(ch)] ?? 0;
                  const intensity = n === 0 ? 0 : 0.25 + 0.75 * (n / maxMentions);
                  return (
                    <div
                      key={ch}
                      className={`timeline-cell ${ch === first ? "entered" : ""} ${ch === maxCh ? "exited" : ""}`}
                      title={n ? `${row.name}: ${n} mentions in chapter ${ch}` : `${row.name}: absent in chapter ${ch}`}
                      style={
                        n
                          ? { background: color, opacity: intensity }
                          : ch < (first ?? Infinity)
                            ? { background: "#10141b" }
                            : undefined
                      }
                    />
                  );
                })}
              </div>
            </div>
          );
        })}
        <div className="timeline-row">
          <div className="timeline-label muted" style={{ fontSize: 10.5 }}>
            chapter
          </div>
          <div className="timeline-cells" style={{ position: "relative", height: 16 }}>
            {axisTicks.map((ch) => (
              <div
                key={ch}
                className="timeline-axis"
                style={{ position: "absolute", left: `${(ch / Math.max(1, maxChapters)) * 100}%`, transform: "translateX(-50%)" }}
              >
                {ch}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function ThreadsChart({ threads, maxChapters }: { threads: Stats["threads"]; maxChapters: number }) {
  if (!threads.length) {
    return (
      <div className="card">
        <strong>Plot threads (open obligations)</strong>
        <div className="muted" style={{ fontSize: 12 }}>
          No obligations recorded yet.
        </div>
      </div>
    );
  }
  return (
    <div className="card">
      <strong>Plot threads (open obligations)</strong>
      <div className="legend">
        <span>
          <span className="legend-swatch" style={{ background: "var(--accent)" }} />
          resolved thread (opened â†’ resolved)
        </span>
        <span>
          <span className="legend-swatch" style={{ background: "var(--warn)" }} />
          still open
        </span>
      </div>
      <div style={{ marginTop: 10 }}>
        {threads
          .slice()
          .sort((a, b) => (a.opened_at ?? 0) - (b.opened_at ?? 0))
          .map((thread) => {
            const opened = thread.opened_at ?? 0;
            const resolved = thread.resolved_at;
            const left = (opened / Math.max(1, maxChapters)) * 100;
            const right = ((resolved ?? maxChapters) / Math.max(1, maxChapters)) * 100;
            return (
              <div key={thread.key} className="threads-row">
                <div className="threads-key" title={thread.key}>
                  {thread.key}
                </div>
                <div className="threads-track">
                  <div
                    className={`threads-bar ${resolved == null ? "open" : ""}`}
                    style={{ left: `${left}%`, width: `${Math.max(2, right - left)}%` }}
                    title={`opened in ch ${opened}${resolved != null ? `, resolved in ch ${resolved}` : " â€” still open"}`}
                  />
                </div>
                <div className="muted" style={{ width: 110, fontSize: 11 }}>
                  ch {opened}{resolved != null ? ` â†’ ${resolved}` : " â†’ ?"}
                </div>
              </div>
            );
          })}
      </div>
    </div>
  );
}

function ConceptGraph({ book }: { book: Book }) {
  const outline = book.outline;
  if (!outline?.chapters?.length) {
    return (
      <div className="card">
        <strong>Book concept</strong>
        <div className="muted" style={{ fontSize: 12 }}>
          Generate an outline to see the concept graph.
        </div>
      </div>
    );
  }
  const acts = outline.act_sections?.length ? outline.act_sections : [];
  const chapters = outline.chapters;
  const codex = book.codex ?? { characters: [], settings: [], lore: [] };
  const chaptersPerAct = acts.length ? Math.ceil(chapters.length / acts.length) : 0;
  const width = Math.max(900, chapters.length * 46);
  const height = 210 + (acts.length ? 90 : 0);
  const chapterX = (index: number) => 60 + index * (width - 120) / Math.max(1, chapters.length - 1);

  return (
    <div className="card">
      <strong>Book concept</strong>
      <div className="inline-note">Premise â†’ acts â†’ chapters with the characters involved (from codex + mentions).</div>
      <svg className="concept-svg" viewBox={`0 0 ${width} ${height}`} height={height}>
        {(() => {
          const parts: JSX.Element[] = [];
          const premY = 36;
          const actY = 110;
          const chY = 200;
          parts.push(
            <g key="premise">
              <rect x={60} y={premY - 22} width={190} height={44} rx={8} fill="#20293a" stroke="#7c9cff" />
              <text className="node-title" x={72} y={premY - 4}>
                PREMISE
              </text>
              <text className="node-sub" x={72} y={premY + 12}>
                {(outline.premise ?? "").slice(0, 42)}â€¦
              </text>
            </g>,
          );
          if (acts.length) {
            acts.forEach((act, i) => {
              const x = 60 + (i * (width - 300)) / Math.max(1, acts.length - 1) + 120;
              parts.push(
                <line key={`e-p-${i}`} className="edge" x1={250} y1={premY} x2={x} y2={actY - 14} />,
                <g key={`a-${i}`}>
                  <rect x={x - 80} y={actY - 18} width={170} height={40} rx={8} fill="#20293a" stroke="#39415a" />
                  <text className="node-title" x={x - 70} y={actY - 2}>
                    ACT {i + 1}: {(act.title ?? "").slice(0, 22)}
                  </text>
                  <text className="node-sub" x={x - 70} y={actY + 14}>
                    chapters {i * chaptersPerAct}â€“{Math.min(chapters.length, (i + 1) * chaptersPerAct) - 1}
                  </text>
                </g>,
              );
            });
          }
          chapters.forEach((chapter, i) => {
            const x = chapterX(i);
            if (acts.length) {
              const actIndex = Math.min(acts.length - 1, Math.floor(i / chaptersPerAct));
              const ax = 60 + (actIndex * (width - 300)) / Math.max(1, acts.length - 1) + 120;
              parts.push(<line key={`e-c-${i}`} className="edge" x1={ax} y1={actY + 22} x2={x} y2={chY - 16} />);
            } else {
              parts.push(<line key={`e-c-${i}`} className="edge" x1={250} y1={premY} x2={x} y2={chY - 16} />);
            }
            const involved = codex.characters.filter((c) => c.name && chapter.description.toLowerCase().includes(c.name.toLowerCase()));
            parts.push(
              <g key={`c-${i}`}>
                <circle cx={x} cy={chY} r={13} fill="#121820" stroke={chapter.id <= (book.chapters_written ?? 0) ? "#3ecf8e" : "#39415a"} strokeWidth={2} />
                <text className="node-text" x={x} y={chY + 4} textAnchor="middle">
                  {chapter.id}
                </text>
                {involved.map((char, k) => (
                  <text
                    key={k}
                    className="node-sub"
                    x={x}
                    y={chY - 20}
                    textAnchor="middle"
                    fill={char.color || "#9aa3b2"}
                  >
                    â—
                  </text>
                ))}
              </g>,
            );
            if (i % Math.max(1, Math.round(chapters.length / 24)) === 0 || i === chapters.length - 1) {
              parts.push(
                <text key={`ct-${i}`} className="node-sub" x={x} y={chY + 30} textAnchor="middle">
                  {(chapter.name ?? "").slice(0, 16)}
                </text>,
              );
            }
          });
          return parts;
        })()}
      </svg>
    </div>
  );
}

export default function StatsTab({ book, showToast }: { book: Book; showToast: (m: string) => void }) {
  const [stats, setStats] = useState<Stats | null>(null);

  useEffect(() => {
    api
      .getStats(book.id)
      .then(setStats)
      .catch((err) => showToast(`stats failed: ${err.message}`));
  }, [book.id, book.chapters_written, book.runner?.status]);

  const maxChapters = useMemo(() => {
    const planned = book.outline?.chapters?.length ?? 0;
    return Math.max(planned, stats?.per_chapter.length ?? 0) - 1;
  }, [book.outline?.chapters, stats?.per_chapter]);

  const progressConfig = useMemo(() => {
    if (!stats?.per_chapter.length) return null;
    return {
      type: "bar",
      data: {
        labels: stats.per_chapter.map((row) => `#${row.chapter_id}`),
        datasets: [
          {
            type: "bar" as const,
            label: "Words per chapter",
            data: stats.per_chapter.map((row) => row.words),
            backgroundColor: "rgba(124, 156, 255, 0.55)",
            borderColor: "#7c9cff",
            borderWidth: 1,
          },
          {
            type: "bar" as const,
            label: "Chapter target",
            data: stats.per_chapter.map((row) => row.target ?? 0),
            backgroundColor: "rgba(255, 255, 255, 0.10)",
            borderColor: "#39415a",
            borderWidth: 1,
          },
          {
            type: "line" as const,
            label: "Cumulative words",
            data: stats.per_chapter.map((row) => row.cumulative),
            yAxisID: "y1",
            borderColor: "#3ecf8e",
            backgroundColor: "rgba(62, 207, 142, 0.12)",
            fill: true,
            tension: 0.3,
            pointRadius: 2,
          },
        ],
      },
      options: {
        scales: {
          y: { beginAtZero: true, grid: { color: "#20293a" }, ticks: { color: "#9aa3b2" } },
          y1: { beginAtZero: true, position: "right" as const, grid: { display: false }, ticks: { color: "#3ecf8e" } },
          x: { grid: { display: false }, ticks: { color: "#9aa3b2", maxRotation: 0 } },
        },
        plugins: { legend: { labels: { color: "#e6e9ee" } } },
      },
    } as unknown as ChartConfiguration;
  }, [stats?.per_chapter]);

  const costConfig = useMemo(() => {
    if (!stats?.per_chapter.length) return null;
    const hasCost = stats.per_chapter.some((row) => (row.cumulative_cost ?? 0) > 0);
    if (!hasCost) return null;
    return {
      type: "line",
      data: {
        labels: stats.per_chapter.map((row) => `#${row.chapter_id}`),
        datasets: [
          {
            label: "Cumulative cost (configured pricing)",
            data: stats.per_chapter.map((row) => row.cumulative_cost ?? 0),
            borderColor: "#f5a623",
            backgroundColor: "rgba(245, 166, 35, 0.12)",
            fill: true,
            tension: 0.3,
            pointRadius: 2,
          },
        ],
      },
      options: {
        scales: {
          y: { beginAtZero: true, grid: { color: "#20293a" }, ticks: { color: "#9aa3b2" } },
          x: { grid: { display: false }, ticks: { color: "#9aa3b2" } },
        },
        plugins: { legend: { labels: { color: "#e6e9ee" } } },
      },
    } as unknown as ChartConfiguration;
  }, [stats]);

  const progressRef = useChart("progress", progressConfig);
  const costRef = useChart("cost", costConfig);

  if (!stats) {
    return <div className="card muted">Loading statisticsâ€¦</div>;
  }

  return (
    <div>
      <div className="stat-cards">
        <div className="stat-card">
          <div className="value">{stats.total_words.toLocaleString()}</div>
          <div className="label">words written ({stats.coverage_words}% of target)</div>
        </div>
        <div className="stat-card">
          <div className="value">
            {stats.chapters_written} / {stats.chapters_planned}
          </div>
          <div className="label">chapters</div>
        </div>
        <div className="stat-card">
          <div className="value">
            {(stats.usage_totals.prompt_tokens / 1000).toFixed(1)}K / {(stats.usage_totals.completion_tokens / 1000).toFixed(1)}K
          </div>
          <div className="label">tokens in / out</div>
        </div>
        <div className="stat-card">
          <div className="value">{stats.usage_totals.cost.toFixed(4)}</div>
          <div className="label">cost (configured pricing)</div>
        </div>
        <div className="stat-card">
          <div className="value">{stats.open_threads.length}</div>
          <div className="label">open plot threads</div>
        </div>
      </div>

      {progressRef && (
        <div className="card">
          <strong>Words per chapter vs. target</strong>
          <canvas ref={progressRef as any} id="progress" style={{ marginTop: 10 }} />
        </div>
      )}

      {costRef && (
        <div className="card">
          <strong>Cost development</strong>
          <canvas ref={costRef as any} id="cost" style={{ marginTop: 10 }} />
        </div>
      )}

      <PresenceTimeline title="Character presence timeline" rows={stats.characters} maxChapters={maxChapters} showRoles />
      <PresenceTimeline title="Settings timeline" rows={stats.settings} maxChapters={maxChapters} showRoles={false} />

      <ThreadsChart threads={stats.threads} maxChapters={maxChapters} />

      <ConceptGraph book={book} />

      <div className="card">
        <strong>Tool usage totals</strong>
        <div className="kv-grid" style={{ marginTop: 8 }}>
          {Object.entries(stats.tool_totals).map(([tool, n]) => (
            <div key={tool} className="chip">
              {tool}: {n}
            </div>
          ))}
          {Object.keys(stats.tool_totals).length === 0 && <span className="muted">no chapters yet</span>}
        </div>
      </div>

      <div className="card">
        <strong>Export</strong>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button
            onClick={async () => {
              const result = await api.exportContent(book.id, "md");
              download(`${book.title.replace(/[^\w\-]+/g, "_")}.md`, result.content);
            }}
          >
            Download manuscript (.md)
          </button>
          <button
            onClick={async () => {
              const result = await api.exportContent(book.id, "txt");
              download(`${book.title.replace(/[^\w\-]+/g, "_")}.txt`, result.content);
            }}
          >
            Download manuscript (.txt)
          </button>
          <a
            href={`/api/books/${book.id}/export?format=csv`}
            download
            style={{ alignSelf: "center" }}
          >
            <button>Download stats (.csv)</button>
          </a>
        </div>
      </div>
    </div>
  );
}

function download(filename: string, content: string) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}