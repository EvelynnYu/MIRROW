export type ThinkingStep = {
  content: string;
  timestamp: string; // ISO格式
};

export type ImageAttachment = {
  id: string;
  url: string;
  filename: string;
  size: number;
  type: string;
};

export type ToolCallRecord = {
  tool: string;
  /** New receipts keep their event identity and occurrence time in tool_calls. */
  event_id?: string;
  completed_at?: string;
  parameters?: Record<string, unknown>;
  success?: boolean;
  description: string;
  result?: string;
  /** Checkup receipts retain an explicit outcome instead of inferring success from text. */
  status?: 'executing' | 'success' | 'partial' | 'failure' | 'rejected' | 'skipped' | string;
  error?: string;
  extra_data?: Record<string, any>;
};

export type VoiceAttachment = {
  path: string;
  text: string;
  duration: number;
  tone?: string;
};

export type LongTextCard = { id: string; title: string; body: string; kind?: 'music' | 'xhs'; music_type?: 'song' | 'playlist'; song_id?: string; playlist_id?: string; artist?: string; cover?: string; duration?: string; link?: string; post_key?: string; link_status?: string; source_status?: string; node_id?: string; comment_draft?: string; share_number?: string; share_count?: string; track_count?: string; track_preview?: string; play_mode?: 'list' | 'loop' | 'one'; lyrics_available?: string; lyrics_excerpt?: string; melody_summary?: string; material_status?: string };

export type Message = {
  modelSource?: {station_id:string;station_name:string;source_id:string;model:string;model_label:string;ordinal:number};
  id: string;
  role: 'user' | 'assistant' | 'divider' | 'parallel_summary' | 'system' | 'notification' | 'event';
  isWander?: boolean;
  isSentinel?: boolean;
  isReminder?: boolean;
  isMusicCochlea?: boolean;  // 音乐耳蜗消息标记
  isMusic?: boolean;
  /** 群聊摘要的结构化 UI 标记（由 eventType/WS 派生） */
  summaryText?: string;
  summaryAt?: string;
  eventType?: string;
  eventParentId?: string;
  content: string;
  timestamp: Date;
  rowOrder?: number;
  thinkingChain?: ThinkingStep[];
  images?: ImageAttachment[];
  toolCalls?: ToolCallRecord[];
  voiceAttachment?: VoiceAttachment;
  reasoning_content?: string;
  longTextCard?: LongTextCard;
  /** Routing facts for UI-only notifications; never sent to the LLM. */
  notificationMetadata?: Record<string, any>;
  /** UI-only carrier whose tool details moved into a persisted receipt. */
  uiReceiptCarrier?: boolean;
};

// ── 从 App.tsx 下沉的共享类型 ─────────────────────────

export type ModelType = 'deepseek' | 'glm';
export type ImageModelType = 'glm' | 'deepseek' | 'flash-vision';

export type Persona = {
  name: string;
  avatar: string; // emoji or URL
  systemPrompt: string;
  nickname?: string;
  chatBackground?: string; // data URL or backend image URL
  chatBackgroundOpacity?: number;
  userName?: string;
  imageStylePrompt?: string;
  aiAppearancePrompt?: string;
  userAppearancePrompt?: string;
  aiImageReference?: { url: string; filename: string };
  userImageReference?: { url: string; filename: string };
  bothImageReference?: { url: string; filename: string };
};

export type Session = {
  id: string;
  title: string;
  messages: Message[];
  createdAt: Date;
  updatedAt: Date;
  locked?: boolean;
  isPrimary?: boolean;
  // 窗口 / 翻页状态：本地 messages 只保留已加载的窗口（默认最近 200 条），
  // 而不是整个会话历史。historyCursor 是「再往前一页」的游标。
  historyCursor?: string | null;   // null 表示已翻到会话开头
  historyCursorTs?: string;        // 游标 id 失效时的兜底比较值
  hasMoreHistory?: boolean;
  tailStartId?: string;
  tailStartTs?: string;
  tailStartRowId?: number;
  totalMessages?: number;
};

export type TopicStatus = {
  is_active: boolean;
  start_time: string | null;
  message_count: number;
  round_count: number;
};

export type SummarizedStatus = {
  session_id: string;
  summarized_ids: string[];
  total_count: number;
};

export type WanderStatus = {
  mode: string;  // offline, idle, brainstorming
  mode_display: string;
  last_user_reply_time: string | null;
  idle_seconds: number;
  idle_threshold: number;
  is_wander_mode_active: boolean;
  current_event_type: string | null;
  shelf_sources?: string[];
  current_activity_state?: string;
  runtime_state?: string;
  active_run_state?: string;
  next_node_at?: string | null;
  timer_ends_at?: string | null;
  next_plan_at?: string;
  wake_reason?: string;
  creator_running?: boolean;
  user_status?: string;
  user_status_display?: string;
  user_status_custom_text?: string;
  user_status_presets?: Record<string, string[]>;
};

export type SystemStatus = {
  timestamp: string;
  backend_status: string;
  ombre_brain_status: string;
  sentence_transformer_status: string;
  vector_index_stats: Record<string, any>;
  bucket_stats: Record<string, any>;
  migration_status: Record<string, any>;
  model_api_status: Record<string, any>;
  scheduler_status: Record<string, any>;
  external_services: Record<string, any>;
  context_pipeline: { last_run?: string; steps?: Record<string, { status: string; duration_ms: number; summary: string; error: string }> };
  post_topic_health: Record<string, any>;
  diary_health: Record<string, any>;
  cron_health: Record<string, any>;
  memory_health: Record<string, any>;
  reminders_health: Record<string, any>;
  components_health: Record<string, any>;
  sessions_health: Record<string, any>;
  keyword_pool_stats: Record<string, any>;
  token_stats: Record<string, any>;
  context_snapshot?: {
    timestamp: string;
    total_tokens: number;
    total_chars: number;
    messages_count: number;
    messages: Array<{ index: number; role: string; tokens: number; chars: number; content: string }>;
    system_sections: Record<string, { text: string; tokens: number; chars: number }>;
    system_tokens: number;
    system_chars: number;
  };
  keyword_pools?: {
    trigger_keywords: string[];
    world_book_keywords: string[];
    world_book_entry_count: number;
    error?: string;
  };
  recipe_call_log?: Record<string, unknown>;
  recipe_call_log_observation?: { status?: string; error_type?: string };
};

export type ToolCallStatus = {
  tool: string;
  status: 'executing' | 'complete';
  description: string;
};

export type TaskItem = {
  id: string;
  title: string;
  description: string;
  reason: string;
  ai_context: string;
  task_type: string;
  trigger_type: string;
  trigger_config: { cron?: string; datetime?: string };
  notify_method: string[];
  alarm_enabled: boolean;
  message_content: string;
  is_active: boolean;
  is_ai_generated: boolean;
  created_by: string;
  tool_name: string;
  execution_prompt: string;
  last_triggered_at: string | null;
  created_at: string;
  updated_at: string;
};

export type TaskFormData = {
  title: string;
  description: string;
  reason: string;
  ai_context: string;
  trigger_type: 'cron' | 'datetime';
  trigger_config: { cron?: string; datetime?: string };
  notify_method: string[];
  alarm_enabled: boolean;
  message_content: string;
  task_type: string;
};

// 通知小卡片
export type NotificationCard = {
  id: string;
  title: string;
  message: string;
  x: number;
  y: number;
  createdAt: number;
};

// 纪念日确认建议
export type MemorialSuggestion = {
  id: string;
  title: string;
  description: string;
  date_string: string;
};
