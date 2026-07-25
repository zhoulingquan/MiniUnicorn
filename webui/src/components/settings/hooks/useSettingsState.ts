// Settings 状态管理 hook：共享层 + 各 section 子 hook 的组装。
//
// 职责：
//  - 持有全局共享状态：settings / loading / error / activeSection / localPrefs /
//    pendingRestartSections / restartConfirmOpen / hostEngineApplying
//  - 提供 applyPayload（简化版：仅 setSettings + setPendingRestartSections + onSettingsChange）
//  - 提供 restart 相关回调：restartViaSettingsSurface / maybeRestartHostEngine / confirmRestart / cancelRestart
//  - 组装各 section 子 hook：webSearch / advanced / runtime / models
//
// 各 section 的 form / dirty / save 逻辑由对应子 hook 自治，
// 通过监听 settings 变化自行同步 form（替代原 applyPayload 内的 setForm 调用）。

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import { fetchSettings } from "@/lib/api";
import { getHostApi } from "@/lib/runtime";
import type { SettingsPayload } from "@/lib/types";

import {
  EMPTY_PENDING_RESTART_SECTIONS,
  LOCAL_PREFS_STORAGE_KEY,
  readLocalPreferences,
  type LocalPreferences,
  type PendingRestartSections,
  type RestartAwarePayload,
  type SettingsSectionKey,
} from "../types";
import { useWebSearchSection } from "./useWebSearchSection";
import { useAdvancedSection } from "./useAdvancedSection";
import { useRuntimeSection } from "./useRuntimeSection";
import { useModelsAndProvidersSection } from "./useModelsAndProvidersSection";
import { useImageGenerationSection } from "./useImageGenerationSection";

export interface UseSettingsStateParams {
  token: string;
  initialSection?: SettingsSectionKey;
  onSettingsChange?: (payload: SettingsPayload) => void;
  onModelNameChange: (modelName: string | null) => void;
  onRestart?: () => void;
}

export function useSettingsState({
  token,
  initialSection = "overview",
  onSettingsChange,
  onModelNameChange,
  onRestart,
}: UseSettingsStateParams) {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<SettingsSectionKey>(initialSection);
  const [localPrefs, setLocalPrefs] = useState<LocalPreferences>(() => readLocalPreferences());
  const [pendingRestartSections, setPendingRestartSections] = useState<PendingRestartSections>(
    EMPTY_PENDING_RESTART_SECTIONS,
  );
  // 保存需要重启的设置后弹出询问对话框（替代旧版 native host 自动重启逻辑）
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false);
  const [hostEngineApplying, setHostEngineApplying] = useState(false);

  useEffect(() => {
    setActiveSection(initialSection);
  }, [initialSection]);

  const text = useCallback(
    (key: string, fallback: string, options?: Record<string, unknown>) =>
      t(key, { defaultValue: fallback, ...(options ?? {}) }),
    [t],
  );

  // 简化版 applyPayload：仅更新 settings + pendingRestart + 通知外部。
  // 各 section 的 form 同步由子 hook 监听 settings 变化自行处理。
  const applyPayload = useCallback((payload: SettingsPayload) => {
    setSettings(payload);
    if (payload.restart_required_sections) {
      setPendingRestartSections({
        runtime: payload.restart_required_sections.includes("runtime"),
        browser: payload.restart_required_sections.includes("browser"),
        images: payload.restart_required_sections.includes("images"),
      });
    }
    onSettingsChange?.(payload);
  }, [onSettingsChange]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchSettings(token)
      .then((payload) => {
        if (!cancelled) {
          applyPayload(payload);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applyPayload, token]);

  useEffect(() => {
    try {
      window.localStorage.setItem(LOCAL_PREFS_STORAGE_KEY, JSON.stringify(localPrefs));
    } catch {
      // Browser-only preferences should never block settings.
    }
  }, [localPrefs]);

  const hasPendingRestart = useMemo(
    () =>
      !!settings?.requires_restart ||
      pendingRestartSections.runtime ||
      pendingRestartSections.browser,
    [pendingRestartSections, settings?.requires_restart],
  );

  const restartViaSettingsSurface = useCallback(async () => {
    const isNativeHost = (settings?.surface ?? settings?.runtime_surface) === "native";
    const hostApi = getHostApi();
    if (isNativeHost && settings?.runtime_capabilities?.can_restart_engine && hostApi) {
      setHostEngineApplying(true);
      try {
        await hostApi.restartEngine();
        const payload = await fetchSettings(token);
        applyPayload(payload);
        setPendingRestartSections(EMPTY_PENDING_RESTART_SECTIONS);
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setHostEngineApplying(false);
      }
      return;
    }
    onRestart?.();
  }, [applyPayload, onRestart, settings, token]);

  const maybeRestartHostEngine = useCallback(
    async (payload: RestartAwarePayload) => {
      // 保存后若 requires_restart=true，弹出对话框询问用户是否重启。
      // 不再自动重启（即使 native host 支持 can_restart_engine），让用户显式确认。
      if (!payload.requires_restart) return;
      setRestartConfirmOpen(true);
    },
    [],
  );

  // 用户在重启询问对话框点击"重启"
  const confirmRestart = useCallback(async () => {
    setRestartConfirmOpen(false);
    await restartViaSettingsSurface();
  }, [restartViaSettingsSurface]);

  // 用户在重启询问对话框点击"稍后"或关闭
  const cancelRestart = useCallback(() => {
    setRestartConfirmOpen(false);
  }, []);

  // === 共享依赖（传入各 section 子 hook） ===
  const shared = {
    settings,
    token,
    setError,
    applyPayload,
    setPendingRestartSections,
    maybeRestartHostEngine,
  };

  // === 各 section 子 hook ===
  const webSearch = useWebSearchSection(shared);
  const advanced = useAdvancedSection(shared);
  const runtime = useRuntimeSection(shared);
  const models = useModelsAndProvidersSection({ ...shared, onModelNameChange });
  const imageGeneration = useImageGenerationSection(shared);

  return {
    // 共享状态
    settings,
    loading,
    error,
    activeSection,
    setActiveSection,
    localPrefs,
    setLocalPrefs,
    pendingRestartSections,
    hasPendingRestart,
    hostEngineApplying,
    restartConfirmOpen,
    confirmRestart,
    cancelRestart,
    restartViaSettingsSurface,

    // translation helper
    text,

    // 分域
    webSearch,
    advanced,
    runtime,
    models,
    imageGeneration,
  };
}
