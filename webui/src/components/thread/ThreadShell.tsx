import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ThreadComposer } from "@/components/thread/ThreadComposer";
import { StreamErrorNotice } from "@/components/thread/StreamErrorNotice";
import { ThreadViewport } from "@/components/thread/ThreadViewport";
import { useMiniunicornStream, type SendImage, type SendOptions } from "@/hooks/useMiniunicornStream";
import { useSessionHistory } from "@/hooks/useSessions";
import { fetchAgents, listSlashCommands, rewindSession, updateSettings, fetchSkills } from "@/lib/api";
import { inferProviderFromModelName, providerDisplayLabel } from "@/lib/provider-brand";
import type {
  AgentInfo,
  ChatSummary,
  SettingsPayload,
  SkillInfo,
  SlashCommand,
  UIMessage,
  WorkspaceScopePayload,
  WorkspacesPayload,
} from "@/lib/types";
import { normalizeLegacyLongTaskMessages } from "@/lib/thread-display-compat";
import { scrubSubagentUiMessages } from "@/lib/subagent-channel-display";
import { useClient } from "@/providers/ClientProvider";

function projectWebuiThreadMessages(messages: UIMessage[]): UIMessage[] {
  return scrubSubagentUiMessages(normalizeLegacyLongTaskMessages(messages));
}

function sameMessageShape(a: UIMessage, b: UIMessage): boolean {
  return (
    a.role === b.role
    && (a.kind ?? "") === (b.kind ?? "")
    && a.content === b.content
  );
}

function isStaleThreadSnapshot(current: UIMessage[], snapshot: UIMessage[]): boolean {
  if (current.length === 0 || snapshot.length >= current.length) return false;
  if (snapshot.length === 0) return true;
  return snapshot.every((message, index) => sameMessageShape(current[index], message));
}

/** Find the N-th (0-based) non-trace user message in the message list. */
function findUserMessageByIndex(messages: UIMessage[], index: number): UIMessage | null {
  if (index < 0) return null;
  let count = 0;
  for (const m of messages) {
    if (m.role === "user" && m.kind !== "trace") {
      if (count === index) return m;
      count += 1;
    }
  }
  return null;
}

/** Truncate the message list at (and including) the N-th non-trace user message. */
function truncateAtUserMessage(messages: UIMessage[], userMessageIndex: number): UIMessage[] {
  if (userMessageIndex < 0) return messages;
  let count = 0;
  for (let i = 0; i < messages.length; i += 1) {
    const m = messages[i];
    if (m.role === "user" && m.kind !== "trace") {
      if (count === userMessageIndex) return messages.slice(0, i);
      count += 1;
    }
  }
  return messages;
}

interface ThreadShellProps {
  session: ChatSummary | null;
  onNewChat?: () => void;
  onCreateChat?: (workspaceScope?: WorkspaceScopePayload | null) => Promise<string | null>;
  onTurnEnd?: () => void;
  workspaceScope?: WorkspaceScopePayload | null;
  workspaceDefaultScope?: WorkspaceScopePayload | null;
  workspaceControls?: WorkspacesPayload["controls"] | null;
  workspaceScopeDisabled?: boolean;
  workspaceError?: string | null;
  onWorkspaceScopeChange?: (scope: WorkspaceScopePayload) => void;
  settingsSnapshot?: SettingsPayload | null;
  /** settings 变化时上报(如 composer 切换 model preset 后),由 App.tsx 统一管理。 */
  onSettingsChange?: (payload: SettingsPayload) => void;
  /** 当前激活的 provider(由 App.tsx 从 settings + modelName 派生)。 */
  currentProvider?: string | null;
  /** Currently selected subagent id (routes outbound turns to that agent). */
  selectedAgentId?: string | null;
  /** Called when the user picks a subagent in the composer. */
  onSelectAgent?: (agentId: string) => void;
  /** Called when the user clears the active subagent selection. */
  onClearAgent?: () => void;
}

