/**
 * Centralised localStorage key registry for the webui.
 *
 * Historically keys were declared inline in each module with slightly
 * different naming conventions (`miniunicorn-webui.`, `miniunicorn.webui.`,
 * `miniunicorn:`, `miniunicorn_debug_`). This object is the single source of
 * truth: new code should always reference `STORAGE_KEYS.*` rather than
 * hand-rolling a string literal.
 */
export const STORAGE_KEYS = {
  /** Auth bootstrap secret persisted between page reloads. */
  bootstrapSecret: "miniunicorn-webui.bootstrap-secret",
  /** Sidebar collapsed/expanded state. */
  sidebar: "miniunicorn-webui.sidebar",
  /** Sidebar "completed runs" badge tracking (versioned). */
  sidebarCompletedRuns: "miniunicorn-webui.sidebar.completed-runs.v1",
  /** Timestamp marking when a host restart was initiated. */
  restartStartedAt: "miniunicorn-webui.restartStartedAt",
  /** Per-user UI density / activity / brand preferences. */
  settingsPreferences: "miniunicorn-webui.settings-preferences",
  /** Cached provider model lists (cleared on settings reload). */
  providerModels: "miniunicorn:providerModels",
  /** Last-selected UI theme. */
  theme: "miniunicorn-webui.theme",
  /** Selected i18n locale. */
  locale: "miniunicorn.locale",
  /** Recently-used slash commands (composer autocomplete). */
  slashCommandRecents: "miniunicorn.webui.slashCommandRecents",
  /** Debug flag for the WebSocket client. */
  debugWs: "miniunicorn_debug_ws",
} as const;

export type StorageKey = (typeof STORAGE_KEYS)[keyof typeof STORAGE_KEYS];
