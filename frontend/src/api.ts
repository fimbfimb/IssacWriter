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
  StudioUser,
  WizardState,
  WizardSummary,
} from "./types";

const BASE = "";
const USER_KEY = "iw.current-user";

export function currentUser(): StudioUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as StudioUser) : null;
  } catch {
    return null;
  }
}

export function setCurrentUser(user: StudioUser | null): void {
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
  else localStorage.removeItem(USER_KEY);
}

export function userHeaders(): Record<string, string> {
  const user = currentUser();
  return user ? { "X-User-Id": user.id } : {};
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = { "Content-Type": "application/json", ...userHeaders() };
  const response = await fetch(BASE + path, { ...options, headers: { ...headers, ...((options as any)?.headers ?? {}) } });
  if (response.status === 401) {
    // stale/unknown user id (e.g. users wiped) — back to the login screen
    setCurrentUser(null);
    window.location.reload();
    throw new Error("401: user unknown — please pick a user again");
  }
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

  listUsers: () => request<{ users: StudioUser[] }>("/api/users"),
  createUser: (name: string) =>
    request<{ user: StudioUser }>("/api/users", { method: "POST", body: JSON.stringify({ name }) }),
  settings: () => request<Record<string, any>>("/api/settings"),
  saveOllamaKey: (key: string) =>
    request<{ ollama_cloud_api_key_set: boolean }>("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ ollama_cloud_api_key: key }),
    }),

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
  translateResetChapter: (id: string, chapterId: number) =>
    request<{ open_flags: number; total_flags: number; reset_batches: number; review: ReviewResponse }>(
      `/api/books/${id}/translate/review/reset-chapter`,
      { method: "POST", body: JSON.stringify({ chapter_id: chapterId }) },
    ),
  translateResetAll: (id: string) =>
    request<{ removed: string[] }>(`/api/books/${id}/translate/reset-all`, { method: "POST" }),
  translateAnnotationsRead: (id: string, ids: string[]) =>
    request<{ read_count: number; review: ReviewResponse }>(
      `/api/books/${id}/translate/review/annotations/read`,
      { method: "POST", body: JSON.stringify({ ids }) },
    ),
  translateFixParagraph: (id: string, data: { batch_index: number; paragraph: number; text: string }) =>
    request<{ open_flags: number; total_flags: number; review: ReviewResponse }>(
      `/api/books/${id}/translate/review/fix-paragraph`,
      { method: "POST", body: JSON.stringify(data) },
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
    return fetch(`/api/books/${id}/layout/illustrations/${posId}/image`, { method: "POST", body: form, headers: userHeaders() }).then(
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
    return fetch(`/api/books/${id}/layout/cover`, { method: "POST", body: form, headers: userHeaders() }).then(async (response) => {
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
      use_refined?: boolean;
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
  translateEpub: async (id: string, language: string) => {
    const response = await fetch(
      `/api/books/${id}/export/translation/epub${language ? `?language=${encodeURIComponent(language)}` : ""}`,
      { headers: userHeaders() },
    );
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch {}
      throw new Error(`${response.status}: ${detail}`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = /filename=([^;]+)/.exec(disposition);
    return { blob, filename: match?.[1]?.trim() ?? "uebersetzung.epub", source: response.headers.get("X-Epub-Source") };
  },
  translateFile: (id: string, path: string) =>
    request<{ path: string; content: string }>(
      `/api/books/${id}/translate/file?path=${encodeURIComponent(path)}`,
    ),

  refineStart: (
    id: string,
    data: {
      language?: string;
      draft_profile_id?: string;
      refiner_profile_id?: string;
      checker_profile_id?: string | null;
      unload_after_call?: boolean;
      chapter_ids?: number[] | null;
      resume?: boolean;
      batch_words?: number;
      refine_rounds?: number;
      similarity_stop?: number;
      ratio_min?: number;
      ratio_max?: number;
      draft_temperature?: number;
      style_probe?: string;
      structured?: boolean;
      check_mode?: string;
    },
  ) =>
    request<{ status: any }>(`/api/books/${id}/refine`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  refineStatus: (id: string) =>
    request<{ status: Record<string, any> }>(`/api/books/${id}/refine/status`),
  refineProgress: (id: string) =>
    request<{
      mode: string | null;
      run_status: string;
      batches_total: number | null;
      chapters: {
        chapter_id: number;
        chapter_name: string;
        words_total: number;
        refined_words: number;
        batches_total: number;
        batches_done: number;
        paragraphs: number;
        review_flags: number;
        started: boolean;
        done: boolean;
        versions: number[];
        active_version: number | null;
      }[];
    }>(`/api/books/${id}/refine/progress`),
  refineStop: (id: string) =>
    request<{ status: any }>(`/api/books/${id}/refine/stop`, { method: "POST" }),
  refineOutputs: (id: string) =>
    request<{ outputs: { path: string; size: number; modified: number }[] }>(
      `/api/books/${id}/refine/outputs`,
    ),
  refineFile: (id: string, path: string) =>
    request<{ path: string; content: string }>(
      `/api/books/${id}/refine/file?path=${encodeURIComponent(path)}`,
    ),
  refineVersions: (id: string) =>
    request<{
      chapters: {
        chapter_id: number;
        chapter_name: string;
        original_words: number;
        active_version: number | null;
        versions: {
          version: number;
          created: string | null;
          words: number;
          review_flags: number;
          models: Record<string, string | null>;
          run_started_at: number | null;
        }[];
      }[];
    }>(`/api/books/${id}/refine/versions`),
  refineVersion: (id: string, chapterId: number, version?: number) =>
    request<Record<string, any>>(
      `/api/books/${id}/refine/version?chapter_id=${chapterId}${version ? `&version=${version}` : ""}`,
    ),
  refineSelectVersion: (id: string, chapterId: number, version: number) =>
    request<{ chapter_id: number; version: number; status: any }>(
      `/api/books/${id}/refine/versions/select`,
      { method: "POST", body: JSON.stringify({ chapter_id: chapterId, version }) },
    ),
  refineResetAll: (id: string) =>
    request<{ removed: string[] }>(`/api/books/${id}/refine/reset-all`, { method: "POST" }),

  outlineRefineStart: (
    id: string,
    data: {
      profile_id: string;
      chapter_ids?: number[] | null;
      resume?: boolean;
      refine_rounds?: number;
      similarity_stop?: number;
      context_prev?: number;
      language?: string;
      unload_after_call?: boolean;
      structured?: boolean;
      style_probe?: string;
    },
  ) =>
    request<{ status: any }>(`/api/books/${id}/outline-refine`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  outlineRefineStatus: (id: string) =>
    request<{ status: Record<string, any> }>(`/api/books/${id}/outline-refine/status`),
  outlineRefineStop: (id: string) =>
    request<{ status: any }>(`/api/books/${id}/outline-refine/stop`, { method: "POST" }),
  outlineRefineProgress: (id: string) =>
    request<{
      run_status: string;
      batches_total: number | null;
      chapters: {
        chapter_id: number;
        chapter_name: string;
        word_count: number;
        brief_words: number;
        started: boolean;
        done: boolean;
        review_notes: number;
        versions: number[];
        active_version: number | null;
      }[];
    }>(`/api/books/${id}/outline-refine/progress`),
  outlineRefineOutputs: (id: string) =>
    request<{ outputs: { path: string; size: number; modified: number }[] }>(
      `/api/books/${id}/outline-refine/outputs`,
    ),
  outlineRefineFile: (id: string, path: string) =>
    request<{ path: string; content: string }>(
      `/api/books/${id}/outline-refine/file?path=${encodeURIComponent(path)}`,
    ),
  outlineRefineVersions: (id: string) =>
    request<{
      chapters: {
        chapter_id: number;
        chapter_name: string;
        original_words: number;
        active_version: number | null;
        versions: {
          version: number;
          created: string | null;
          words: number;
          models: Record<string, string | null>;
        }[];
      }[];
    }>(`/api/books/${id}/outline-refine/versions`),
  outlineRefineVersion: (id: string, chapterId: number, version?: number) =>
    request<Record<string, any>>(
      `/api/books/${id}/outline-refine/version?chapter_id=${chapterId}${version ? `&version=${version}` : ""}`,
    ),
  outlineRefineSelectVersion: (id: string, chapterId: number, version: number) =>
    request<{ chapter_id: number; version: number; status: any }>(
      `/api/books/${id}/outline-refine/versions/select`,
      { method: "POST", body: JSON.stringify({ chapter_id: chapterId, version }) },
    ),
  outlineRefineResetAll: (id: string) =>
    request<{ removed: string[] }>(`/api/books/${id}/outline-refine/reset-all`, { method: "POST" }),

  wizardCreate: (
    data: {
      book: {
        title: string;
        story_prompt: string;
        language?: string;
        target_words?: number;
        genre_notes?: string;
        codex_min?: Record<string, any> | null;
      };
      steps: {
        step: number;
        kind: string;
        enabled?: boolean;
        planner_profile_id?: string | null;
        writer_profile_id?: string | null;
        candidate_profile_ids?: string[];
        judge_profile_id?: string | null;
        parallel?: boolean;
        refine_rounds?: number | null;
        similarity_stop?: number | null;
        context_prev?: number | null;
        style_probe?: string | null;
        draft_temperature?: number | null;
        batch_words?: number | null;
        target_language?: string | null;
        draft_profile_id?: string | null;
        refiner_profile_id?: string | null;
        checker_profile_id?: string | null;
      }[];
      pause_between_steps?: boolean;
    },
  ) =>
    request<{ book: Book; wizard_state: WizardState | null }>("/api/wizard", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  wizardList: () =>
    request<{ wizards: WizardSummary[] }>("/api/wizards"),
  wizardState: (id: string) =>
    request<{ wizard_state: WizardState | null }>(`/api/books/${id}/wizard/state`),
  wizardPause: (id: string) =>
    request<{ wizard_state: WizardState | null }>(`/api/books/${id}/wizard/pause`, { method: "POST" }),
  wizardResume: (id: string) =>
    request<{ wizard_state: WizardState | null }>(`/api/books/${id}/wizard/resume`, { method: "POST" }),
  wizardStop: (id: string) =>
    request<{ wizard_state: WizardState | null }>(`/api/books/${id}/wizard/stop`, { method: "POST" }),
  wizardRetryStep: (id: string, step: number) =>
    request<{ wizard_state: WizardState | null }>(`/api/books/${id}/wizard/retry-step`, {
      method: "POST",
      body: JSON.stringify({ step }),
    }),
  wizardPatchConfig: (id: string, patch: { pause_between_steps?: boolean; steps?: Record<string, any>[] }) =>
    request<{ wizard_state: WizardState | null }>(`/api/books/${id}/wizard/config`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  wizardDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/books/${id}/wizard`, { method: "DELETE" }),

  germanRefineStart: (
    id: string,
    data: {
      language?: string;
      profile_id?: string | null;
      candidate_profile_ids?: string[];
      judge_profile_id?: string | null;
      parallel_candidates?: boolean;
      unload_after_call?: boolean;
      resume?: boolean;
      batch_words?: number;
      refine_rounds?: number;
      similarity_stop?: number;
      structured?: boolean;
      style_probe?: string;
    },
  ) =>
    request<{ status: any }>(`/api/books/${id}/german-refine`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  germanRefineStatus: (id: string) =>
    request<{ status: Record<string, any> }>(`/api/books/${id}/german-refine/status`),
  germanRefineStop: (id: string) =>
    request<{ status: any }>(`/api/books/${id}/german-refine/stop`, { method: "POST" }),
  germanRefineProgress: (id: string) =>
    request<{
      language: string;
      run_status: string | null;
      batches_total: number | null;
      chapters: {
        chapter_id: number;
        chapter_name?: string;
        started?: boolean;
        done?: boolean;
        batches_total?: number;
        batches_done?: number;
        refined_words?: number;
        versions?: number[];
        active_version?: number | null;
      }[];
    }>(`/api/books/${id}/german-refine/progress`),
  germanRefineVersions: (id: string) =>
    request<{
      language: string;
      chapters: {
        chapter_id: number;
        chapter_name: string;
        active_version: number | null;
        versions: { version: number; created: string | null; words: number; models: Record<string, unknown> }[];
      }[];
    }>(`/api/books/${id}/german-refine/versions`),
  germanRefineVersion: (id: string, chapterId: number, version?: number, language?: string) =>
    request<Record<string, any>>(
      `/api/books/${id}/german-refine/version?chapter_id=${chapterId}${version ? `&version=${version}` : ""}${language ? `&language=${encodeURIComponent(language)}` : ""}`,
    ),
  germanRefineSelectVersion: (id: string, chapterId: number, version: number, language?: string) =>
    request<{ chapter_id: number; version: number; status: any }>(
      `/api/books/${id}/german-refine/versions/select`,
      { method: "POST", body: JSON.stringify({ chapter_id: chapterId, version, language }) },
    ),
  germanRefineResetAll: (id: string, language?: string) =>
    request<{ removed: string[] }>(
      `/api/books/${id}/german-refine/reset-all${language ? `?language=${encodeURIComponent(language)}` : ""}`,
      { method: "POST" },
    ),
  germanRefineFile: (id: string, path: string, language?: string) =>
    request<{ path: string; content: string }>(
      `/api/books/${id}/german-refine/file?path=${encodeURIComponent(path)}${language ? `&language=${encodeURIComponent(language)}` : ""}`,
    ),

  llmModels: (data: { profile_id?: string | null; base_url?: string | null; api_key?: string | null }) =>
    request<{ models: string[] }>("/api/llm/models", {
      method: "POST",
      body: JSON.stringify(data),
    }),
};

export function openEventStream(bookId: string, onEvent: (event: any) => void): () => void {
  const user = currentUser();
  const source = new EventSource(`${BASE}/api/books/${bookId}/events${user ? `?user=${encodeURIComponent(user.id)}` : ""}`);
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data));
    } catch {}
  };
  source.onerror = () => {};
  return () => source.close();
}