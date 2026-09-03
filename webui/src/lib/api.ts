import type {
  AgentDetail,
  AgentsPayload,
  ChannelQrBeginPayload,
  ChannelQrStatusPayload,
  ChannelsPayload,
  ChatSummary,
  CliAppsPayload,
  CronJobCreate,
  CronJobPayload,
  CronJobsPayload,
  ImageGenerationSettingsUpdate,
  McpPresetsPayload,
  ModelConfigurationCreate,
  ModelConfigurationUpdate,
  NetworkSafetySettingsUpdate,
  ProviderSettingsUpdate,
  RuntimeSettingsUpdate,
  SettingsPayload,
  SettingsUpdate,
  SidebarStatePayload,
  SkillDetail,
  SkillFilePayload,
  SkillsPayload,
  SlashCommand,
  ToolImportResult,
  ToolsPayload,
  WebFetchSettingsUpdate,
  WebSearchSettingsUpdate,
  WorkspacesPayload,
  WebuiThreadPersistedPayload,
  WorkspaceScopePayload,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(url, {
    ...(init ?? {}),
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
    credentials: "same-origin",
  });
  if (!res.ok) {
    const text = typeof res.text === "function" ? (await res.text()).trim() : "";
    throw new ApiError(res.status, text || `HTTP ${res.status}`);
  }
  const contentType = res.headers?.get?.("content-type") ?? "";
  if (contentType && !contentType.toLowerCase().includes("application/json")) {
    const text = typeof res.text === "function" ? await res.text() : "";
    const isHtml = text.trimStart().toLowerCase().startsWith("<!doctype");
    throw new ApiError(
      res.status,
      isHtml
        ? "Gateway returned WebUI HTML instead of JSON. Restart MiniUnicorn gateway and try again."
        : "Gateway returned a non-JSON response.",
    );
  }
  return (await res.json()) as T;
}

function mcpValuesHeader(values: Record<string, unknown>): HeadersInit | undefined {
  const payload: Record<string, unknown> = {};
  Object.entries(values).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) payload[key] = trimmed;
      return;
    }
    payload[key] = value;
  });
  if (!Object.keys(payload).length) return undefined;
  return { "x-miniunicorn-MCP-Values": JSON.stringify(payload) };
}

/**
 * Build the `x-miniunicorn-Values` header used to carry sensitive fields
 * (api_key, api_base, config, etc.) out of the URL query string. The backend
 * merges these into `ctx.query` transparently via `_merge_sensitive_values_header`.
 * A single JSON string header is fine — `collect_chunked_header_limited` on the
 * backend handles chunking if we ever need to split.
 */
function sensitiveValuesHeader(values: Record<string, unknown>): HeadersInit | undefined {
  const payload: Record<string, unknown> = {};
  Object.entries(values).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) payload[key] = trimmed;
      return;
    }
    payload[key] = value;
  });
  if (!Object.keys(payload).length) return undefined;
  return { "x-miniunicorn-Values": JSON.stringify(payload) };
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
    run_started_at?: number | null;
    workspace_scope?: WorkspaceScopePayload | null;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    title: s.title ?? "",
    preview: s.preview ?? "",
    runStartedAt: s.run_started_at ?? null,
    workspaceScope: s.workspace_scope ?? null,
  }));
}

/** Disk-backed WebUI display thread snapshot (separate from agent session). */
export async function fetchWebuiThread(
  token: string,
  key: string,
  base: string = "",
): Promise<WebuiThreadPersistedPayload | null> {
  const url = `${base}/api/sessions/${encodeURIComponent(key)}/webui-thread`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as WebuiThreadPersistedPayload;
}

export async function deleteSession(
  token: string,
  key: string,
  base: string = "",
): Promise<boolean> {
  const body = await request<{ deleted: boolean }>(
    `${base}/api/sessions/${encodeURIComponent(key)}/delete`,
    token,
  );
  return body.deleted;
}

export interface RewindResult {
  rewound: boolean;
  transcriptLinesRemoved: number;
  sessionMessagesRemoved: number;
}

/**
 * Truncate a websocket session to before the N-th user message (0-based).
 * Both the WebUI transcript and the agent session file are truncated, and a
 * ``session_updated`` event is broadcast so the live thread view refreshes.
 */
