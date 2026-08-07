// Heartbeat 专用 LLM 配置行:Overview section 内嵌使用。
// 从 SettingsView.tsx 拆分而来。

import type { Dispatch, SetStateAction } from "react";
import { KeyRound } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { RuntimeSettingsUpdate, SettingsPayload } from "@/lib/types";

import { ModelPresetSelect } from "./ModelPresetSelect";
import { SettingsRow } from "./SettingsRow";

/**
 * Heartbeat 专用 LLM 配置。一个 SettingsRow,右侧下拉列表展示所有已配置的
 * model_presets,让用户为 heartbeat 选择一个专用 LLM;选"使用主模型"
 * 则 heartbeat 复用 agent 主 provider/model。
 */
export function HeartbeatLlmConfig({
  runtimeForm,
  onChangeRuntimeForm,
  settings,
}: {
  runtimeForm: RuntimeSettingsUpdate;
  onChangeRuntimeForm: Dispatch<SetStateAction<RuntimeSettingsUpdate>>;
  settings: SettingsPayload;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const presets = settings.model_presets;
  const visiblePresets = presets.filter((p) => !p.is_default);
  const currentValue = runtimeForm.heartbeatModelPreset ?? settings.runtime.heartbeat.model_preset ?? "";
  const selectedPreset = visiblePresets.find((p) => p.name === currentValue) ?? null;
  const defaultOptionLabel = tx("settings.heartbeat.useMain", "Main model");

  return (
    <SettingsRow
      icon={KeyRound}
      title={tx("settings.heartbeat.llmTitle", "Heartbeat LLM")}
      description={
        selectedPreset
          ? t("settings.heartbeat.configuredHint", {
              defaultValue: "Using: {{model}}",
              model: selectedPreset.model,
            })
          : tx("settings.heartbeat.defaultHint", "Using main agent model.")
      }
    >
      <ModelPresetSelect
        defaultSentinel=""
        value={currentValue}
        presets={presets}
        settings={settings}
        label={tx("settings.heartbeat.llmTitle", "Heartbeat LLM")}
        defaultOptionLabel={defaultOptionLabel}
        onChange={(value) =>
          onChangeRuntimeForm((prev) => ({
            ...prev,
            heartbeatModelPreset: value ?? "",
          }))
        }
      />
    </SettingsRow>
  );
}
