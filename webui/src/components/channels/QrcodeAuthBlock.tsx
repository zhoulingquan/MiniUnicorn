import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  QrCode,
  RefreshCw,
  Smartphone,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  beginChannelQrLogin,
  pollChannelQrStatus,
} from "@/lib/api";
import type {
  ChannelQrBeginPayload,
  ChannelQrStatusPayload,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface QrcodeAuthBlockProps {
  /** 频道名称（如 feishu / weixin / wecom / dingtalk / qq）。 */
  channelName: string;
  /** WebUI API token。 */
  token: string;
  /** 可选域名提示（feishu/lark），默认 feishu。仅 feishu handler 使用。 */
  domain?: string;
  /** 扫码登录成功后的回调（参数为后端返回的最新 channel config dict）。 */
  onSuccess: (config: Record<string, unknown>) => void;
}

type Phase =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "awaiting-scan"; begin: ChannelQrBeginPayload }
  | { kind: "polling"; begin: ChannelQrBeginPayload }
  | { kind: "succeeded"; config: Record<string, unknown> | null }
  | { kind: "failed"; error: string };

/**
 * QR 扫码登录块（参考 QwenPaw QrcodeAuthBlock）。
 *
 * 流程：
 * 1. 用户点击"开始扫码登录" → 调用 ``beginChannelQrLogin`` 获取
 *    ``qrcode_image``（base64 PNG）+ ``poll_token``
 * 2. 前端直接用 ``<img>`` 显示后端生成的二维码（不再用 qrcode.react 自己渲染）
 * 3. 自动轮询 ``pollChannelQrStatus``（间隔由后端返回，默认 5s）
 * 4. 状态变为 ``succeeded`` 时调用 ``onSuccess``，关闭轮询；
 *    变为 ``failed`` 时显示重试按钮；
 *    变为 ``expired`` 时自动重新获取二维码
 * 5. 二维码过期（超过 ``expires_in`` 秒）后自动刷新
 */
