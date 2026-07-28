// Runtime section 子 hook：runtime (heartbeat/dream) + planner 的 form / dirty / save 逻辑。
//
// 从 useSettingsState 抽取，降低主 hook 复杂度。
// Overview section 中的"系统"区域（心跳间隔、Dream cron、心跳模型、Plan & Execute）使用此 hook。
// 行为保持与拆分前一致：
//   - form 初值、dirty 判定、save 流程（applyPayload → pendingRestart → maybeRestart → setError）原样迁入
//   - form 同步改为监听 settings 变化（原 applyPayload 中的同步逻辑移至此处 useEffect）
//   - save 流程统一走 useSaveAction 原语

import { Dispatch, SetStateAction, useEffect, useMemo, useState } from "react";

import { updateRuntimeSettings, updateSettings } from "@/lib/api";
import type { RuntimeSettingsUpdate, SettingsUpdate } from "@/lib/types";

import { extractDreamCron } from "../types";
import { useSaveAction } from "./useSaveAction";
import type { SaveActionSharedDeps } from "./useSaveAction";
import type { UseSectionShared } from "./useWebSearchSection";

/** Planner save argument: partial update forwarded to the generic settings API. */
export type PlannerUpdate = { usePlanner?: boolean; plannerModel?: string | null };

/** Runtime section 暴露的状态与回调 */
export interface RuntimeSectionState {
  runtimeForm: RuntimeSettingsUpdate;
  setRuntimeForm: Dispatch<SetStateAction<RuntimeSettingsUpdate>>;
  runtimeSaving: boolean;
  runtimeDirty: boolean;
  saveRuntimeSettings: () => Promise<void>;
  plannerSaving: boolean;
  savePlannerSettings: (update: PlannerUpdate) => Promise<void>;
}

export function useRuntimeSection(shared: UseSectionShared): RuntimeSectionState {
  const { settings, token } = shared;

  const [runtimeForm, setRuntimeForm] = useState<RuntimeSettingsUpdate>({
    heartbeatIntervalS: 3600,
    dreamCron: "0 3 * * *",
    heartbeatModelPreset: "",
  });

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

  const sharedDeps: SaveActionSharedDeps = {
    applyPayload: shared.applyPayload,
    setError: shared.setError,
    setPendingRestartSections: shared.setPendingRestartSections,
    maybeRestartHostEngine: shared.maybeRestartHostEngine,
  };

  const runtimeAction = useSaveAction<void, RuntimeSettingsUpdate>({
    shared: sharedDeps,
    token,
    enabled: !!settings && runtimeDirty,
    buildPayload: () => {
      // 仅发送发生变化的字段，避免意外清除其他 runtime 配置。
      const rt = settings!.runtime;
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
      return update;
    },
    apiCall: updateRuntimeSettings,
    restartSectionKey: "runtime",
  });

  const plannerAction = useSaveAction<PlannerUpdate, SettingsUpdate>({
    shared: sharedDeps,
    token,
    enabled: !!settings,
    buildPayload: (arg) => arg,
    apiCall: (tok, payload) => updateSettings(tok, payload),
    restartSectionKey: "runtime",
  });

  return {
    runtimeForm,
    setRuntimeForm,
    runtimeSaving: runtimeAction.saving,
    runtimeDirty,
    saveRuntimeSettings: runtimeAction.save,
    plannerSaving: plannerAction.saving,
    savePlannerSettings: plannerAction.save,
  };
}
