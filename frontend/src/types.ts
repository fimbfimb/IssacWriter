export type Profile = {
  id: string;
  name: string;
  base_url: string;
  api_key_set: boolean;
  api_key: string;
  model: string;
  temperature: number;
  max_output_tokens: number;
  price_in_per_m: number;
  price_out_per_m: number;
  thinking: string;
  extra_params: Record<string, any>;
};

export type CodexEntry = {
  name: string;
  aliases: string[];
  description: string;
  role?: string;
  color?: string;
};

export type Codex = {
  characters: CodexEntry[];
  settings: CodexEntry[];
  lore: CodexEntry[];
};

export type OutlineChapter = {
  id: number;
  name: string;
  description: string;
  word_count: number;
};

export type Outline = {
  premise?: string;
  synopsis?: string;
  acts?: string;
  act_sections?: { title: string; description: string }[];
  chapters?: OutlineChapter[];
  frozen?: boolean;
  target_words?: number;
};

export type RunSettings = {
  mode: "auto" | "review" | "single";
  temperature: number;
  dynamic_tokens: boolean;
  hard_max_output_tokens: number;
  max_turns: number;
  max_attempts_per_chapter: number;
};

export type Book = {
  id: string;
  title: string;
  story_prompt: string;
  genre_notes: string;
  language: string;
  target_words: number;
  codex: Codex;
  outline: Outline | null;
  status: string;
  planner_profile_id: string | null;
  writer_profile_id: string | null;
  run_settings: RunSettings;
  created: string;
  updated: string;
  words_written?: number;
  chapters_written?: number;
  runner?: RunnerStatus;
  translation?: TranslationStatus;
  translation_settings?: Record<string, any>;
  author_notes?: string;
  last_error?: string;
};

export type BookSummary = {
  id: string;
  title: string;
  language: string;
  target_words: number;
  status: string;
  created: string;
  updated: string;
  chapters_written: number;
  words_written: number;
};

export type RunnerStatus = {
  status: string;
  error: string | null;
  current_chapter: number | null;
  task_alive?: boolean;
};

export type TranslationToolStatus = {
  tool_dir: string;
  tool_present: boolean;
  venv_python: string | null;
  ready: boolean;
};

export type TranslationStatus = {
  status: string;
  error: string | null;
  output_file: string | null;
  language: string | null;
  profile_name: string | null;
  model: string | null;
  batch_size: number;
  started_at: number | null;
  finished_at: number | null;
  interrupted: boolean;
  task_alive?: boolean;
  tool: TranslationToolStatus;
};

export type ReviewDecision = {
  action: string;
  decided_at: string;
  edited_text?: string;
};

export type ReviewRow = {
  id: string;
  batch_index: number;
  chapter_id: number;
  chapter_name: string;
  type: string;
  paragraph: number | null;
  detail: string;
  source_en: string;
  text_de: string;
  stage: string | null;
  decision: ReviewDecision | null;
  history_count: number;
  editable: boolean;
};

export type ReviewResponse = {
  mode: string | null;
  language: string;
  open_flags: number;
  total_flags: number;
  chapters: {
    chapter_id: number;
    chapter_name: string;
    rows: ReviewRow[];
  }[];
  report_path?: string | null;
  hint?: string;
  decided?: ReviewDecision;
};

export type SequelProposal = {
  summary_ending: string;
  open_threads: string[];
  characters: { name: string; codex_id?: string; alive: boolean | null; reason: string }[];
  proposed_title: string;
  story_prompt: string;
  author_notes: string;
  extraction_error?: string;
};

export type LayoutIllustration = {
  id: string;
  chapter_id: number | null;
  paragraph_index: number | null;
  anchor_word_offset?: number;
  scene?: string;
  location?: string;
  setting?: string;
  prompt?: string;
  image_path?: string;
  anchor_valid?: boolean;
  retired?: boolean;
  near_duplicate?: boolean;
  people_in_scene?: string[];
};

export type ExtractVisualResult = {
  name: string;
  status: "ok" | "no_text" | "error";
  visual?: string;
  detail?: string;
  error?: string;
};

export type LayoutData = {
  style_directive: string;
  words_per_page: number;
  images_every_n_pages: number;
  cover_image_path: string;
  characters: { name: string; visual: string }[];
  illustrations: LayoutIllustration[];
  book_stats?: {
    total_words: number;
    spacing_words?: number;
    per_chapter?: {
      chapter_id: number;
      chapter_name: string;
      words: number;
      start_offset?: number;
      paragraphs: number;
    }[];
  };
};

export type LayoutResponse = {
  layout: LayoutData;
  stats: {
    total_words: number;
    per_chapter: { chapter_id: number; chapter_name: string; words: number; paragraphs: number }[];
  };
};

export type ChapterRecord = {
  id: number;
  name: string;
  content?: string;
  status?: string;
  target?: number;
  written_words?: number;
  attempts?: number;
  tool_counts?: Record<string, number>;
  update_info?: Record<string, any>;
  usage?: { prompt_tokens: number; completion_tokens: number; cost: number };
  accepted_at?: string;
};

export type StateCollection = {
  character_states: { id: number; name: string; description: string }[];
  past_events: { id: number; key?: string; description: string }[];
  future_requirements: { id: number; key?: string; description: string }[];
};

export type Stats = {
  book_id: string;
  title: string;
  target_words: number;
  chapters_written: number;
  chapters_planned: number;
  total_words: number;
  coverage_words: number;
  usage_totals: { prompt_tokens: number; completion_tokens: number; cost: number };
  tool_totals: Record<string, number>;
  per_chapter: {
    chapter_id: number;
    name: string;
    words: number;
    target: number | null;
    cumulative: number;
    cost?: number;
    cumulative_cost?: number;
    prompt_tokens?: number;
    completion_tokens?: number;
    tool_counts?: Record<string, number>;
  }[];
  characters: {
    name: string;
    role: string;
    description: string;
    color: string;
    mentions: Record<string, number>;
    total_mentions: number;
    first_chapter: number | null;
    last_chapter: number | null;
    spans: [number, number][];
    tracked_since: number | null;
  }[];
  settings: {
    name: string;
    description: string;
    color: string;
    mentions: Record<string, number>;
    total_mentions: number;
    first_chapter: number | null;
    last_chapter: number | null;
    spans: [number, number][];
  }[];
  threads: { key: string; opened_at: number | null; resolved_at: number | null }[];
  open_threads: { key: string; opened_at: number | null }[];
  state_now: StateCollection;
};