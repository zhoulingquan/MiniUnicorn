// Advanced section 子 hook：networkSafety (web 安全) 的 form / dirty / save 逻辑。
//
// 从 useSettingsState 抽取，降低主 hook 复杂度。
// 行为保持与拆分前一致：
//   - form 初值、dirty 判定、save 流程（applyPayload → pendingRestart → maybeRestart → setError）原样迁入
//   - form 同步改为监听 settings 变化（原 applyPayload 中的同步逻辑移至此处 useEffect）

import { Dispatch, SetStateAction, useCallback, useEffect, useMemo, useState } from "react";

import { updateNetworkSafetySettings } from "@/lib/api";
import type { NetworkSafetySettingsUpdate, SettingsPayload } from "@/lib/types";

import { visibleWebuiDefaultAccessMode, type RestartAwarePayload } from "../types";
import type { UseSectionShared } from "../types";

/** Advanced section 暴露的状态与回调 */
export interface AdvancedSectionState {
  networkSafetyForm: NetworkSafetySettingsUpdate;
  setNetworkSafetyForm: Dispatch<SetStateAction<NetworkSafetySettingsUpdate>>;
  networkSafetySaving: boolean;
  networkSafetyDirty: boolean;
  saveNetworkSafetySettings: () => Promise<void>;
}

export function useAdvancedSection(shared: UseSectionShared): AdvancedSectionState {
  const {
    settings,
    token,
    setError,
    applyPayload,
    setPendingRestartSections,
    maybeRestartHostEngine,
  } = shared;

  const [networkSafetyForm, setNetworkSafetyForm] = useState<NetworkSafetySettingsUpdate>({
    webuiAllowLocalServiceAccess: true,
    webuiDefaultAccessMode: "default",
  });
  const [networkSafetySaving, setNetworkSafetySaving] = useState(false);

  // 监听 settings 变化同步 form（原 applyPayload 中的逻辑，移至此处）
  useEffect(() => {
    if (!settings) return;
    const adv = settings.advanced;
    setNetworkSafetyForm({
      webuiAllowLocalServiceAccess:
        adv.webui_allow_local_service_access ?? adv.allow_local_preview_access ?? true,
      webuiDefaultAccessMode: visibleWebuiDefaultAccessMode(adv.webui_default_access_mode),
    });
  }, [settings]);

  const networkSafetyDirty = useMemo(() => {
    if (!settings) return false;
    const adv = settings.advanced;
    const currentLocalServiceAccess =
      adv.webui_allow_local_service_access ?? adv.allow_local_preview_access ?? true;
    const currentDefaultAccess = visibleWebuiDefaultAccessMode(adv.webui_default_access_mode);
    return (
      networkSafetyForm.webuiAllowLocalServiceAccess !== currentLocalServiceAccess ||
      networkSafetyForm.webuiDefaultAccessMode !== currentDefaultAccess
    );
  }, [networkSafetyForm, settings]);

  const saveNetworkSafetySettings = useCallback(async () => {
    if (!settings || !networkSafetyDirty || networkSafetySaving) return;
    setNetworkSafetySaving(true);
    try {
      const payload: SettingsPayload = await updateNetworkSafetySettings(token, networkSafetyForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload as RestartAwarePayload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setNetworkSafetySaving(false);
    }
  }, [settings, networkSafetyDirty, networkSafetySaving, networkSafetyForm, token, applyPayload, setPendingRestartSections, maybeRestartHostEngine, setError]);

  return {
    networkSafetyForm,
    setNetworkSafetyForm,
    networkSafetySaving,
    networkSafetyDirty,
    saveNetworkSafetySettings,
  };
}