export async function rewindSession(
  token: string,
  key: string,
  userMessageIndex: number,
  base: string = "",
): Promise<RewindResult> {
  const query = new URLSearchParams();
  query.set("user_message_index", String(userMessageIndex));
  const body = await request<{
    rewound: boolean;
    transcript_lines_removed: number;
    session_messages_removed: number;
  }>(
    `${base}/api/sessions/${encodeURIComponent(key)}/rewind?${query}`,
    token,
  );
  return {
    rewound: body.rewound,
    transcriptLinesRemoved: body.transcript_lines_removed,
    sessionMessagesRemoved: body.session_messages_removed,
  };
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(`${base}/api/settings`, token);
}

export async function fetchWorkspaces(
  token: string,
  base: string = "",
): Promise<WorkspacesPayload> {
  return request<WorkspacesPayload>(`${base}/api/workspaces`, token);
}

export async function fetchCliApps(
  token: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(`${base}/api/settings/cli-apps`, token);
}

export async function runCliAppAction(
  token: string,
  action: "install" | "update" | "uninstall" | "test",
  name: string,
  base: string = "",
): Promise<CliAppsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<CliAppsPayload>(`${base}/api/settings/cli-apps/${action}?${query}`, token);
}

export async function fetchMcpPresets(
  token: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(`${base}/api/settings/mcp-presets`, token);
}

export async function runMcpPresetAction(
  token: string,
  action: "enable" | "remove" | "test",
  name: string,
  values: Record<string, string> = {},
  base: string = "",
): Promise<McpPresetsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/${action}?${query}`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function saveCustomMcpServer(
  token: string,
  values: Record<string, string>,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/custom`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function importMcpConfig(
  token: string,
  config: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/import`,
    token,
    { headers: mcpValuesHeader({ config }) },
  );
}

export async function updateMcpServerTools(
  token: string,
  name: string,
  enabledTools: string[],
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/tools`,
    token,
    { headers: mcpValuesHeader({ name, enabled_tools: enabledTools }) },
  );
}

export async function fetchSkills(
  token: string,
  base: string = "",
): Promise<SkillsPayload> {
  return request<SkillsPayload>(`${base}/api/skills`, token);
}

export async function deleteSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<{ deleted: boolean; name: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<{ deleted: boolean; name: string }>(
    `${base}/api/skills/delete?${query}`,
    token,
  );
}

/**
 * Split a string into URL-encoded chunks small enough to fit in a single HTTP
 * header line (the websockets HTTP layer caps lines at 8192 bytes). Returns an
 * array of header values to be sent as repeated headers.
 */
const HEADER_CHUNK_SIZE = 6000;
function chunkHeaderValue(raw: string): string[] {
  const encoded = encodeURIComponent(raw);
  const chunks: string[] = [];
  for (let i = 0; i < encoded.length; i += HEADER_CHUNK_SIZE) {
    chunks.push(encoded.slice(i, i + HEADER_CHUNK_SIZE));
  }
  return chunks.length ? chunks : [""];
}

export async function toggleSkill(
  token: string,
  name: string,
  disabled: boolean,
  base: string = "",
): Promise<{ name: string; disabled: boolean; disabled_skills: string[] }> {
  const query = new URLSearchParams();
  query.set("name", name);
  query.set("disabled", disabled ? "true" : "false");
  return request<{ name: string; disabled: boolean; disabled_skills: string[] }>(
    `${base}/api/skills/toggle?${query}`,
    token,
  );
}

export async function readSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillDetail> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<SkillDetail>(`${base}/api/skills/read?${query}`, token);
}

export async function readSkillFile(
  token: string,
  name: string,
  path: string,
  base: string = "",
): Promise<SkillFilePayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  query.set("path", path);
  return request<SkillFilePayload>(`${base}/api/skills/file?${query}`, token);
}

export async function saveSkill(
  token: string,
  name: string,
  content: string,
  base: string = "",
): Promise<{ saved: boolean; name: string; path: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  const headers: Record<string, string> = {};
  chunkHeaderValue(content).forEach((chunk, idx) => {
    // Repeated header name; the backend joins values in order.
    if (idx === 0) {
      headers["x-miniunicorn-Skill-Content"] = chunk;
    } else {
      headers[`x-miniunicorn-Skill-Content-${idx}`] = chunk;
    }
  });
  return request<{ saved: boolean; name: string; path: string }>(
    `${base}/api/skills/save?${query}`,
    token,
    { headers },
  );
}

export interface BootstrapFilePayload {
  name: string;
  content: string;
  exists: boolean;
}

