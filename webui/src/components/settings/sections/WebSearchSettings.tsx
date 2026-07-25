// Web Search section:web_search 工具配置(基础、网络、后端 API Key、Jina reader)

import { useMemo, type Dispatch, type SetStateAction } from "react";
import { useTranslation } from "react-i18next";

import type { WebSearchSettingsUpdate } from "@/lib/types";

import { NumberInput, ToggleButton } from "../components/SegmentedControl";
import { RestartSettingsFooter } from "../components/RestartSettingsFooter";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
  ClearableInput,
} from "../components/SettingsRow";

// 可配置 API Key / Base URL 的后端(只有这些需要在 UI 展示卡片)
// 精简为以 SearXNG 为主力的架构:searxng(主力,需 base_url) + tavily(AI 摘要,需 key)
// bing_cn 是免 Key 兜底后端,不需要展示凭证输入框
const KEYED_BACKENDS = [
  "searxng", // 需 base_url(自托管实例地址)
  "tavily",  // 需 api_key
] as const;

// 后端内部标识 → 展示名称
const BACKEND_DISPLAY_NAMES: Record<string, string> = {
  searxng: "SearXNG",
  tavily: "Tavily",
};

// 后端推荐默认超时(秒);与后端 SearchBackend.default_timeout 保持一致。
// 用户未配置时草稿显示此值,保存后覆盖为用户显式配置。
const BACKEND_DEFAULT_TIMEOUTS: Record<string, number> = {
  searxng: 10,  // 本地实例响应快
  tavily: 15,   // 云端 API 需更多缓冲
};

