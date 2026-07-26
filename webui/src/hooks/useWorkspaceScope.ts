import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchWorkspaces } from "@/lib/api";
import type { MiniunicornClient } from "@/lib/miniunicorn-client";
import type {
  ChatSummary,
  WorkspaceScopePayload,
  WorkspacesPayload,
} from "@/lib/types";
import { projectNameFromPath } from "@/lib/workspace";

/**
 * 把服务端返回的 WorkspaceScopePayload 规整成 UI 内部统一形态:
 * - access_mode 只允许 "restricted" / "full"(其它视为 full)
 * - restrict_to_workspace 与 access_mode 保持一致
 * - project_name 缺失时从 project_path 派生
 *
 * 同时被 Shell 的 onCreateChat / onNewChatInProject / onSelectChat 调用,
 * 因此从本文件 export。
 */
export function normalizeWorkspaceScope(
  scope: WorkspaceScopePayload,
): WorkspaceScopePayload {
  const accessMode = scope.access_mode === "restricted" ? "restricted" : "full";
  return {
    ...scope,
    project_name: scope.project_name ?? projectNameFromPath(scope.project_path),
    access_mode: accessMode,
    restrict_to_workspace: accessMode === "restricted",
  };
}

export interface UseWorkspaceScopeOptions {
  token: string;
  client: MiniunicornClient;
  sessions: ChatSummary[];
  loading: boolean;
  activeSession: ChatSummary | null;
  activeChatId: string | null;
  /** 当前活跃会话是否正在运行;运行时禁止改 scope。 */
  activeChatRunning: boolean;
}

/**
 * 管理 workspace scope 相关状态:
 * - workspaces: 服务端 workspaces payload(default_scope + controls)
 * - workspaceError: 设置 scope 被服务端拒绝时的错误文案
 * - draftWorkspaceScope: 未选中 chat 时的"草稿" scope(创建新 chat 时使用)
 * - workspaceOverrides: per-chat 的 scope 覆盖(由 onSessionUpdate 同步)
 *
 * 拆分原因:
 * - 涉及 4 个 effect(refreshWorkspaces、清理已删除 chatId 的 override、
 *   onSessionUpdate 同步、onError 拒绝提示)与 1 个 applyWorkspaceScope 回调
 * - 与 Shell 中 view 切换 / 对话框等关注点无直接耦合
 *
 * 注意:applyWorkspaceScope 依赖 activeChatRunning,该值由 useChatRunStatus 提供,
 * 因此 Shell 必须先调用 useChatRunStatus 再调用本 hook。
 */
export function useWorkspaceScope({
  token,
  client,
  sessions,
  loading,
  activeSession,
  activeChatId,
  activeChatRunning,
}: UseWorkspaceScopeOptions) {
  const { t } = useTranslation();
  const [workspaces, setWorkspaces] = useState<WorkspacesPayload | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [draftWorkspaceScope, setDraftWorkspaceScope] =
    useState<WorkspaceScopePayload | null>(null);
  const [workspaceOverrides, setWorkspaceOverrides] =
    useState<Record<string, WorkspaceScopePayload>>({});

  const refreshWorkspaces = useCallback(async () => {
    try {
      const payload = await fetchWorkspaces(token);
      setWorkspaces(payload);
    } catch {
      setWorkspaces(null);
    }
  }, [token]);

  useEffect(() => {
    void refreshWorkspaces();
  }, [refreshWorkspaces]);

  // sessions 变化时清理已删除 chatId 对应的 override
  useEffect(() => {
    if (loading) return;
    const knownChatIds = new Set(sessions.map((session) => session.chatId));
    setWorkspaceOverrides((current) => {
      const entries = Object.entries(current).filter(([chatId]) =>
        knownChatIds.has(chatId),
      );
      return entries.length === Object.keys(current).length
        ? current
        : Object.fromEntries(entries);
    });
  }, [loading, sessions]);

  // 服务端推送 session 更新时同步 workspaceScope
  useEffect(() => {
    return client.onSessionUpdate((_chatId, _scope, workspaceScope) => {
      if (!workspaceScope) return;
      const next = normalizeWorkspaceScope(workspaceScope);
      setWorkspaceOverrides((current) => ({
        ...current,
        [_chatId]: next,
      }));
      setDraftWorkspaceScope(next);
      setWorkspaceError(null);
      void refreshWorkspaces();
    });
  }, [client, refreshWorkspaces]);

  // 服务端推送 workspace_scope_rejected 错误时显示提示
  useEffect(() => {
    return client.onError((error) => {
      if (error.kind !== "workspace_scope_rejected") return;
      setWorkspaceError(t("errors.workspaceScopeRejected.body"));
      void refreshWorkspaces();
    });
  }, [client, refreshWorkspaces, t]);

  const activeWorkspaceScope = useMemo<WorkspaceScopePayload | null>(() => {
    if (activeChatId && workspaceOverrides[activeChatId]) {
      return workspaceOverrides[activeChatId];
    }
    if (activeSession?.workspaceScope) {
      return activeSession.workspaceScope;
    }
    return draftWorkspaceScope ?? workspaces?.default_scope ?? null;
  }, [
    activeChatId,
    activeSession?.workspaceScope,
    draftWorkspaceScope,
    workspaceOverrides,
    workspaces?.default_scope,
  ]);

  const applyWorkspaceScope = useCallback(
    (scope: WorkspaceScopePayload) => {
      const next = normalizeWorkspaceScope(scope);
      setWorkspaceError(null);
      if (activeChatId) {
        if (!activeChatRunning) {
          client.setWorkspaceScope(activeChatId, next);
        }
        return;
      }
      setDraftWorkspaceScope(next);
    },
    [activeChatId, activeChatRunning, client],
  );

  return {
    workspaces,
    workspaceError,
    draftWorkspaceScope,
    workspaceOverrides,
    activeWorkspaceScope,
    refreshWorkspaces,
    applyWorkspaceScope,
    setDraftWorkspaceScope,
    setWorkspaceError,
    setWorkspaceOverrides,
  };
}