function toModelBadgeLabel(modelName: string | null): string | null {
  if (!modelName) return null;
  const trimmed = modelName.trim();
  if (!trimmed) return null;
  const leaf = trimmed.split("/").pop() ?? trimmed;
  return leaf || trimmed;
}

interface ModelBadgeInfo {
  label: string | null;
  provider: string | null;
  providerLabel: string | null;
  apiBase: string | null;
}

export function activeModelPreset(settings: SettingsPayload | null): SettingsPayload["model_presets"][number] | null {
  if (!settings) return null;
  const configured = settings.agent.model_preset || "default";
  return (
    settings.model_presets.find((preset) => preset.name === configured)
    ?? settings.model_presets.find((preset) => preset.active)
    ?? null
  );
}

export function resolvedModelProvider(settings: SettingsPayload | null, modelName: string | null): string | null {
  const preset = activeModelPreset(settings);
  const rawProvider = preset?.provider || settings?.agent.provider || null;
  if (rawProvider === "auto") {
    return settings?.agent.resolved_provider || inferProviderFromModelName(modelName) || null;
  }
  // custom 命名 preset:返回虚拟 row name(custom__<preset_name>),
  // 让 header 下拉和设置页匹配到对应的独立虚拟卡片
  if (rawProvider === "custom" && preset && !preset.is_default) {
    return `custom__${preset.name}`;
  }
  return rawProvider || inferProviderFromModelName(modelName);
}

function toModelBadgeInfo(modelName: string | null, settings: SettingsPayload | null): ModelBadgeInfo {
  const label = toModelBadgeLabel(modelName || settings?.agent.model || null);
  const provider = resolvedModelProvider(settings, modelName || settings?.agent.model || null);
  // 从 providers 找到匹配 row 的 api_base(用于 custom 动态 brand 图标生成)
  const row = provider ? settings?.providers?.find((p) => p.name === provider) : null;
  return {
    label,
    provider,
    providerLabel: provider ? providerDisplayLabel(settings?.providers ?? [], provider) : null,
    apiBase: row?.api_base ?? null,
  };
}

const HERO_GREETING_KEYS = [
  "thread.empty.greetings.workOn",
  "thread.empty.greetings.start",
  "thread.empty.greetings.build",
  "thread.empty.greetings.tackle",
] as const;

function randomHeroGreetingKey(): (typeof HERO_GREETING_KEYS)[number] {
  const index = Math.floor(Math.random() * HERO_GREETING_KEYS.length);
  return HERO_GREETING_KEYS[index] ?? HERO_GREETING_KEYS[0];
}

interface PendingFirstMessage {
  content: string;
  images?: SendImage[];
  options?: SendOptions;
}