export function WebSearchSettings({
  form,
  dirty,
  saving,
  requiresRestartPending,
  onChangeForm,
  onSave,
  onRestart,
  isRestarting,
}: {
  form: WebSearchSettingsUpdate;
  dirty: boolean;
  saving: boolean;
  requiresRestartPending: boolean;
  onChangeForm: Dispatch<SetStateAction<WebSearchSettingsUpdate>>;
  onRestart?: () => void;
  isRestarting?: boolean;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  // 后端卡片需要的所有 backend 都展示,缺失的补上空草稿,便于用户填写
  const backendEntries = useMemo(() => {
    const result: Array<{ name: string; draft: WebSearchSettingsUpdate["backends"][string] }> = [];
    for (const name of KEYED_BACKENDS) {
      const draft = form.backends[name] ?? { api_key: "", base_url: "", timeout: BACKEND_DEFAULT_TIMEOUTS[name] ?? 30 };
      result.push({ name, draft });
    }
    // 额外展示用户已配置但不在 KEYED_BACKENDS 列表中的后端
    for (const [name, draft] of Object.entries(form.backends)) {
      if (!(KEYED_BACKENDS as readonly string[]).includes(name)) {
        result.push({ name, draft });
      }
    }
    return result;
  }, [form.backends]);

  const setField = <K extends keyof WebSearchSettingsUpdate>(
    key: K,
    value: WebSearchSettingsUpdate[K],
  ) => {
    onChangeForm((prev) => ({ ...prev, [key]: value }));
  };

  const setBackendField = (
    backendName: string,
    field: "api_key" | "base_url" | "timeout",
    value: string | number,
  ) => {
    onChangeForm((prev) => {
      const existing = prev.backends[backendName] ?? { api_key: "", base_url: "", timeout: 30 };
      return {
        ...prev,
        backends: {
          ...prev.backends,
          [backendName]: { ...existing, [field]: value },
        },
      };
    });
  };

  return (
    <div className="space-y-7">
      {/* 基础设置 */}
      <section>
        <SettingsSectionTitle>
          {tx("settings.sections.webSearchBasic", "Web Search")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.webSearchEnable", "Enable web_search")}
            description={tx(
              "settings.help.webSearchEnable",
              "Toggle the web_search tool on/off for the agent. Backend='auto' queries all engines concurrently and merges deduped results.",
            )}
          >
            <ToggleButton
              checked={form.enable}
              onChange={(v) => setField("enable", v)}
              ariaLabel={tx("settings.rows.webSearchEnable", "Enable web_search")}
              label={form.enable ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.webSearchMaxResults", "Max results")}
            description={tx(
              "settings.help.webSearchMaxResults",
              "Maximum number of results returned per query (1-10).",
            )}
          >
            <NumberInput
              value={form.max_results}
              min={1}
              max={10}
              onChange={(v) => setField("max_results", v)}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.webSearchTimeout", "Timeout")}
            description={tx(
              "settings.help.webSearchTimeout",
              "Per-request timeout in seconds.",
            )}
          >
            <NumberInput
              value={form.timeout}
              min={5}
              max={120}
              onChange={(v) => setField("timeout", v)}
              suffix={tx("settings.values.seconds", "s")}
            />
          </SettingsRow>
        </SettingsGroup>
      </section>

      {/* 网络 */}
      <section>
        <SettingsSectionTitle>
          {tx("settings.sections.webSearchCache", "Network")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.webSearchProxy", "Proxy")}
            description={tx(
              "settings.help.webSearchProxy",
              "HTTP proxy for overseas backends (e.g. tavily). Leave empty to use system env.",
            )}
          >
            <ClearableInput
              value={form.proxy}
              onChange={(e) => setField("proxy", e.target.value)}
              onClear={() => setField("proxy", "")}
              placeholder="http://127.0.0.1:7890"
              className="h-8 w-72 rounded-full text-[13px]"
            />
          </SettingsRow>
        </SettingsGroup>
      </section>

      {/* 后端 API Key 配置 */}
      <section>
        <SettingsSectionTitle>
          {tx("settings.sections.webSearchBackends", "Backend Credentials")}
        </SettingsSectionTitle>
        <SettingsGroup>
          {backendEntries.map(({ name, draft }) => (
            <div
              key={name}
              className="flex flex-col gap-3 px-4 py-3.5 sm:px-5"
            >
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[14px] font-medium leading-5 text-foreground">{BACKEND_DISPLAY_NAMES[name] ?? name}</div>
                  <div className="mt-0.5 text-[12px] leading-5 text-muted-foreground">
                    {tx(`settings.help.webSearchBackend_${name}`, backendHelpFallback(name))}
                  </div>
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <ClearableInput
                  value={draft.api_key}
                  onChange={(e) => setBackendField(name, "api_key", e.target.value)}
                  onClear={() => setBackendField(name, "api_key", "")}
                  placeholder={tx("settings.rows.webSearchApiKey", "API Key")}
                  className="h-8 w-56 rounded-full text-[13px]"
                />
                <ClearableInput
                  value={draft.base_url}
                  onChange={(e) => setBackendField(name, "base_url", e.target.value)}
                  onClear={() => setBackendField(name, "base_url", "")}
                  placeholder={tx("settings.rows.webSearchBaseUrl", "Base URL (optional)")}
                  className="h-8 w-56 rounded-full text-[13px]"
                />
                <NumberInput
                  value={draft.timeout}
                  min={5}
                  max={120}
                  onChange={(v) => setBackendField(name, "timeout", v)}
                  suffix={tx("settings.values.seconds", "s")}
                />
              </div>
            </div>
          ))}
        </SettingsGroup>
      </section>

      <RestartSettingsFooter
        dirty={dirty}
        saving={saving}
        pendingRestart={requiresRestartPending}
        onSave={onSave}
        onRestart={onRestart}
        isRestarting={isRestarting}
      />
    </div>
  );
}

function backendHelpFallback(name: string): string {
  switch (name) {
    case "searxng":
      return "SearXNG self-hosted meta search (requires base_url, aggregates 100+ engines, free)";
    case "tavily":
      return "Tavily AI Search API (requires key, returns LLM summary, 1000/mo free)";
    default:
      return "";
  }
}
