import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ErzaClient } from "@/lib/erza-client";
import { STORAGE_KEYS } from "@/lib/storage";
import type { ChatSummary } from "@/lib/types";
import { debounce } from "@/lib/utils";

const COMPLETED_RUNS_STORAGE_KEY = STORAGE_KEYS.sidebarCompletedRuns;

function readCompletedRunChatIds(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(COMPLETED_RUNS_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return new Set();
    return new Set(
      parsed.filter((item): item is string => typeof item === "string"),
    );
  } catch {
    return new Set();
  }
}

function writeCompletedRunChatIds(chatIds: Set<string>): void {
  try {
    window.localStorage.setItem(
      COMPLETED_RUNS_STORAGE_KEY,
      JSON.stringify(Array.from(chatIds)),
    );
  } catch {
    // ignore storage errors (private mode, etc.)
  }
}

export interface UseChatRunStatusOptions {
  client: ErzaClient;
  sessions: ChatSummary[];
  loading: boolean;
  /** 当前活跃会话的 chatId;传入后用于派生 activeChatRunning。 */
  activeChatId?: string | null;
}

/**
 * 管理侧边栏"运行中 / 刚完成"chat 集合的状态。
 *
 * 拆分原因:
 * - 这两组 Set 与 Shell 其它关注点(view 切换、对话框等)无直接耦合,
 *   但涉及 4 个 effect(初始 attach、清理已删除会话、订阅 onRunStatus、
 *   节流写 localStorage),原 Shell 中相关代码分散在多个区块。
 *
 * 保留行为:
 * - 首次加载 sessions 后,对带 runStartedAt 的会话自动 attach + 加入 runningChatIds
 * - onRunStatus(chatId, startedAt!=null) -> 加入 runningChatIds,从 completedChatIds 移除
 * - onRunStatus(chatId, null) -> 从 runningChatIds 移除,加入 completedChatIds
 * - completedChatIds 写 localStorage 时做 500ms 节流,卸载时 flush 避免丢失
 * - sessions 变化时清理已删除 chatId
 *
 * 使用 ref(runningChatIdsRef)保证 onRunStatus 回调能拿到最新集合,避免回调
 * 闭包陈旧;原实现亦如此,此处保持一致。
 */
export function useChatRunStatus({
  client,
  sessions,
  loading,
  activeChatId = null,
}: UseChatRunStatusOptions) {
  const [runningChatIds, setRunningChatIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [completedChatIds, setCompletedChatIds] =
    useState<Set<string>>(readCompletedRunChatIds);
  const runningChatIdsRef = useRef<Set<string>>(new Set());

  // 节流写入 completedChatIds 到 localStorage,避免多会话并发场景下频繁 JSON.stringify + 写入
  const writeCompletedDebounced = useMemo(
    () => debounce((ids: Set<string>) => writeCompletedRunChatIds(ids), 500),
    [],
  );
  useEffect(() => {
    writeCompletedDebounced(completedChatIds);
  }, [completedChatIds, writeCompletedDebounced]);
  // 组件卸载时立即刷新挂起的写入,避免丢失最后一次更新
  useEffect(() => {
    return () => {
      writeCompletedDebounced.flush();
    };
  }, [writeCompletedDebounced]);

  // sessions 变化时清理已删除 chatId
  useEffect(() => {
    if (loading) return;
    const knownChatIds = new Set(sessions.map((session) => session.chatId));
    setCompletedChatIds((current) => {
      const next = new Set(
        Array.from(current).filter((chatId) => knownChatIds.has(chatId)),
      );
      return next.size === current.size ? current : next;
    });
  }, [loading, sessions]);

  // 首次加载 sessions 后,对运行中的会话自动 attach 并补全 runningChatIds
  useEffect(() => {
    if (loading) return;
    const activeRunIds = sessions
      .filter((session) => typeof session.runStartedAt === "number")
      .map((session) => session.chatId);
    if (activeRunIds.length === 0) return;

    for (const chatId of activeRunIds) {
      client.attach(chatId);
    }
    setRunningChatIds((current) => {
      let changed = false;
      const next = new Set(current);
      for (const chatId of activeRunIds) {
        if (!next.has(chatId)) changed = true;
        next.add(chatId);
      }
      if (!changed) return current;
      runningChatIdsRef.current = next;
      return next;
    });
    setCompletedChatIds((current) => {
      let changed = false;
      const next = new Set(current);
      for (const chatId of activeRunIds) {
        if (next.delete(chatId)) changed = true;
      }
      return changed ? next : current;
    });
  }, [client, loading, sessions]);

  // 订阅运行状态变更:started -> running;stopped -> completed
  useEffect(() => {
    return client.onRunStatus((chatId, startedAt) => {
      if (startedAt != null) {
        const nextRunning = new Set(runningChatIdsRef.current);
        nextRunning.add(chatId);
        runningChatIdsRef.current = nextRunning;
        setRunningChatIds(nextRunning);
        setCompletedChatIds((current) => {
          if (!current.has(chatId)) return current;
          const next = new Set(current);
          next.delete(chatId);
          return next;
        });
        return;
      }

      if (!runningChatIdsRef.current.has(chatId)) return;
      const nextRunning = new Set(runningChatIdsRef.current);
      nextRunning.delete(chatId);
      runningChatIdsRef.current = nextRunning;
      setRunningChatIds(nextRunning);
      setCompletedChatIds((current) => {
        const next = new Set(current);
        next.add(chatId);
        return next;
      });
    });
  }, [client]);

  const runningChatIdList = useMemo(
    () => Array.from(runningChatIds),
    [runningChatIds],
  );
  const completedChatIdList = useMemo(
    () => Array.from(completedChatIds),
    [completedChatIds],
  );

  const activeChatRunning = activeChatId
    ? runningChatIds.has(activeChatId)
    : false;

  // 用户点击会话时清除其"已完成"标记(由 Shell 中 onSelectChat 调用)
  const clearCompleted = useCallback((chatId: string) => {
    setCompletedChatIds((current) => {
      if (!current.has(chatId)) return current;
      const next = new Set(current);
      next.delete(chatId);
      return next;
    });
  }, []);

  return {
    runningChatIds,
    completedChatIds,
    runningChatIdList,
    completedChatIdList,
    activeChatRunning,
    clearCompleted,
  };
}
