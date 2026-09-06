/**
 * Centralised localStorage key registry for the webui.
 *
 * Historically keys were declared inline in each module with slightly
 * different naming conventions (`erza-webui.`, `erza.webui.`,
 * `erza:`, `erza_debug_`). This object is the single source of
 * truth: new code should always reference `STORAGE_KEYS.*` rather than
 * hand-rolling a string literal.
 */
export const STORAGE_KEYS = {
  /** Auth bootstrap secret persisted between page reloads. */
  bootstrapSecret: "erza-webui.bootstrap-secret",
  /** Sidebar collapsed/expanded state. */
  sidebar: "erza-webui.sidebar",
  /** Sidebar "completed runs" badge tracking (versioned). */
  sidebarCompletedRuns: "erza-webui.sidebar.completed-runs.v1",
  /** Timestamp marking when a host restart was initiated.
   * 设计 §4.5: 使用 sessionStorage(单标签页),不允许其他标签页清除发起页
   * 的进行中状态。键名保留兼容,但读取/写入入口在 ``useRestartFlow`` 中
   * 统一切换到了 ``window.sessionStorage``。 */
  restartStartedAt: "erza-webui.restartStartedAt",
  /** Per-user UI density / activity / brand preferences. */
  settingsPreferences: "erza-webui.settings-preferences",
  /** Cached provider model lists (cleared on settings reload). */
  providerModels: "erza:providerModels",
  /** Last-selected UI theme. */
  theme: "erza-webui.theme",
  /** Selected i18n locale. */
  locale: "erza.locale",
  /** Recently-used slash commands (composer autocomplete). */
  slashCommandRecents: "erza.webui.slashCommandRecents",
  /** Debug flag for the WebSocket client. */
  debugWs: "erza_debug_ws",
} as const;

export type StorageKey = (typeof STORAGE_KEYS)[keyof typeof STORAGE_KEYS];