export interface DreamFileEntry {
  name: string;
  exists: boolean;
  size: number;
  size_human: string;
  modified_at: number | null;
  modified_at_human: string;
}

export interface DreamFilesPayload {
  files: DreamFileEntry[];
}

export interface DreamFilePayload {
  name: string;
  content: string;
  exists: boolean;
}

export async function listDreamFiles(
  token: string,
  base: string = "",
): Promise<DreamFilesPayload> {
  return request<DreamFilesPayload>(`${base}/api/dream/files`, token);
}

export async function readDreamFile(
  token: string,
  name: string,
  base: string = "",
): Promise<DreamFilePayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<DreamFilePayload>(
    `${base}/api/dream/file?${query}`,
    token,
  );
}

export async function readBootstrapFile(
  token: string,
  name: string,
  base: string = "",
): Promise<BootstrapFilePayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<BootstrapFilePayload>(
    `${base}/api/bootstrap-file?${query}`,
    token,
  );
}

export async function saveBootstrapFile(
  token: string,
  name: string,
  content: string,
  base: string = "",
): Promise<{ saved: boolean; name: string; path: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  const headers: Record<string, string> = {};
  chunkHeaderValue(content).forEach((chunk, idx) => {
    if (idx === 0) {
      headers["x-miniunicorn-Bootstrap-Content"] = chunk;
    } else {
      headers[`x-miniunicorn-Bootstrap-Content-${idx}`] = chunk;
    }
  });
  return request<{ saved: boolean; name: string; path: string }>(
    `${base}/api/bootstrap-file/save?${query}`,
    token,
    { headers },
  );
}

export async function uploadSkillZip(
  token: string,
  file: File | Blob,
  preferredName?: string,
  base: string = "",
): Promise<{ uploaded: boolean; name: string }> {
  const query = new URLSearchParams();
  if (preferredName) query.set("name", preferredName);
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  // Base64-encode in chunks small enough to fit under the 8KB header line.
  const CHUNK_BYTES = 4000; // ~5.3KB base64, safe under 8KB line limit
  const headers: Record<string, string> = {};
  let headerIdx = 0;
  for (let i = 0; i < bytes.length; i += CHUNK_BYTES) {
    const slice = bytes.slice(i, i + CHUNK_BYTES);
    let binary = "";
    for (let j = 0; j < slice.length; j++) {
      binary += String.fromCharCode(slice[j]);
    }
    const b64 = btoa(binary);
    if (headerIdx === 0) {
      headers["x-miniunicorn-Skill-Zip"] = b64;
    } else {
      headers[`x-miniunicorn-Skill-Zip-${headerIdx}`] = b64;
    }
    headerIdx++;
  }
  return request<{ uploaded: boolean; name: string }>(
    `${base}/api/skills/upload?${query}`,
    token,
    { headers },
  );
}

export async function fetchAgents(
  token: string,
  base: string = "",
): Promise<AgentsPayload> {
  return request<AgentsPayload>(`${base}/api/agents`, token);
}

export async function readAgent(
  token: string,
  name: string,
  base: string = "",
): Promise<AgentDetail> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<AgentDetail>(`${base}/api/agents/read?${query}`, token);
}

export async function saveAgent(
  token: string,
  name: string,
  content: string,
  base: string = "",
): Promise<{ saved: boolean; name: string; path: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  const headers: Record<string, string> = {};
  chunkHeaderValue(content).forEach((chunk, idx) => {
    if (idx === 0) {
      headers["x-miniunicorn-Agent-Content"] = chunk;
    } else {
      headers[`x-miniunicorn-Agent-Content-${idx}`] = chunk;
    }
  });
  return request<{ saved: boolean; name: string; path: string }>(
    `${base}/api/agents/save?${query}`,
    token,
    { headers },
  );
}

export async function deleteAgent(
  token: string,
  name: string,
  base: string = "",
): Promise<{ deleted: boolean; name: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<{ deleted: boolean; name: string }>(
    `${base}/api/agents/delete?${query}`,
    token,
  );
}

/** AI-generate a subagent definition (.md) from a natural-language description.
 * Returns the markdown content and the proposed agent name. */
