// WebSearch section 子 hook：web_search + web_fetch 的 form / dirty / save 逻辑。
//
// 从 useSettingsState 抽取，降低主 hook 复杂度。
// 行为保持与拆分前一致：
//   - form 初值、dirty 判定、save 流程（applyPayload → pendingRestart → maybeRestart → setError）原样迁入
//   - form 同步改为监听 settings 变化（原 applyPayload 中的同步逻辑移至此处 useEffect）
//   - save 流程统一走 useSaveAction 原语（in-flight guard + saving + error + restart）
//
// 共享依赖由主 hook 传入：settings / token / setError / applyPayload / setPendingRestartSections / maybeRestartHostEngine

import { Dispatch, SetStateAction, useEffect, useMemo, useState } from "react";

import { updateWebFetchSettings, updateWebSearchSettings } from "@/lib/api";
import type {
  SettingsPayload,
  WebFetchSettingsUpdate,
  WebSearchSettingsUpdate,
} from "@/lib/types";

import type { PendingRestartSections, RestartAwarePayload } from "../types";
import { useSaveAction } from "./useSaveAction";
import type { SaveActionSharedDeps } from "./useSaveAction";

/** 主 hook 传入的共享依赖 */
export interface UseSectionShared {
  settings: SettingsPayload | null;
  token: string;
  setError: (msg: string | null) => void;
  applyPayload: (payload: SettingsPayload) => void;
  setPendingRestartSections: Dispatch<SetStateAction<PendingRestartSections>>;
  maybeRestartHostEngine: (payload: RestartAwarePayload) => Promise<void>;
}

/** WebSearch section 暴露的状态与回调 */
export interface WebSearchSectionState {
  webSearchForm: WebSearchSettingsUpdate;
  setWebSearchForm: Dispatch<SetStateAction<WebSearchSettingsUpdate>>;
  webSearchSaving: boolean;
  webSearchDirty: boolean;
  saveWebSearchSettings: () => Promise<void>;
  webFetchForm: WebFetchSettingsUpdate;
  setWebFetchForm: Dispatch<SetStateAction<WebFetchSettingsUpdate>>;
  webFetchSaving: boolean;
  webFetchDirty: boolean;
  saveWebFetchSettings: () => Promise<void>;
}

export function useWebSearchSection(shared: UseSectionShared): WebSearchSectionState {
  const {
    settings,
    token,
  } = shared;

  const [webSearchForm, setWebSearchForm] = useState<WebSearchSettingsUpdate>({
    enable: true,
    provider: "auto",
    max_results: 5,
    timeout: 30,
    proxy: "",
    backends: {},
  });
  const [webFetchForm, setWebFetchForm] = useState<WebFetchSettingsUpdate>({
    useJinaReader: true,
  });

  // 监听 settings 变化同步 form（原 applyPayload 中的逻辑，移至此处）
  useEffect(() => {
    if (!settings) return;
    // web_search form 从 payload 初始化；backends 的 api_key 字段用空串占位，
    // 后端只回传 hint，真实 key 不下发到前端
    if (settings.web_search) {
      const ws = settings.web_search;
      const backendsDraft: WebSearchSettingsUpdate["backends"] = {};
      for (const [name, info] of Object.entries(ws.backends ?? {})) {
        backendsDraft[name] = {
          api_key: "",
          base_url: info.base_url ?? "",
          timeout: info.timeout ?? 30,
        };
      }
      setWebSearchForm({
        enable: ws.enable,
        provider: ws.provider,
        max_results: ws.max_results,
        timeout: ws.timeout,
        proxy: ws.proxy ?? "",
        backends: backendsDraft,
      });
    }
    // web_fetch form（jina reader 开关）
    if (settings.web?.fetch) {
      setWebFetchForm({
        useJinaReader: settings.web.fetch.use_jina_reader,
      });
    }
  }, [settings]);

  const webSearchDirty = useMemo(() => {
    if (!settings?.web_search) return false;
    const ws = settings.web_search;
    if (webSearchForm.enable !== ws.enable) return true;
    if (webSearchForm.provider !== ws.provider) return true;
    if (webSearchForm.max_results !== ws.max_results) return true;
    if (webSearchForm.timeout !== ws.timeout) return true;
    if ((webSearchForm.proxy || null) !== (ws.proxy ?? null)) return true;
    // backends：任何 api_key 非空都视为 dirty（用户输入了新 key）
    for (const draft of Object.values(webSearchForm.backends)) {
      if (draft.api_key) return true;
    }
    // backends base_url / timeout 变化也算 dirty
    for (const [name, draft] of Object.entries(webSearchForm.backends)) {
      const cur = ws.backends?.[name];
      if (!cur) return true; // 新增后端
      if (draft.base_url !== cur.base_url) return true;
      if (draft.timeout !== cur.timeout) return true;
    }
    // 后端有但 form 没有的：用户没改动不算 dirty（初始化时已补全）
    return false;
  }, [webSearchForm, settings]);

  const webFetchDirty = useMemo(() => {
    if (!settings?.web?.fetch) return false;
    return webFetchForm.useJinaReader !== settings.web.fetch.use_jina_reader;
  }, [webFetchForm, settings]);

  const sharedDeps: SaveActionSharedDeps = {
    applyPayload: shared.applyPayload,
    setError: shared.setError,
    setPendingRestartSections: shared.setPendingRestartSections,
    maybeRestartHostEngine: shared.maybeRestartHostEngine,
  };

  const webSearchAction = useSaveAction<void, WebSearchSettingsUpdate>({
    shared: sharedDeps,
    token,
    enabled: !!settings && webSearchDirty,
    buildPayload: () => webSearchForm,
    apiCall: updateWebSearchSettings,
    restartSectionKey: "browser",
  });

  const webFetchAction = useSaveAction<void, WebFetchSettingsUpdate>({
    shared: sharedDeps,
    token,
    enabled: !!settings && webFetchDirty,
    buildPayload: () => webFetchForm,
    apiCall: updateWebFetchSettings,
    restartSectionKey: "browser",
  });

  return {
    webSearchForm,
    setWebSearchForm,
    webSearchSaving: webSearchAction.saving,
    webSearchDirty,
    saveWebSearchSettings: webSearchAction.save,
    webFetchForm,
    setWebFetchForm,
    webFetchSaving: webFetchAction.saving,
    webFetchDirty,
    saveWebFetchSettings: webFetchAction.save,
  };
}