export function ThreadShell({
  session,
  onCreateChat,
  onTurnEnd,
  workspaceScope = null,
  workspaceDefaultScope = null,
  workspaceControls = null,
  workspaceScopeDisabled = false,
  workspaceError = null,
  onWorkspaceScopeChange,
  settingsSnapshot = null,
  onSettingsChange,
  currentProvider: currentProviderProp,
  selectedAgentId = null,
  onSelectAgent,
  onClearAgent,
}: ThreadShellProps) {
  const { t } = useTranslation();
  const chatId = session?.chatId ?? null;
  const historyKey = session?.key ?? null;
  const {
    messages: historical,
    loading,
    hasPendingToolCalls,
    refresh: refreshHistory,
    version: historyVersion,
  } = useSessionHistory(historyKey);
  const { client, modelName, token } = useClient();
  const [booting, setBooting] = useState(false);
  const [slashCommands, setSlashCommands] = useState<SlashCommand[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  // settings 由 App.tsx 统一管理(通过 settingsSnapshot prop 传入),
  // ThreadShell 不再持有本地 settings 状态,变更通过 onSettingsChange 上报。
  const settings = settingsSnapshot;
  const [heroGreetingKey, setHeroGreetingKey] = useState(randomHeroGreetingKey);
  const [scrollToBottomSignal, setScrollToBottomSignal] = useState(0);
  // 回退后,被回退的用户消息内容会写入此处,由 ThreadComposer 消费后清空。
  const [prefillText, setPrefillText] = useState<string | null>(null);
  const clearPrefillText = useCallback(() => setPrefillText(null), []);
  const pendingFirstRef = useRef<PendingFirstMessage | null>(null);

  // ---------------------------------------------------------------------------
  // Thread message cache — 6 refs that cooperate to keep per-chat in-memory
  // thread state alive across session switches.
  //
  // DATA FOW
  //   messageCacheRef  ─►  `initial` (useMemo)  ─►  useMiniunicornStream
  //        ▲                                              │
  //        │                                              ▼
  //   sync effects ◄── setMessages / messages ────────────┘
  //
  // Because `useMiniunicornStream` consumes `initial` (which reads
  // `messageCacheRef`) **and** produces `setMessages` / `messages` that the
  // cache-sync effects write back, these refs cannot be cleanly extracted into
  // a standalone hook without creating a circular dependency. They are kept
  // inline and documented below.
  //
  // REF INVENTORY
  //   messageCacheRef            chatId → projected UIMessage[] (live thread)
  //   prevChatIdForCacheRef      last chatId rendered; drives cache-on-switch
  //   skipLayoutCacheRef         suppress one cache write after a chat switch
  //                              (the first paint still sees the old chat's
  //                              messages from useMiniunicornStream's reset)
  //   appliedHistoryVersionRef   chatId → last historyVersion merged into the
  //                              live thread (prevents re-applying stale snaps)
  //   pendingCanonicalHydrateRef chatIds awaiting a fresh canonical replay
  //                              (set on `session_updated`, cleared once the
  //                              new history has been merged)
  //   sessionKeyByChatIdRef      chatId → sessionKey mapping for telemetry
  // ---------------------------------------------------------------------------
  const messageCacheRef = useRef<Map<string, UIMessage[]>>(new Map());
  const prevChatIdForCacheRef = useRef<string | null>(null);
  const skipLayoutCacheRef = useRef(false);
  const appliedHistoryVersionRef = useRef<Map<string, number>>(new Map());
  const pendingCanonicalHydrateRef = useRef<Set<string>>(new Set());
  const sessionKeyByChatIdRef = useRef<Map<string, string>>(new Map());

  const initial = useMemo(() => {
    if (!chatId) return historical;
    return messageCacheRef.current.get(chatId) ?? historical;
  }, [chatId, historical]);
  const handleTurnEnd = useCallback(() => {
    onTurnEnd?.();
  }, [onTurnEnd]);
  const {
    messages,
    isStreaming,
    runStartedAt,
    goalState,
    contextUsage,
    send,
    stop,
    setMessages,
    streamError,
    dismissStreamError,
  } = useMiniunicornStream(chatId, initial, hasPendingToolCalls, handleTurnEnd);

  useEffect(() => {
    if (chatId && historyKey) sessionKeyByChatIdRef.current.set(chatId, historyKey);
  }, [chatId, historyKey]);

  const displayMessages = useMemo(() => projectWebuiThreadMessages(messages), [messages]);

  // 上下文消息条数:仅统计 user/assistant 对话回合,不计入 trace(工具提示)行。
  const conversationMessageCount = useMemo(
    () => displayMessages.filter((m) => m.kind !== "trace").length,
    [displayMessages],
  );
  const contextWindowTokens =
    settings?.agent.resolved_context_window_tokens ??
    settings?.agent.context_window_tokens ??
    null;

  const showHeroComposer = messages.length === 0 && !loading;
  const wasShowingHeroComposerRef = useRef(showHeroComposer);
  const modelBadge = useMemo(
    () => toModelBadgeInfo(modelName, settings),
    [modelName, settings],
  );

  useEffect(() => {
    if (showHeroComposer && !wasShowingHeroComposerRef.current) {
      setHeroGreetingKey(randomHeroGreetingKey());
    }
    wasShowingHeroComposerRef.current = showHeroComposer;
  }, [showHeroComposer]);

  const withWorkspaceScope = useCallback(
    (options?: SendOptions): SendOptions | undefined => {
      if (!workspaceScope) return options;
      return {
        ...(options ?? {}),
        workspaceScope,
      };
    },
    [workspaceScope],
  );

  // settings 由 App.tsx 统一管理(含 runtime model update 监听),ThreadShell 直接消费 props。
  // currentProvider 同样由 App.tsx 派生,通过 currentProviderProp 传入。
  const currentProvider = currentProviderProp ?? null;

  // 已配置的模型列表:
  // - 虚拟 row(如 custom__xxx):单 preset,只显示该 preset 的 model(徽章静态)
  // - 常规 provider:显示该 provider 下所有命名 preset 的 model,选中切换到对应 preset
  const availableModels = useMemo(() => {
    if (!settings || !currentProvider) return [];
    // 虚拟 preset row:只显示该 preset 的 model
    if (currentProvider.includes("__")) {
      const row = settings.providers.find((p) => p.name === currentProvider);
      const presetName = row?.preset_name;
      if (!presetName) return [];
      const preset = settings.model_presets.find((p) => p.name === presetName);
      return preset ? [preset.model] : [];
    }
    // 常规 provider:显示该 provider 下所有命名 preset(非 default)的 model
    const seen = new Set<string>();
    const result: string[] = [];
    for (const preset of settings.model_presets) {
      if (preset.is_default) continue;
      if (preset.provider !== currentProvider) continue;
      const model = preset.model?.trim();
      if (!model || seen.has(model)) continue;
      seen.add(model);
      result.push(model);
    }
    return result;
  }, [settings, currentProvider]);

  // 用户在 composer 模型徽章弹出菜单选择其他模型。
  // 列表显示的是当前 provider 下已配置的命名 preset,选中即切换到对应 preset
  // (preset 自带 provider/model/凭证),通过 updateSettings({ modelPreset }) 切换。
  // settings 变更通过 onSettingsChange 上报给 App.tsx(统一管理 settings 状态)。
  const handleSelectModel = useCallback(
    async (model: string) => {
      if (!token || !model || model === modelBadge.label) return;
      const target = settings?.model_presets.find(
        (p) => !p.is_default && p.provider === currentProvider && p.model === model,
      );
      if (!target) return;
      try {
        const next = await updateSettings(token, { modelPreset: target.name });
        onSettingsChange?.(next);
      } catch (err) {
        console.error("[ThreadShell] switch model preset failed", err);
      }
    },
    [token, modelBadge.label, settings, currentProvider, onSettingsChange],
  );

  // Canonical history hydration — merges fetched history into the live thread.
  // Reads: messageCacheRef, appliedHistoryVersionRef, pendingCanonicalHydrateRef.
  // Writes: messageCacheRef, appliedHistoryVersionRef, pendingCanonicalHydrateRef.
  useEffect(() => {
    if (!chatId || loading) return;
    const cached = messageCacheRef.current.get(chatId);
    const appliedVersion = appliedHistoryVersionRef.current.get(chatId) ?? 0;
    const hasPendingCanonicalHydrate = pendingCanonicalHydrateRef.current.has(chatId);
    const hasNewCanonicalHistory = hasPendingCanonicalHydrate && historyVersion > appliedVersion;
    // When the user switches away and back, keep the local in-memory thread
    // state (including not-yet-persisted messages) instead of replacing it with
    // whatever the history endpoint currently knows about. Once a fresh
    // canonical replay arrives (e.g. after ``session_updated`` refresh), prefer it
    // so rendering converges to the same shape as a manual refresh.
    setMessages((prev) => {
      const normalizedHistory = projectWebuiThreadMessages(historical);
      const keepLiveMessages = (messagesToKeep: UIMessage[]) => {
        const projected = projectWebuiThreadMessages(messagesToKeep);
        messageCacheRef.current.set(chatId, projected);
        return projected;
      };
      if (hasNewCanonicalHistory && historical.length > 0) {
        if (isStaleThreadSnapshot(prev, normalizedHistory)) return keepLiveMessages(prev);
        pendingCanonicalHydrateRef.current.delete(chatId);
        appliedHistoryVersionRef.current.set(chatId, historyVersion);
        messageCacheRef.current.set(chatId, normalizedHistory);
        return normalizedHistory;
      }
      if (cached && cached.length > 0) {
        const normalizedCached = projectWebuiThreadMessages(cached);
        if (isStaleThreadSnapshot(prev, normalizedCached)) return keepLiveMessages(prev);
        return normalizedCached;
      }
      if (isStaleThreadSnapshot(prev, normalizedHistory)) return keepLiveMessages(prev);
      appliedHistoryVersionRef.current.set(chatId, historyVersion);
      if (normalizedHistory.length > 0) messageCacheRef.current.set(chatId, normalizedHistory);
      return normalizedHistory;
    });
  }, [loading, chatId, historical, historyVersion, setMessages]);

  // Marks a chat as pending canonical hydration when the backend signals a
  // session update, then triggers a history refresh.
  // Writes: pendingCanonicalHydrateRef.
  useEffect(() => {
    if (!chatId) return;
    return client.onSessionUpdate((updatedChatId, scope) => {
      if (updatedChatId !== chatId) return;
      if (scope === "metadata") return;
      pendingCanonicalHydrateRef.current.add(chatId);
      refreshHistory();
    });
  }, [chatId, client, refreshHistory]);

  useEffect(() => {
    if (!chatId || loading) return;
    setScrollToBottomSignal((value) => value + 1);
  }, [chatId, loading, historical]);

  useEffect(() => {
    if (chatId) return;
    setMessages(projectWebuiThreadMessages(historical));
  }, [chatId, historical, setMessages]);

  // Cache-on-switch — runs synchronously before paint to snapshot the outgoing
  // chat's messages into messageCacheRef and arm skipLayoutCacheRef so the
  // post-paint persist effect doesn't overwrite it with stale data.
  // Reads: prevChatIdForCacheRef. Writes: messageCacheRef, skipLayoutCacheRef, prevChatIdForCacheRef.
  useLayoutEffect(() => {
    if (chatId) {
      const prev = prevChatIdForCacheRef.current;
      if (prev && prev !== chatId) {
        messageCacheRef.current.set(prev, projectWebuiThreadMessages(messages));
        skipLayoutCacheRef.current = true;
      }
      prevChatIdForCacheRef.current = chatId;
    } else {
      if (prevChatIdForCacheRef.current) {
        messageCacheRef.current.set(
          prevChatIdForCacheRef.current,
          projectWebuiThreadMessages(messages),
        );
        skipLayoutCacheRef.current = true;
      }
      prevChatIdForCacheRef.current = null;
    }
  }, [chatId, messages]);

  // Persist thread to in-memory cache after paint so ``useMiniunicornStream``'s chat switch
  // ``useEffect`` reset has flushed; ``skipLayoutCacheRef`` drops the first run that still
  // sees the *previous* chat's ``messages`` (avoids stale rows leaking across sessions).
  useEffect(() => {
    if (!chatId) {
      return;
    }
    if (skipLayoutCacheRef.current) {
      skipLayoutCacheRef.current = false;
      return;
    }
    if (loading) {
      return;
    }
    messageCacheRef.current.set(chatId, projectWebuiThreadMessages(messages));
  }, [chatId, loading, messages]);

  useEffect(() => {
    if (!chatId) return;
    const pending = pendingFirstRef.current;
    if (!pending) return;
    pendingFirstRef.current = null;
    setScrollToBottomSignal((value) => value + 1);
    send(pending.content, pending.images, pending.options);
    setBooting(false);
  }, [chatId, send]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const commands = await listSlashCommands(token);
        if (!cancelled) setSlashCommands(commands);
      } catch {
        if (!cancelled) setSlashCommands([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  // Fetch available subagents for the composer selector. Refresh on token change.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await fetchAgents(token);
        if (!cancelled) setAgents(payload.agents);
      } catch {
        if (!cancelled) setAgents([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  // Fetch available skills for the composer selector. Refresh on token change.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await fetchSkills(token);
        if (!cancelled) setSkills(payload.skills);
      } catch {
        if (!cancelled) setSkills([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleWelcomeSend = useCallback(
    async (content: string, images?: SendImage[], options?: SendOptions) => {
      if (booting) return;
      setBooting(true);
      pendingFirstRef.current = { content, images, options: withWorkspaceScope(options) };
      const newId = await onCreateChat?.(workspaceScope);
      if (!newId) {
        pendingFirstRef.current = null;
        setBooting(false);
      }
    },
    [booting, onCreateChat, withWorkspaceScope, workspaceScope],
  );

  const handleThreadSend = useCallback(
    (content: string, images?: SendImage[], options?: SendOptions) => {
      setScrollToBottomSignal((value) => value + 1);
      send(content, images, withWorkspaceScope(options));
    },
    [send, withWorkspaceScope],
  );

  // Rewind: truncate both the WebUI transcript and the agent session file to
  // before the N-th user message, then refresh the thread view. The backend
  // broadcasts ``session_updated`` after the truncation, which triggers
  // ``refreshHistory`` via ``client.onSessionUpdate``. We also optimistically
  // truncate the local ``messages`` state for immediate UI feedback and clear
  // the in-memory cache so the canonical refresh wins.
  const handleRewind = useCallback(
    async (userMessageIndex: number) => {
      if (!token || !historyKey || !chatId) return;
      if (isStreaming) return;
      // 先捕获被回退的用户消息内容,以便填入输入框供用户编辑重发。
      const targetUserMessage = findUserMessageByIndex(messages, userMessageIndex);
      const rewindContent = targetUserMessage?.content ?? "";
      try {
        await rewindSession(token, historyKey, userMessageIndex);
        pendingCanonicalHydrateRef.current.add(chatId);
        messageCacheRef.current.delete(chatId);
        setMessages((prev) => truncateAtUserMessage(prev, userMessageIndex));
        setScrollToBottomSignal((value) => value + 1);
        // 把被回退的用户消息内容回填到输入框,方便用户编辑后重新发送。
        if (rewindContent.trim().length > 0) {
          setPrefillText(rewindContent);
        }
      } catch (err) {
        console.error("[ThreadShell] rewind failed", err);
      }
    },
    [token, historyKey, chatId, isStreaming, messages, setMessages],
  );

  // Retry: rewind to before the user turn that produced this assistant reply,
  // then re-send the original user content. Images and per-turn options are
  // not preserved (typical retry use case is re-generating a text answer).
  // Optimistic ``send`` bubble survives the canonical hydrate because the
  // stale-snapshot check treats the server's shorter truncated history as a
  // prefix of the optimistic state.
  const handleRetry = useCallback(
    async (userMessageIndex: number) => {
      if (!token || !historyKey || !chatId) return;
      if (isStreaming) return;
      const targetUserMessage = findUserMessageByIndex(messages, userMessageIndex);
      if (!targetUserMessage) return;
      const retryContent = targetUserMessage.content;
      if (!retryContent.trim()) return;
      try {
        await rewindSession(token, historyKey, userMessageIndex);
        // Do NOT mark as pending canonical hydrate here — the optimistic
        // ``send`` bubble we are about to create should win over the rewind's
        // truncated canonical state. The subsequent turn's own
        // ``session_updated`` broadcast will reconcile.
        messageCacheRef.current.delete(chatId);
        setMessages((prev) => truncateAtUserMessage(prev, userMessageIndex));
        setScrollToBottomSignal((value) => value + 1);
        send(retryContent);
      } catch (err) {
        console.error("[ThreadShell] retry failed", err);
      }
    },
    [token, historyKey, chatId, isStreaming, messages, send, setMessages],
  );

  const composer = (
    <>
      {streamError ? (
        <StreamErrorNotice
          error={streamError}
          onDismiss={dismissStreamError}
        />
      ) : null}
      {session ? (
        <ThreadComposer
          onSend={handleThreadSend}
          disabled={!chatId}
          isStreaming={isStreaming}
          placeholder={
            showHeroComposer
              ? t("thread.composer.placeholderHero")
              : t("thread.composer.placeholderThread")
          }
          modelLabel={modelBadge.label}
          modelProvider={modelBadge.provider}
          modelProviderLabel={modelBadge.providerLabel}
          modelApiBase={modelBadge.apiBase}
          models={availableModels}
          onSelectModel={handleSelectModel}
          variant={showHeroComposer ? "hero" : "thread"}
          slashCommands={slashCommands}
          onStop={stop}
          runStartedAt={runStartedAt}
          goalState={goalState}
          workspaceScope={workspaceScope}
          workspaceDefaultScope={workspaceDefaultScope}
          workspaceControls={workspaceControls}
          workspaceScopeDisabled={workspaceScopeDisabled}
          workspaceError={workspaceError}
          onWorkspaceScopeChange={onWorkspaceScopeChange}
          agents={agents}
          selectedAgentId={selectedAgentId}
          onSelectAgent={onSelectAgent}
          onClearAgent={onClearAgent}
          skills={skills}
          messageCount={conversationMessageCount}
          contextWindowTokens={contextWindowTokens}
          contextUsage={contextUsage}
          conversationKey={historyKey}
          prefillText={prefillText}
          onPrefillConsumed={clearPrefillText}
        />
      ) : (
        <ThreadComposer
          onSend={handleWelcomeSend}
          disabled={booting}
          isStreaming={isStreaming}
          placeholder={
            booting
              ? t("thread.composer.placeholderOpening")
              : t("thread.composer.placeholderHero")
          }
          modelLabel={modelBadge.label}
          modelProvider={modelBadge.provider}
          modelProviderLabel={modelBadge.providerLabel}
          modelApiBase={modelBadge.apiBase}
          models={availableModels}
          onSelectModel={handleSelectModel}
          variant="hero"
          slashCommands={slashCommands}
          runStartedAt={runStartedAt}
          goalState={goalState}
          workspaceScope={workspaceScope}
          workspaceDefaultScope={workspaceDefaultScope}
          workspaceControls={workspaceControls}
          workspaceScopeDisabled={workspaceScopeDisabled}
          workspaceError={workspaceError}
          onWorkspaceScopeChange={onWorkspaceScopeChange}
          agents={agents}
          selectedAgentId={selectedAgentId}
          onSelectAgent={onSelectAgent}
          onClearAgent={onClearAgent}
          skills={skills}
          messageCount={conversationMessageCount}
          contextWindowTokens={contextWindowTokens}
          contextUsage={contextUsage}
        />
      )}
    </>
  );

  const emptyState = loading ? (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      {t("thread.loadingConversation")}
    </div>
  ) : (
    <div className="flex w-full flex-col items-center text-center animate-in fade-in-0 slide-in-from-bottom-2 duration-500">
      <img
        src="/logo.svg"
        alt={t("app.brand")}
        className="mb-6 h-40 w-40 rounded-2xl"
      />
      <h1 className="text-balance text-[40px] font-normal leading-tight tracking-[-0.045em] text-foreground sm:text-[48px]">
        {t(heroGreetingKey)}
      </h1>
    </div>
  );

  return (
    <section className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
      {/* ThreadHeader 已移除:logo + PanelLeft + provider 下拉 + 语言/主题
       * 统一移至 App.tsx 的全局 TopBar,跨整个窗口宽度固定显示。 */}
      <ThreadViewport
        messages={displayMessages}
        isStreaming={isStreaming}
        emptyState={emptyState}
        composer={composer}
        scrollToBottomSignal={scrollToBottomSignal}
        conversationKey={historyKey}
        showScrollToBottomButton={!!session}
        onRewind={session ? handleRewind : undefined}
        onRetry={session ? handleRetry : undefined}
      />
    </section>
  );
}