export async function generateAgent(
  description: string,
  token: string,
  base: string = "",
): Promise<{ content: string; name: string }> {
  const headers: Record<string, string> = {};
  chunkHeaderValue(description).forEach((chunk, idx) => {
    if (idx === 0) {
      headers["x-miniunicorn-Agent-Description"] = chunk;
    } else {
      headers[`x-miniunicorn-Agent-Description-${idx}`] = chunk;
    }
  });
  return request<{ content: string; name: string }>(
    `${base}/api/agents/generate`,
    token,
    { headers },
  );
}

export async function listSlashCommands(
  token: string,
  base: string = "",
): Promise<SlashCommand[]> {
  type Row = {
    command: string;
    title: string;
    description: string;
    icon: string;
    arg_hint?: string;
  };
  const body = await request<{ commands: Row[] }>(`${base}/api/commands`, token);
  return body.commands
    .filter((command) => !["/stop", "/restart"].includes(command.command))
    .map((command) => ({
      command: command.command,
      title: command.title,
      description: command.description,
      icon: command.icon,
      argHint: command.arg_hint ?? "",
    }));
}

export async function fetchSidebarState(
  token: string,
  base: string = "",
): Promise<SidebarStatePayload> {
  return request<SidebarStatePayload>(`${base}/api/webui/sidebar-state`, token);
}

export async function updateSidebarState(
  token: string,
  state: SidebarStatePayload,
  base: string = "",
): Promise<SidebarStatePayload> {
  const query = new URLSearchParams();
  query.set("state", JSON.stringify(state));
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state/update?${query}`,
    token,
  );
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.modelPreset !== undefined) {
    query.set("model_preset", update.modelPreset ?? "default");
  }
  if (update.model !== undefined) query.set("model", update.model);
  if (update.provider !== undefined) query.set("provider", update.provider);
  if (update.contextWindowTokens !== undefined) {
    query.set("context_window_tokens", String(update.contextWindowTokens));
  }
  if (update.toolHintMaxLength !== undefined) {
    query.set("tool_hint_max_length", String(update.toolHintMaxLength));
  }
  if (update.usePlanner !== undefined) {
    query.set("use_planner", String(update.usePlanner));
  }
  if (update.plannerModel !== undefined) {
    query.set("planner_model", update.plannerModel ?? "");
  }
  return request<SettingsPayload>(`${base}/api/settings/update?${query}`, token);
}

export async function createModelConfiguration(
  token: string,
  configuration: ModelConfigurationCreate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (configuration.name !== undefined) query.set("name", configuration.name);
  query.set("label", configuration.label);
  query.set("provider", configuration.provider);
  query.set("model", configuration.model);
  const sensitive = sensitiveValuesHeader({
    api_key: configuration.apiKey,
    api_base: configuration.apiBase,
  });
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/create?${query}`,
    token,
    { headers: sensitive ?? {} },
  );
}

export async function updateModelConfiguration(
  token: string,
  configuration: ModelConfigurationUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("name", configuration.name);
  if (configuration.label !== undefined) query.set("label", configuration.label);
  if (configuration.provider !== undefined) query.set("provider", configuration.provider);
  if (configuration.model !== undefined) query.set("model", configuration.model);
  if (configuration.contextWindowTokens !== undefined) {
    query.set("context_window_tokens", String(configuration.contextWindowTokens));
  }
  const sensitive = sensitiveValuesHeader({
    api_key: configuration.apiKey,
    api_base: configuration.apiBase,
  });
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/update?${query}`,
    token,
    { headers: sensitive ?? {} },
  );
}

export async function deleteModelConfiguration(
  token: string,
  name: string,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/delete?${query}`,
    token,
  );
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  const sensitive = sensitiveValuesHeader({
    api_key: update.apiKey,
    api_base: update.apiBase,
    api_type: update.apiType,
  });
  return request<SettingsPayload>(
    `${base}/api/settings/provider/update?${query}`,
    token,
    { method: "GET", headers: sensitive ?? {} },
  );
}

export async function deleteProviderSettings(
  token: string,
  provider: string,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/delete?${query}`,
    token,
  );
}

export async function deleteAllProviders(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/providers/delete-all`,
    token,
  );
}

export async function fetchProviderModels(
  token: string,
  provider: string,
  options?: { apiKey?: string; apiBase?: string },
  base: string = "",
): Promise<string[]> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  const sensitive = sensitiveValuesHeader({
    api_key: options?.apiKey,
    api_base: options?.apiBase,
  });
  const body = await request<{ provider: string; models: string[] }>(
    `${base}/api/settings/provider/models?${query}`,
    token,
    { headers: sensitive ?? {} },
  );
  return body.models;
}

