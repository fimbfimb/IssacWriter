import { useEffect, useState } from "react";
import { api, userHeaders } from "../api";
import type { Book } from "../types";

function download(filename: string, content: string) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function ExportTab({ book, showToast }: { book: Book; showToast: (m: string) => void }) {
  const [epubBusy, setEpubBusy] = useState(false);
  const [translationEpubBusy, setTranslationEpubBusy] = useState(false);
  const [outputs, setOutputs] = useState<{ path: string; size: number; modified: number }[]>([]);
  const [fileBusy, setFileBusy] = useState<string | null>(null);

  useEffect(() => {
    api.translateOutputs(book.id).then((r) => setOutputs(r.outputs ?? [])).catch(() => setOutputs([]));
  }, [book.id, book.translation?.status]);

  const language = book.translation_settings?.target_language ?? "German";

  const exportEnglishEpub = async () => {
    setEpubBusy(true);
    try {
      const response = await fetch(`/api/books/${book.id}/export/epub`, { headers: userHeaders() });
      if (!response.ok) {
        let detail = response.statusText;
        try {
          const body = await response.json();
          if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        } catch {}
        throw new Error(detail);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") ?? "";
      const match = /filename=([^;]+)/.exec(disposition);
      const filename = match?.[1]?.trim() || "buch.epub";
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
      showToast("Englisches EPUB heruntergeladen");
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setEpubBusy(false);
    }
  };

  const exportTranslationEpub = async () => {
    setTranslationEpubBusy(true);
    try {
      const { blob, filename } = await api.translateEpub(book.id, language);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
      showToast(`Übersetzungs-EPUB (${language}) heruntergeladen`);
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setTranslationEpubBusy(false);
    }
  };

  const fetchFile = async (path: string) => {
    setFileBusy(path);
    try {
      const { content } = await api.translateFile(book.id, path);
      const filename = path.replace(/\\/g, "/").split("/").pop() ?? "download.txt";
      download(filename, content);
    } catch (err: any) {
      showToast(err.message);
    } finally {
      setFileBusy(null);
    }
  };

  const assembled = outputs.filter((row) => {
    const path = String(row.path).replace(/\\/g, "/").replace(/^german\//, "");
    return (
      path === "book_german.txt" ||
      path === "book_bilingual.txt" ||
      path === "manuscript.md" ||
      path === "book_german_refined_current.txt" ||
      path === "book_german_refined.txt" ||
      path === "german_refines/manuscript_refined.md" ||
      path === "review_list.md"
    );
  });

  const hasChapters = (book.chapters_written ?? 0) > 0;
  const hasTranslation = outputs.length > 0;
  const refinerDone = assembled.some((row) =>
    /book_german_refined|manuscript_refined/.test(String(row.path)),
  );

  return (
    <div>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
          <div>
            <strong>Englisches Buch (Original)</strong>
            <div className="inline-note">
              {hasChapters
                ? `${book.chapters_written} Kapitel · ${(book.words_written ?? 0).toLocaleString()} Words`
                : "Noch keine Kapitel geschrieben."}
              {" "}EPUB = Titelblatt, Cover, Illustrationen (ganzseitig), Inhaltsverzeichnis.
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <button className="btn-primary" onClick={exportEnglishEpub} disabled={!hasChapters || epubBusy}>
            {epubBusy ? "EPUB wird gebaut…" : "↓ EPUB (English)"}
          </button>
          <button
            onClick={async () => {
              try {
                const result = await api.exportContent(book.id, "md");
                download(`${book.title.replace(/[^\w\-]+/g, "_")}.md`, result.content);
              } catch (err: any) {
                showToast(err.message);
              }
            }}
            disabled={!hasChapters}
          >
            ↓ Manuskript (.md)
          </button>
          <button
            onClick={async () => {
              try {
                const result = await api.exportContent(book.id, "txt");
                download(`${book.title.replace(/[^\w\-]+/g, "_")}.txt`, result.content);
              } catch (err: any) {
                showToast(err.message);
              }
            }}
            disabled={!hasChapters}
          >
            ↓ Manuskript (.txt)
          </button>
          <a href={`/api/books/${book.id}/export?format=csv`} download style={{ alignSelf: "center" }}>
            <button disabled={!hasChapters}>↓ Statistik (.csv)</button>
          </a>
        </div>
      </div>

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
          <div>
            <strong>Übersetztes Buch ({language})</strong>
            <div className="inline-note">
              {hasTranslation
                ? refinerDone
                  ? "Deutsch-Verfeinerung liegt vor — der EPUB nimmt die verfeinerten Kapitel."
                  : "Übersetzung vorhanden — der EPUB nimmt die übersetzten Kapitel."
                : "Noch keine Übersetzungsausgänge — laufen lassen, dann erscheinen die Downloads hier."}
              {" "}Cover + Illustrationen kommen mit.
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <button className="btn-primary" onClick={exportTranslationEpub} disabled={!hasTranslation || translationEpubBusy}>
            {translationEpubBusy ? "EPUB wird gebaut…" : `↓ EPUB (${language})`}
          </button>
          {assembled.map((row) => (
            <button key={row.path} onClick={() => fetchFile(row.path)} disabled={fileBusy === row.path}>
              ↓ {String(row.path).replace(/\\/g, "/").split("/").pop()}
              {String(row.path).includes("german_refined") ? " (verfeinert)" : ""}
            </button>
          ))}
        </div>
        {hasTranslation && assembled.length === 0 && (
          <div className="inline-note" style={{ marginTop: 8 }}>
            Ausgänge da, aber keine fertigen Manuskripte — der EPUB-Button funktioniert schon.
          </div>
        )}
      </div>
    </div>
  );
}