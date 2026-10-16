import type { Book, BookSummary, ChapterRecord, Codex, Profile, StateCollection, Stats } from "./types";

const BASE = "";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<{ ok: boolean }>("/api/health"),
  settings: () => request<Record<string, any>>("/api/settings"),

  listProfiles: () => request<{ profiles: Profile[] }>("/api/profiles"),
  createProfile: (data: Partial<Profile>) =>
    request<{ profile: Profile }>("/api/profiles", { method: "POST", body: JSON.stringify(data) }),
  updateProfile: (id: string, data: Partial<Profile>) =>
    request<{ profile: Profile }>(`/api/profiles/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteProfile: (id: string) => request<{ ok: boolean }>(`/api/profiles/${id}`, { method: "DELETE" }),
  profileModels: (id: string) => request<{ models: string[] }>(`/api/profiles/${id}/models`),

  listBooks: () => request<{ books: BookSummary[] }>("/api/books"),
  createBook: (data: { title: string; story_prompt: string; language: string; target_words: number; genre_notes?: string }) =>
    request<{ book: Book }>("/api/books", { method: "POST", body: JSON.stringify(data) }),
  getBook: (id: string) => request<{ book: Book }>(`/api/books/${id}`),
  patchBook: (id: string, data: Partial<Book>) =>
    request<{ book: Book }>(`/api/books/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteBook: (id: string) => request<{ ok: boolean }>(`/api/books/${id}`, { method: "DELETE" }),

  replaceCodex: (id: string, codex: Codex) =>
    request<{ codex: Codex }>(`/api/books/${id}/codex`, { method: "PUT", body: JSON.stringify(codex) }),
  extractCodex: (id: string, plannerProfileId: string) =>
    request<{ codex: Codex; added: Record<string, number>; added_names: Record<string, { added: string[] }> }>(
      `/api/books/${id}/codex/extract`,
      { method: "POST", body: JSON.stringify({ planner_profile_id: plannerProfileId }) },
    ),

  generateOutline: (id: string, plannerProfileId: string) =>
    request<{ status: any }>(`/api/books/${id}/outline/generate`, {
      method: "POST",
      body: JSON.stringify({ planner_profile_id: plannerProfileId }),
    }),
  regenerateStage: (id: string, stage: string, plannerProfileId?: string) =>
    request<{ status: any }>(`/api/books/${id}/outline/regenerate-stage`, {
      method: "POST",
      body: JSON.stringify({ stage, planner_profile_id: plannerProfileId }),
    }),
  freezeOutline: (id: string) =>
    request<{ outline: any }>(`/api/books/${id}/outline/freeze`, { method: "POST" }),
  patchOutline: (id: string, data: Record<string, any>) =>
    request<{ outline: any }>(`/api/books/${id}/outline`, { method: "PATCH", body: JSON.stringify(data) }),

  runStart: (id: string, data: { writer_profile_id?: string }) =>
    request<{ status: any }>(`/api/books/${id}/run/start`, { method: "POST", body: JSON.stringify(data) }),
  runPause: (id: string) => request<{ status: any }>(`/api/books/${id}/run/pause`, { method: "POST" }),
  runResume: (id: string) => request<{ status: any }>(`/api/books/${id}/run/resume`, { method: "POST" }),
  runStop: (id: string) => request<{ status: any }>(`/api/books/${id}/run/stop`, { method: "POST" }),
  runAbort: (id: string) =>
    request<{ status: any; message: string }>(`/api/books/${id}/run/abort`, { method: "POST" }),
  runReview: (id: string, decision: { action: string; content?: string; note?: string }) =>
    request<{ status: any }>(`/api/books/${id}/run/review`, { method: "POST", body: JSON.stringify(decision) }),
  runStatus: (id: string) => request<{ status: any }>(`/api/books/${id}/run/status`),

  getChapters: (id: string) => request<{ chapters: ChapterRecord[] }>(`/api/books/${id}/chapters`),
  patchChapter: (id: string, chapterId: number, data: { name?: string; content?: string }) =>
    request<{ chapter: ChapterRecord }>(`/api/books/${id}/chapters/${chapterId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  getState: (id: string) => request<{ state: StateCollection }>(`/api/books/${id}/state`),
  patchState: (
    id: string,
    data: { collection: string; key_or_name: string; description?: string; delete?: boolean },
  ) => request<{ state: StateCollection }>(`/api/books/${id}/state`, { method: "PATCH", body: JSON.stringify(data) }),

  putNotes: (id: string, notes: string) =>
    request<{ author_notes: string }>(`/api/books/${id}/notes`, { method: "PUT", body: JSON.stringify({ notes }) }),

  getStats: (id: string) => request<Stats>(`/api/books/${id}/stats`),
  exportContent: (id: string, format: "md" | "txt") =>
    request<{ content: string }>(`/api/books/${id}/export?format=${format}`),
};

export function openEventStream(bookId: string, onEvent: (event: any) => void): () => void {
  const source = new EventSource(`${BASE}/api/books/${bookId}/events`);
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data));
    } catch {}
  };
  source.onerror = () => {};
  return () => source.close();
}