export async function updateWebFetchSettings(
  token: string,
  update: WebFetchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("use_jina_reader", String(update.useJinaReader));
  return request<SettingsPayload>(
    `${base}/api/settings/web-fetch/update?${query}`,
    token,
  );
}

export async function updateImageGenerationSettings(
  token: string,
  update: ImageGenerationSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enable", String(update.enabled));
  query.set("preset", update.preset);
  query.set("default_aspect_ratio", update.defaultAspectRatio);
  query.set("default_image_size", update.defaultImageSize);
  query.set("max_images_per_turn", String(update.maxImagesPerTurn));
  query.set("save_dir", update.saveDir);
  return request<SettingsPayload>(
    `${base}/api/settings/image-generation/update?${query}`,
    token,
  );
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enable", String(update.enable));
  query.set("provider", update.provider);
  query.set("max_results", String(update.max_results));
  query.set("timeout", String(update.timeout));
  // backends 嵌套结构通过 JSON 字符串传递
  // 空 backends 对象传 "{}" 以便后端清空;只在有非空 api_key 时包含该后端
  const backendsObj: Record<string, { api_key?: string; base_url: string; timeout: number }> = {};
  for (const [name, draft] of Object.entries(update.backends)) {
    backendsObj[name] = {
      base_url: draft.base_url,
      timeout: draft.timeout,
    };
    if (draft.api_key) {
      backendsObj[name].api_key = draft.api_key;
    }
  }
  // proxy and backends_json (which may contain api_key values) are sensitive —
  // move them out of the URL query string into the x-miniunicorn-Values header.
  const sensitive = sensitiveValuesHeader({
    proxy: update.proxy,
    backends_json: JSON.stringify(backendsObj),
  });
  return request<SettingsPayload>(
    `${base}/api/settings/web-search/update?${query}`,
    token,
    { headers: sensitive ?? {} },
  );
}

export async function updateNetworkSafetySettings(
  token: string,
  update: NetworkSafetySettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("webui_allow_local_service_access", String(update.webuiAllowLocalServiceAccess));
  query.set("webui_default_access_mode", update.webuiDefaultAccessMode);
  return request<SettingsPayload>(
    `${base}/api/settings/network-safety/update?${query}`,
    token,
  );
}

export async function updateRuntimeSettings(
  token: string,
  update: RuntimeSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.heartbeatIntervalS !== undefined) {
    query.set("heartbeat_interval_s", String(update.heartbeatIntervalS));
  }
  if (update.dreamCron !== undefined) {
    query.set("dream_cron", update.dreamCron);
  }
  if (update.heartbeatModelPreset !== undefined) {
    query.set("heartbeat_model_preset", update.heartbeatModelPreset);
  }
  if (update.heartbeatLightContext !== undefined) {
    query.set("heartbeat_light_context", String(update.heartbeatLightContext));
  }
  if (update.heartbeatIsolatedSession !== undefined) {
    query.set("heartbeat_isolated_session", String(update.heartbeatIsolatedSession));
  }
  if (update.heartbeatActiveHoursStart !== undefined) {
    query.set("heartbeat_active_hours_start", update.heartbeatActiveHoursStart);
  }
  if (update.heartbeatActiveHoursEnd !== undefined) {
    query.set("heartbeat_active_hours_end", update.heartbeatActiveHoursEnd);
  }
  return request<SettingsPayload>(
    `${base}/api/settings/runtime/update?${query}`,
    token,
  );
}

export async function fetchCronJobs(
  token: string,
  base: string = "",
): Promise<CronJobsPayload> {
  return request<CronJobsPayload>(`${base}/api/cron/jobs`, token);
}

export async function createCronJob(
  token: string,
  job: CronJobCreate,
  base: string = "",
): Promise<CronJobPayload> {
  const query = new URLSearchParams();
  query.set("name", job.name);
  query.set("message", job.message);
  query.set("schedule", job.schedule);
  if (job.schedule === "every" && job.everySeconds !== undefined) {
    query.set("every_seconds", String(job.everySeconds));
  }
  if (job.schedule === "cron") {
    if (job.cronExpr) query.set("cron_expr", job.cronExpr);
    if (job.tz) query.set("tz", job.tz);
  }
  if (job.schedule === "at" && job.atMs !== undefined) {
    query.set("at_ms", String(job.atMs));
  }
  if (job.deliver !== undefined) query.set("deliver", String(job.deliver));
  if (job.deleteAfterRun !== undefined) {
    query.set("delete_after_run", String(job.deleteAfterRun));
  }
  return request<CronJobPayload>(`${base}/api/cron/jobs/create?${query}`, token);
}

