// Advanced section 子 hook：networkSafety (web 安全) 的 form / dirty / save 逻辑。
//
// 从 useSettingsState 抽取，降低主 hook 复杂度。
// 行为保持与拆分前一致：
//   - form 初值、dirty 判定、save 流程（applyPayload → pendingRestart → maybeRestart → setError）原样迁入
//   - form 同步改为监听 settings 变化（原 applyPayload 中的同步逻辑移至此处 useEffect）
//   - save 流程统一走 useSaveAction 原语

import { Dispatch, SetStateAction, useEffect, useMemo, useState } from "react";

import { updateNetworkSafetySettings } from "@/lib/api";
import type { NetworkSafetySettingsUpdate } from "@/lib/types";

import { visibleWebuiDefaultAccessMode } from "../types";
import { useSaveAction } from "./useSaveAction";
import type { SaveActionSharedDeps } from "./useSaveAction";
import type { UseSectionShared } from "./useWebSearchSection";

/** Advanced section 暴露的状态与回调 */
export interface AdvancedSectionState {
  networkSafetyForm: NetworkSafetySettingsUpdate;
  setNetworkSafetyForm: Dispatch<SetStateAction<NetworkSafetySettingsUpdate>>;
  networkSafetySaving: boolean;
  networkSafetyDirty: boolean;
  saveNetworkSafetySettings: () => Promise<void>;
}

export function useAdvancedSection(shared: UseSectionShared): AdvancedSectionState {
  const { settings, token } = shared;

  const [networkSafetyForm, setNetworkSafetyForm] = useState<NetworkSafetySettingsUpdate>({
    webuiAllowLocalServiceAccess: true,
    webuiDefaultAccessMode: "default",
  });

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

  const sharedDeps: SaveActionSharedDeps = {
    applyPayload: shared.applyPayload,
    setError: shared.setError,
    setPendingRestartSections: shared.setPendingRestartSections,
    maybeRestartHostEngine: shared.maybeRestartHostEngine,
  };

  const networkSafetyAction = useSaveAction<void, NetworkSafetySettingsUpdate>({
    shared: sharedDeps,
    token,
    enabled: !!settings && networkSafetyDirty,
    buildPayload: () => networkSafetyForm,
    apiCall: updateNetworkSafetySettings,
    restartSectionKey: "runtime",
  });

  return {
    networkSafetyForm,
    setNetworkSafetyForm,
    networkSafetySaving: networkSafetyAction.saving,
    networkSafetyDirty,
    saveNetworkSafetySettings: networkSafetyAction.save,
  };
}
