// Models section:当前模型选择 + 上下文窗口徽章。
// 从 SettingsView.tsx 拆分而来。

import type { Dispatch, SetStateAction } from "react";
import { useTranslation } from "react-i18next";

import type { SettingsPayload } from "@/lib/types";

import {
  editableDefaultProvider,
  type AgentSettingsDraft,
} from "../types";
import { ContextWindowBadge } from "../components/ContextWindowBadge";
import { ModelPresetPicker } from "../components/ModelPresetPicker";
import { SettingsFooter } from "../components/RestartSettingsFooter";
import {
  SettingsGroup,
  SettingsRow,
} from "../components/SettingsRow";

export function ModelsSettings({
  form,
  setForm,
  settings,
  dirty,
  saving,
  contextWindowLearning,
  contextWindowLearnTimeout,
  showBrandLogos,
  onSave,
  onSaveContextWindow,
  onCreateConfiguration,
}: {
  form: AgentSettingsDraft;
  setForm: Dispatch<SetStateAction<AgentSettingsDraft>>;
  settings: SettingsPayload;
  dirty: boolean;
  saving: boolean;
  contextWindowLearning: boolean;
  contextWindowLearnTimeout: boolean;
  showBrandLogos: boolean;
  onSave: () => void;
  onSaveContextWindow: (value: number) => Promise<void>;
  onCreateConfiguration: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  // 上下文窗口跟随当前选中的 preset(未保存时也立即反映),
  // 而不是 settings.agent(只有保存后才会更新)
  const selectedPreset = settings.model_presets.find((p) => p.name === form.modelPreset);
  const ctxResolved = selectedPreset?.resolved_context_window_tokens ?? settings.agent.resolved_context_window_tokens;
  const ctxConfigured = selectedPreset?.context_window_tokens ?? settings.agent.context_window_tokens;
  const ctxStatus = selectedPreset?.resolved_context_window_status ?? settings.agent.resolved_context_window_status;
  const ctxError = selectedPreset?.resolved_context_window_error ?? settings.agent.resolved_context_window_error;
  return (
    <div className="space-y-7">
      <section>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.currentModel", "Current model")}
            description={tx("settings.help.currentModel", "Choose the model Erza uses for new replies.")}
          >
            <ModelPresetPicker
              presets={settings.model_presets}
              value={form.modelPreset}
              settings={settings}
              draftModel={form.model}
              draftProvider={form.provider}
              showProviderLogos={showBrandLogos}
              onChange={(modelPreset) => {
                const nextPreset = settings.model_presets.find((preset) => preset.name === modelPreset);
                setForm((prev) => ({
                  ...prev,
                  modelPreset,
                  model: nextPreset?.model ?? prev.model,
                  provider: nextPreset?.is_default
                    ? editableDefaultProvider(settings)
                    : nextPreset?.provider ?? prev.provider,
                  presetLabel: nextPreset?.label ?? modelPreset,
                }));
              }}
              onCreateConfiguration={onCreateConfiguration}
            />
          </SettingsRow>
          <SettingsRow
            title={t("settings.rows.contextWindow")}
            description={t("settings.help.contextWindow")}
          >
            <ContextWindowBadge
              key={`${form.modelPreset}-${ctxResolved}-${ctxStatus}`}
              resolved={ctxResolved}
              configured={ctxConfigured}
              status={ctxStatus}
              error={ctxError}
              timeout={contextWindowLearnTimeout}
              onSave={onSaveContextWindow}
            />
          </SettingsRow>
          <SettingsFooter
            dirty={dirty}
            saving={saving || contextWindowLearning}
            saved={false}
            disabled={false}
            savingLabel={contextWindowLearning ? t("settings.actions.queryingContext") : undefined}
            onSave={onSave}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}