export async function deleteCronJob(
  token: string,
  jobId: string,
  base: string = "",
): Promise<{ removed: boolean; job_id: string }> {
  const query = new URLSearchParams();
  query.set("job_id", jobId);
  return request(`${base}/api/cron/jobs/delete?${query}`, token);
}

export async function toggleCronJob(
  token: string,
  jobId: string,
  enabled: boolean,
  base: string = "",
): Promise<CronJobPayload> {
  const query = new URLSearchParams();
  query.set("job_id", jobId);
  query.set("enabled", String(enabled));
  return request<CronJobPayload>(
    `${base}/api/cron/jobs/toggle?${query}`,
    token,
  );
}

export async function fetchTools(
  token: string,
  base: string = "",
): Promise<ToolsPayload> {
  return request<ToolsPayload>(`${base}/api/tools`, token);
}

export async function importToolFile(
  token: string,
  filename: string,
  content: string,
  base: string = "",
): Promise<ToolImportResult> {
  const query = new URLSearchParams();
  query.set("filename", filename);
  const bytes = new TextEncoder().encode(content);
  const CHUNK_BYTES = 4000;
  const headers: Record<string, string> = {};
  let headerIdx = 0;
  for (let i = 0; i < bytes.length; i += CHUNK_BYTES) {
    const slice = bytes.slice(i, i + CHUNK_BYTES);
    let binary = "";
    for (let j = 0; j < slice.length; j++) {
      binary += String.fromCharCode(slice[j]);
    }
    const b64 = btoa(binary);
    if (headerIdx === 0) {
      headers["x-miniunicorn-Tool-Content"] = b64;
    } else {
      headers[`x-miniunicorn-Tool-Content-${headerIdx}`] = b64;
    }
    headerIdx++;
  }
  return request<ToolImportResult>(
    `${base}/api/tools/import?${query}`,
    token,
    { headers },
  );
}

export async function deleteTool(
  token: string,
  name: string,
  base: string = "",
): Promise<{ deleted: boolean; name: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request(`${base}/api/tools/delete?${query}`, token);
}

export async function fetchChannels(
  token: string,
  base: string = "",
): Promise<ChannelsPayload> {
  return request<ChannelsPayload>(`${base}/api/channels`, token);
}

export async function updateChannelConfig(
  token: string,
  name: string,
  config: Record<string, unknown> | null,
  enabled?: boolean,
  base: string = "",
): Promise<{ ok: boolean; name: string; enabled: boolean; config: Record<string, unknown> | null }> {
  const query = new URLSearchParams();
  query.set("name", name);
  if (enabled !== undefined) {
    query.set("enabled", enabled ? "true" : "false");
  }
  // Channel config may contain secrets (api_key, tokens, etc.) — move it out
  // of the URL query string into the x-miniunicorn-Values header.
  const sensitive = sensitiveValuesHeader({
    config: config === null ? undefined : JSON.stringify(config),
  });
  return request(`${base}/api/channels/update?${query}`, token, {
    headers: sensitive ?? {},
  });
}

export async function deleteChannelConfig(
  token: string,
  name: string,
  base: string = "",
): Promise<{ ok: boolean; name: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request(`${base}/api/channels/delete?${query}`, token);
}

/** 启动某个 channel 的扫码登录流程（参考 QwenPaw QrcodeAuthBlock）。 */
export async function beginChannelQrLogin(
  token: string,
  name: string,
  domain?: string,
  base: string = "",
): Promise<ChannelQrBeginPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  if (domain) query.set("domain", domain);
  return request<ChannelQrBeginPayload>(
    `${base}/api/channels/qrcode?${query}`,
    token,
  );
}

/** 轮询扫码登录状态。成功时后端自动将凭证写入 config.json。 */
export async function pollChannelQrStatus(
  token: string,
  name: string,
  pollToken: string,
  domain?: string,
  base: string = "",
): Promise<ChannelQrStatusPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  query.set("poll_token", pollToken);
  if (domain) query.set("domain", domain);
  return request<ChannelQrStatusPayload>(
    `${base}/api/channels/qrcode/status?${query}`,
    token,
  );
}

