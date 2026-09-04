// Runtime section 子 hook：runtime (heartbeat/dream) + planner 的 form / dirty / save 逻辑。
//
// 从 useSettingsState 抽取，降低主 hook 复杂度。
// Overview section 中的"系统"区域（心跳间隔、Dream cron、心跳模型、Plan & Execute）使用此 hook。
// 行为保持与拆分前一致：
//   - form 初值、dirty 判定、save 流程（applyPayload → pendingRestart → maybeRestart → setError）原样迁入
//   - form 同步改为监听 settings 变化（原 applyPayload 中的同步逻辑移至此处 useEffect）

import { Dispatch, SetStateAction, useCallback, useEffect, useMemo, useState } from "react";

import { updateRuntimeSettings, updateSettings } from "@/lib/api";
import type { RuntimeSettingsUpdate, SettingsPayload } from "@/lib/types";

import { extractDreamCron, type RestartAwarePayload } from "../types";
import type { UseSectionShared } from "../types";

/** Runtime section 暴露的状态与回调 */
export interface RuntimeSectionState {
  runtimeForm: RuntimeSettingsUpdate;
  setRuntimeForm: Dispatch<SetStateAction<RuntimeSettingsUpdate>>;
  runtimeSaving: boolean;
  runtimeDirty: boolean;
  saveRuntimeSettings: () => Promise<void>;
  plannerSaving: boolean;
  savePlannerSettings: (update: { usePlanner?: boolean; plannerModel?: string | null }) => Promise<void>;
}

export function useRuntimeSection(shared: UseSectionShared): RuntimeSectionState {
  const {
    settings,
    token,
    setError,
    applyPayload,
    setPendingRestartSections,
    maybeRestartHostEngine,
  } = shared;

  const [runtimeForm, setRuntimeForm] = useState<RuntimeSettingsUpdate>({
    heartbeatIntervalS: 3600,
    dreamCron: "0 3 * * *",
    heartbeatModelPreset: "",
  });
  const [runtimeSaving, setRuntimeSaving] = useState(false);
  const [plannerSaving, setPlannerSaving] = useState(false);

  // 监听 settings 变化同步 form（原 applyPayload 中的逻辑，移至此处）
  useEffect(() => {
    if (!settings) return;
    const rt = settings.runtime;
    setRuntimeForm({
      heartbeatIntervalS: rt.heartbeat.interval_s,
      dreamCron: extractDreamCron(rt.dream.schedule),
      heartbeatModelPreset: rt.heartbeat.model_preset ?? "",
    });
  }, [settings]);

  const runtimeDirty = useMemo(() => {
    if (!settings) return false;
    const rt = settings.runtime;
    const hbPresetChanged =
      (runtimeForm.heartbeatModelPreset ?? "") !== (rt.heartbeat.model_preset ?? "");
    return (
      runtimeForm.heartbeatIntervalS !== rt.heartbeat.interval_s ||
      (runtimeForm.dreamCron ?? "") !== extractDreamCron(rt.dream.schedule) ||
      hbPresetChanged
    );
  }, [runtimeForm, settings]);

  const saveRuntimeSettings = useCallback(async () => {
    if (!settings || !runtimeDirty || runtimeSaving) return;
    setRuntimeSaving(true);
    try {
      const rt = settings.runtime;
      // 仅发送发生变化的字段，避免意外清除其他 runtime 配置。
      const update: RuntimeSettingsUpdate = {};
      if (runtimeForm.heartbeatIntervalS !== undefined && runtimeForm.heartbeatIntervalS !== rt.heartbeat.interval_s) {
        update.heartbeatIntervalS = runtimeForm.heartbeatIntervalS;
      }
      const dreamCurrent = extractDreamCron(rt.dream.schedule);
      if (runtimeForm.dreamCron !== undefined && (runtimeForm.dreamCron ?? "") !== dreamCurrent) {
        update.dreamCron = runtimeForm.dreamCron ?? "";
      }
      const hbPresetChanged =
        (runtimeForm.heartbeatModelPreset ?? "") !== (rt.heartbeat.model_preset ?? "");
      if (hbPresetChanged) {
        update.heartbeatModelPreset = runtimeForm.heartbeatModelPreset ?? "";
      }
      const payload: SettingsPayload = await updateRuntimeSettings(token, update);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload as RestartAwarePayload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRuntimeSaving(false);
    }
  }, [settings, runtimeDirty, runtimeSaving, runtimeForm, token, applyPayload, setPendingRestartSections, maybeRestartHostEngine, setError]);

  const savePlannerSettings = useCallback(
    async (update: { usePlanner?: boolean; plannerModel?: string | null }) => {
      if (!settings || plannerSaving) return;
      setPlannerSaving(true);
      try {
        const payload: SettingsPayload = await updateSettings(token, update);
        applyPayload(payload);
        if (payload.requires_restart) {
          setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
        }
        await maybeRestartHostEngine(payload as RestartAwarePayload);
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setPlannerSaving(false);
      }
    },
    [settings, plannerSaving, token, applyPayload, setPendingRestartSections, maybeRestartHostEngine, setError],
  );

  return {
    runtimeForm,
    setRuntimeForm,
    runtimeSaving,
    runtimeDirty,
    saveRuntimeSettings,
    plannerSaving,
    savePlannerSettings,
  };
}
