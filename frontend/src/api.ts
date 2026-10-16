import type {
  Book,
  BookSummary,
  ChapterRecord,
  Codex,
  ExtractVisualResult,
  LayoutIllustration,
  LayoutResponse,
  Profile,
  ReviewResponse,
  SequelProposal,
  StateCollection,
  Stats,
} from "./types";

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

  sequelPrepare: (id: string, data: { idea: string; planner_profile_id?: string; unload_after_call?: boolean }) =>
    request<{ proposal: SequelProposal }>(`/api/books/${id}/sequel/prepare`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  sequelCreate: (
    id: string,
    data: {
      proposal: SequelProposal;
      keep_character_names: string[];
      idea: string;
      target_words?: number | null;
      include_dead_as_flashbacks?: boolean;
    },
  ) => request<{ book: Book }>(`/api/books/${id}/sequel/create`, { method: "POST", body: JSON.stringify(data) }),

  translateReview: (id: string) =>
    request<ReviewResponse>(`/api/books/${id}/translate/review`),
  translateReviewDecide: (
    id: string,
    data: {
      batch_index: number;
      chapter_id?: number | null;
      type: string;
      paragraph: number | null;
      detail: string;
      action: string;
      text?: string | null;
    },
    ) =>
    request<{ open_flags: number; total_flags: number; review: ReviewResponse }>(
      `/api/books/${id}/translate/review/decide`,
      { method: "POST", body: JSON.stringify(data) },
    ),
  translateRetranslateBatch: (id: string, batchIndex: number, unloadAfterCall = true) =>
    request<{ open_flags: number; total_flags: number; review: ReviewResponse }>(
      `/api/books/${id}/translate/review/retranslate`,
      { method: "POST", body: JSON.stringify({ batch_index: batchIndex, unload_after_call: unloadAfterCall }) },
    ),

  getLayout: (id: string) => request<LayoutResponse>(`/api/books/${id}/layout`),
  putLayout: (id: string, data: Record<string, any>) =>
    request<{ layout: LayoutResponse["layout"] }>(`/api/books/${id}/layout`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  extractLayoutCharacters: (id: string, data: { profile_id?: string; unload_after_call?: boolean } = {}) =>
    request<{ characters: { name: string; visual: string }[]; results: ExtractVisualResult[] }>(
      `/api/books/${id}/layout/extract-characters`,
      { method: "POST", body: JSON.stringify(data) },
    ),
  extractLayoutCharacter: (id: string, name: string, data: { profile_id?: string; unload_after_call?: boolean } = {}) =>
    request<{ result: ExtractVisualResult; character: { name: string; visual: string } }>(
      `/api/books/${id}/layout/extract-characters/${encodeURIComponent(name)}`,
      { method: "POST", body: JSON.stringify(data) },
    ),
  computePositions: (id: string) =>
    request<{ layout: LayoutResponse["layout"] }>(`/api/books/${id}/layout/compute-positions`, { method: "POST" }),
  describeIllustration: (id: string, posId: string, data: { profile_id?: string; unload_after_call?: boolean } = {}) =>
    request<{ illustration: LayoutIllustration }>(
      `/api/books/${id}/layout/illustrations/${posId}/describe`,
      { method: "POST", body: JSON.stringify(data) },
    ),
  uploadIllustrationImage: (id: string, posId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return fetch(`/api/books/${id}/layout/illustrations/${posId}/image`, { method: "POST", body: form }).then(
      async (response) => {
        if (!response.ok) {
          let detail = response.statusText;
          try {
            const body = await response.json();
            if (body?.detail) detail = body.detail;
          } catch {}
          throw new Error(`${response.status}: ${detail}`);
        }
        return (await response.json()) as { illustration: LayoutIllustration };
      },
    );
  },
  deleteIllustrationImage: (id: string, posId: string) =>
    request<{ illustration: LayoutIllustration }>(`/api/books/${id}/layout/illustrations/${posId}/image`, {
      method: "DELETE",
    }),
  layoutImageHref: (id: string, path: string, cacheBust?: string) =>
    `/api/books/${id}/layout/image?path=${encodeURIComponent(path)}${cacheBust ? `&v=${cacheBust}` : ""}`,
  uploadCover: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return fetch(`/api/books/${id}/layout/cover`, { method: "POST", body: form }).then(async (response) => {
      if (!response.ok) {
        let detail = response.statusText;
        try {
          const body = await response.json();
          if (body?.detail) detail = body.detail;
        } catch {}
        throw new Error(`${response.status}: ${detail}`);
      }
      return (await response.json()) as { cover_image_path: string };
    });
  },
  deleteCover: (id: string) =>
    request<{ cover_image_path: string }>(`/api/books/${id}/layout/cover`, { method: "DELETE" }),

  translateStart: (
    id: string,
    data: {
      target_language: string;
      mode?: string;
      profile_id?: string;
      single_translate?: boolean;
      test_num?: number;
      batch_size?: number;
      resume?: boolean;
      candidate_profile_ids?: string[];
      judge_profile_id?: string | null;
      unload_after_call?: boolean;
      draft_profile_id?: string;
      refiner_profile_id?: string;
      checker_profile_id?: string | null;
      style_probe?: string;
      draft_temperature?: number;
      refine_rounds?: number;
      similarity_stop?: number;
      ratio_min?: number;
      ratio_max?: number;
      batch_words?: number;
    },
  ) =>
    request<{ status: any }>(`/api/books/${id}/translate`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  translateStatus: (id: string) =>
    request<{ status: Record<string, any> }>(`/api/books/${id}/translate/status`),
  translateProgress: (id: string) =>
    request<{
      language: string;
      mode: string | null;
      run_status: string;
      batches_total: number | null;
      chapters: {
        chapter_id: number;
        chapter_name: string;
        words_total: number;
        translated_words: number;
        batches_total: number;
        batches_done: number;
        paragraphs: number;
        review_flags: number;
        started: boolean;
        done: boolean;
      }[];
    }>(`/api/books/${id}/translate/progress`),
  translateStop: (id: string) =>
    request<{ status: any }>(`/api/books/${id}/translate/stop`, { method: "POST" }),
  translateOutputs: (id: string) =>
    request<{ outputs: { path: string; size: number; modified: number }[] }>(
      `/api/books/${id}/translate/outputs`,
    ),
  translateFile: (id: string, path: string) =>
    request<{ path: string; content: string }>(
      `/api/books/${id}/translate/file?path=${encodeURIComponent(path)}`,
    ),
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