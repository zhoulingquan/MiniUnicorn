import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { MiniunicornClient } from "@/lib/miniunicorn-client";
import { STORAGE_KEYS } from "@/lib/storage";

const RESTART_STARTED_KEY = STORAGE_KEYS.restartStartedAt;

export interface UseRestartFlowOptions {
  client: MiniunicornClient;
  /** 触发重启时使用的 chatId(通常为 activeSession?.chatId ?? client.defaultChatId)。 */
  activeChatId: string | null;
}

/**
 * 管理设置页"重启"按钮触发的重启流程:
 * - 用户点击重启 -> 发送 /restart -> 记录开始时间到 localStorage
 * - 监听 client 连接状态:经历"断开 -> 重连"后弹出完成 toast
 *
 * localStorage 的目的是跨页面刷新保持重启时间戳(同一浏览器标签页内有效)。
 * 1500ms 的"短重启"过滤用于避免连接抖动误触发完成提示;只有真正经历断开
 * (restartSawDisconnectRef.current=true)或超过 1.5s 才认为重启完成。
 */
export function useRestartFlow({ client, activeChatId }: UseRestartFlowOptions) {
  const { t } = useTranslation();
  const restartSawDisconnectRef = useRef(false);
  const [restartToast, setRestartToast] = useState<string | null>(null);
  const [isRestarting, setIsRestarting] = useState(false);

  const onRestart = useCallback(() => {
    if (!activeChatId) return;
    restartSawDisconnectRef.current = false;
    setIsRestarting(true);
    try {
      window.localStorage.setItem(RESTART_STARTED_KEY, String(Date.now()));
    } catch {
      // ignore storage errors
    }
    client.sendMessage(activeChatId, "/restart");
  }, [activeChatId, client]);

  useEffect(() => {
    return client.onStatus((status) => {
      const startedAt = (() => {
        try {
          return Number(window.localStorage.getItem(RESTART_STARTED_KEY) ?? "0");
        } catch {
          return 0;
        }
      })();
      if (!startedAt) return;
      if (status !== "open") {
        restartSawDisconnectRef.current = true;
        return;
      }
      const elapsedMs = Date.now() - startedAt;
      if (!restartSawDisconnectRef.current && elapsedMs < 1500) return;
      try {
        window.localStorage.removeItem(RESTART_STARTED_KEY);
      } catch {
        // ignore storage errors
      }
      setIsRestarting(false);
      setRestartToast(
        t("app.restart.completed", { seconds: (elapsedMs / 1000).toFixed(1) }),
      );
      window.setTimeout(() => setRestartToast(null), 3_500);
    });
  }, [client, t]);

  return {
    isRestarting,
    restartToast,
    onRestart,
  };
}