export function QrcodeAuthBlock({
  channelName,
  token,
  domain = "feishu",
  onSuccess,
}: QrcodeAuthBlockProps) {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const pollTimerRef = useRef<number | null>(null);
  const expireTimerRef = useRef<number | null>(null);
  const cancelledRef = useRef(false);
  // 设计 §4.5: 每次 begin/refresh 递增 generation,poll 响应只有在 generation
  // 仍匹配时才允许更新状态。防止"用户连点刷新"或"过期自动 refresh 时旧 poll
  // 网络回包晚到"导致旧二维码状态覆盖新二维码。
  const generationRef = useRef(0);

  const clearTimers = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (expireTimerRef.current !== null) {
      window.clearTimeout(expireTimerRef.current);
      expireTimerRef.current = null;
    }
  }, []);

  const startPolling = useCallback(
    (begin: ChannelQrBeginPayload, generation: number) => {
      clearTimers();
      setPhase({ kind: "polling", begin });

      const intervalMs = Math.max(2, begin.interval) * 1000;

      const pollOnce = async () => {
        if (cancelledRef.current) return;
        // Capture generation at call time; if a newer begin has started,
        // silently drop this poll's result without touching phase.
        if (generationRef.current !== generation) return;
        try {
          const res: ChannelQrStatusPayload = await pollChannelQrStatus(
            token,
            channelName,
            begin.poll_token,
            domain,
          );
          if (cancelledRef.current) return;
          if (generationRef.current !== generation) return;

          if (res.status === "succeeded") {
            clearTimers();
            setPhase({ kind: "succeeded", config: res.config ?? null });
            if (res.config) onSuccess(res.config);
          } else if (res.status === "failed") {
            clearTimers();
            setPhase({
              kind: "failed",
              error: res.error || t("channels.qr.errors.failed"),
            });
          } else if (res.status === "expired") {
            clearTimers();
            // 二维码过期，自动刷新
            void handleBegin();
          }
          // pending → 继续轮询
        } catch (e) {
          if (cancelledRef.current) return;
          if (generationRef.current !== generation) return;
          // 网络错误不立即失败，继续重试（轮询间隔后会再次尝试）
          console.warn("[QrcodeAuthBlock] poll error:", e);
        }
      };

      // 立即轮询一次，然后按 interval 重复
      void pollOnce();
      pollTimerRef.current = window.setInterval(pollOnce, intervalMs);

      // 设置过期定时器，到期后刷新二维码
      const expiresMs = Math.max(30, begin.expires_in) * 1000;
      expireTimerRef.current = window.setTimeout(() => {
        if (cancelledRef.current) return;
        if (generationRef.current !== generation) return;
        // 二维码过期，自动刷新
        void handleBegin();
      }, expiresMs);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [token, channelName, domain, clearTimers, onSuccess, t],
  );

  const handleBegin = useCallback(async () => {
    clearTimers();
    // 递增 generation:之前 in-flight 的 begin/poll/expire 回调在写状态前
    // 都会检查 generation,generation 不匹配则丢弃,从而避免旧二维码的
    // 迟到响应覆盖新二维码状态(设计 §4.5)。
    const generation = ++generationRef.current;
    setPhase({ kind: "loading" });
    cancelledRef.current = false;
    try {
      const begin = await beginChannelQrLogin(token, channelName, domain);
      if (cancelledRef.current) return;
      if (generationRef.current !== generation) return;
      setPhase({ kind: "awaiting-scan", begin });
      // 短暂展示"等待扫码"状态后开始轮询
      window.setTimeout(() => {
        if (cancelledRef.current) return;
        if (generationRef.current !== generation) return;
        if (begin) {
          startPolling(begin, generation);
        }
      }, 500);
    } catch (e) {
      if (cancelledRef.current) return;
      if (generationRef.current !== generation) return;
      setPhase({
        kind: "failed",
        error: (e as Error).message || t("channels.qr.errors.beginFailed"),
      });
    }
  }, [token, channelName, domain, clearTimers, startPolling, t]);

  // 卸载时清理定时器并标记取消,防止 in-flight 回调写已卸载组件状态
  useEffect(() => {
    return () => {
      cancelledRef.current = true;
      // 递增 generation 使任何 in-flight 回调在写状态前被丢弃
      generationRef.current += 1;
      clearTimers();
    };
  }, [clearTimers]);

  // ------------------------------------------------------------------
  // 渲染
  // ------------------------------------------------------------------

  if (phase.kind === "idle") {
    return (
      <div className="rounded-lg border border-dashed border-foreground/20 bg-muted/20 p-3">
        <div className="flex items-start gap-2.5">
          <QrCode className="mt-0.5 h-4 w-4 shrink-0 text-foreground/60" />
          <div className="flex-1">
            <p className="text-[11px] font-medium text-foreground/80">
              {t("channels.qr.title")}
            </p>
            <p className="mt-0.5 text-[10px] text-muted-foreground/70">
              {t("channels.qr.description")}
            </p>
          </div>
          <Button
            size="sm"
            className="h-7 gap-1 text-[11px]"
            onClick={handleBegin}
          >
            <QrCode className="h-3 w-3" />
            {t("channels.qr.begin")}
          </Button>
        </div>
      </div>
    );
  }

  if (phase.kind === "loading") {
    return (
      <div className="rounded-lg border border-dashed border-foreground/20 bg-muted/20 p-3">
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {t("channels.qr.loading")}
        </div>
      </div>
    );
  }

  if (phase.kind === "failed") {
    return (
      <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3">
        <div className="flex items-start gap-2.5">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <div className="flex-1">
            <p className="text-[11px] font-medium text-destructive">
              {t("channels.qr.errors.title")}
            </p>
            <p className="mt-0.5 text-[10px] text-muted-foreground">
              {phase.error}
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1 text-[11px]"
            onClick={handleBegin}
          >
            <RefreshCw className="h-3 w-3" />
            {t("channels.qr.retry")}
          </Button>
        </div>
      </div>
    );
  }

  if (phase.kind === "succeeded") {
    return (
      <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3">
        <div className="flex items-start gap-2.5">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
          <div className="flex-1">
            <p className="text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
              {t("channels.qr.success")}
            </p>
            <p className="mt-0.5 text-[10px] text-muted-foreground/70">
              {t("channels.qr.successHint")}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 text-[11px]"
            onClick={handleBegin}
          >
            <RefreshCw className="h-3 w-3" />
            {t("channels.qr.beginAgain")}
          </Button>
        </div>
      </div>
    );
  }

  // awaiting-scan or polling
  const begin = phase.begin;
  const isPolling = phase.kind === "polling";
  return (
    <div className="rounded-lg border border-foreground/15 bg-background p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
        {/* 二维码 —— 直接显示后端生成的 base64 PNG */}
        <div className="flex flex-col items-center gap-1.5">
          <div
            className={cn(
              "rounded-md border border-foreground/10 bg-white p-2",
              isPolling && "animate-pulse-soft",
            )}
          >
            <img
              src={`data:image/png;base64,${begin.qrcode_image}`}
              alt={t("channels.qr.title")}
              width={120}
              height={120}
              className="h-[120px] w-[120px]"
            />
          </div>
          <span className="text-[10px] text-muted-foreground/60">
            {t("channels.qr.expiresIn", { seconds: begin.expires_in })}
          </span>
        </div>

        {/* 状态描述 */}
        <div className="flex-1">
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-foreground/80">
            <Smartphone className="h-3.5 w-3.5" />
            {t("channels.qr.scanPrompt", { channel: channelName })}
          </div>
          <p className="mt-1 text-[10px] text-muted-foreground/70">
            {t("channels.qr.scanHint")}
          </p>
          {isPolling ? (
            <div className="mt-2 flex items-center gap-1.5 text-[10px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {t("channels.qr.waiting")}
            </div>
          ) : (
            <div className="mt-2 flex items-center gap-1.5 text-[10px] text-muted-foreground">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
              {t("channels.qr.preparing")}
            </div>
          )}
          <button
            type="button"
            onClick={handleBegin}
            className="mt-2 inline-flex items-center gap-1 text-[10px] text-muted-foreground/60 hover:text-foreground"
          >
            <RefreshCw className="h-2.5 w-2.5" />
            {t("channels.qr.refresh")}
          </button>
        </div>
      </div>
    </div>
  );
}
