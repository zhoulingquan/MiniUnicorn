// Overview section:状态总览 + AI / Persona / System 三组配置入口。
// 从 SettingsView.tsx 拆分而来。

import type { Dispatch, SetStateAction } from "react";
import { Activity, Bot, HardDrive, Moon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Input } from "@/components/ui/input";
import type { RuntimeSettingsUpdate, SettingsPayload } from "@/lib/types";

import {
  cronToFreqValue,
  DREAM_FREQ_PRESETS,
  extractDreamCron,
  freqValueToCron,
  type DreamFreqValue,
  type SettingsSectionKey,
} from "../types";
import { BootstrapFileRow } from "../components/BootstrapFileRow";
import { DreamFilesButton } from "../components/DreamFilesButton";
import { HeartbeatLlmConfig } from "../components/HeartbeatLlmConfig";
import { NumberInput, SegmentedControl } from "../components/SegmentedControl";
import { PlannerConfig } from "../components/PlannerConfig";
import { RestartSettingsFooter } from "../components/RestartSettingsFooter";
import {
  OverviewListRow,
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
  StatusPill,
} from "../components/SettingsRow";

export function OverviewSettings({
  settings,
  requiresRestart,
  onRestart,
  isRestarting,
  onSelectSection,
  showBrandLogos,
  runtimeForm,
  runtimeDirty,
  runtimeSaving,
  onChangeRuntimeForm,
  onSaveRuntime,
  plannerSaving,
  onSavePlanner,
}: {
  settings: SettingsPayload;
  requiresRestart: boolean;
  onRestart?: () => void;
  isRestarting?: boolean;
  onSelectSection: (section: SettingsSectionKey) => void;
  showBrandLogos: boolean;
  runtimeForm: RuntimeSettingsUpdate;
  runtimeDirty: boolean;
  runtimeSaving: boolean;
  onChangeRuntimeForm: Dispatch<SetStateAction<RuntimeSettingsUpdate>>;
  onSaveRuntime: () => void;
  plannerSaving: boolean;
  onSavePlanner: (update: { usePlanner?: boolean; plannerModel?: string | null }) => Promise<void>;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const activePreset = settings.agent.model_preset || "default";
  const activeProvider = settings.agent.resolved_provider ?? settings.agent.provider;
  const usePlanner = settings.agent.use_planner;
  const plannerPreset = settings.agent.planner_model;
  return (
    <div className="space-y-7">
      <section>
        <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86 shadow-[0_18px_65px_rgba(15,23,42,0.075)] backdrop-blur-xl dark:border-white/10 dark:shadow-[0_18px_65px_rgba(0,0,0,0.24)]">
          <div className="flex flex-row items-center justify-between gap-4 px-5 py-5">
            <div className="flex min-w-0 items-center gap-3">
              <div className="min-w-0">
                <div className="text-[12px] font-medium text-muted-foreground">MiniUnicorn</div>
                <div className="mt-0.5 truncate text-[18px] font-semibold leading-6 text-foreground">
                  {settings.agent.model}
                </div>
                <div className="mt-0.5 truncate text-[13px] leading-5 text-muted-foreground">
                  {activeProvider} · {activePreset}
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 sm:justify-end">
              <StatusPill tone={requiresRestart ? "neutral" : "success"}>
                {requiresRestart
                  ? tx("settings.values.restartPending", "Restart pending")
                  : tx("settings.values.ready", "Ready")}
              </StatusPill>
            </div>
          </div>
        </div>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.ai", "AI")}</SettingsSectionTitle>
        <SettingsGroup>
          <OverviewListRow
            icon={Bot}
            valueLogoProvider={activeProvider}
            title={tx("settings.overview.model", "Current model")}
            value={settings.agent.model}
            caption={`${activeProvider} · ${activePreset}`}
            showBrandLogos={showBrandLogos}
            onClick={() => onSelectSection("models")}
          />
          <PlannerConfig
            settings={settings}
            usePlanner={usePlanner}
            plannerPreset={plannerPreset}
            saving={plannerSaving}
            onToggle={(enabled) => {
              void onSavePlanner({ usePlanner: enabled });
            }}
            onSelectPreset={(presetName) => {
              void onSavePlanner({ plannerModel: presetName });
            }}
          />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.persona", "Persona")}</SettingsSectionTitle>
        <SettingsGroup>
          <BootstrapFileRow fileName="AGENTS.md" />
          <BootstrapFileRow fileName="SOUL.md" />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.system", "System")}</SettingsSectionTitle>
        <SettingsGroup>
          <OverviewListRow
            icon={HardDrive}
            title={tx("settings.overview.workspace", "Workspace")}
            value={settings.runtime.workspace_path}
            caption={tx("settings.rows.configPath", "Config path")}
          />
          <SettingsRow
            icon={Activity}
            title={tx("settings.overview.heartbeat", "Heartbeat")}
            description={tx("settings.help.heartbeat", "Idle check interval in seconds (60-86400). Heartbeat only runs in gateway mode with a configured channel (Feishu/DingTalk etc.) — it lets the agent proactively notify you via IM. Desktop mode and CLI-only setups do not trigger heartbeat.")}
          >
            <NumberInput
              value={runtimeForm.heartbeatIntervalS ?? settings.runtime.heartbeat.interval_s}
              min={60}
              max={86400}
              suffix="s"
              onChange={(heartbeatIntervalS) =>
                onChangeRuntimeForm((prev) => ({ ...prev, heartbeatIntervalS }))
              }
            />
          </SettingsRow>
          <HeartbeatLlmConfig
            runtimeForm={runtimeForm}
            onChangeRuntimeForm={onChangeRuntimeForm}
            settings={settings}
          />
          <SettingsRow
            icon={Moon}
            title={tx("settings.overview.dream", "Dream")}
            description={tx("settings.help.dream", "Memory consolidation frequency. Idle trigger is enabled by default — Dream also runs automatically when you stop using it for 5 min.")}
          >
            <div className="flex items-center gap-2">
              <SegmentedControl
                value={cronToFreqValue(runtimeForm.dreamCron ?? extractDreamCron(settings.runtime.dream.schedule))}
                options={[
                  ...DREAM_FREQ_PRESETS.map((p) => ({ value: p.value, label: p.label })),
                  { value: "custom", label: tx("settings.dream.custom", "自定义") },
                ]}
                onChange={(value) => {
                  const freq = value as DreamFreqValue;
                  const cron = freqValueToCron(freq);
                  if (cron) {
                    onChangeRuntimeForm((prev) => ({ ...prev, dreamCron: cron }));
                  } else {
                    // 选"自定义"时保留当前 cron 值，显示输入框
                    onChangeRuntimeForm((prev) => ({
                      ...prev,
                      dreamCron: prev.dreamCron ?? extractDreamCron(settings.runtime.dream.schedule),
                    }));
                  }
                }}
              />
              {cronToFreqValue(runtimeForm.dreamCron ?? extractDreamCron(settings.runtime.dream.schedule)) === "custom" && (
                <Input
                  type="text"
                  value={runtimeForm.dreamCron ?? extractDreamCron(settings.runtime.dream.schedule)}
                  placeholder="0 3 * * *"
                  spellCheck={false}
                  autoComplete="off"
                  onChange={(event) =>
                    onChangeRuntimeForm((prev) => ({ ...prev, dreamCron: event.target.value }))
                  }
                  className="h-8 w-32 rounded-full text-center text-[13px]"
                />
              )}
            </div>
          </SettingsRow>
          <RestartSettingsFooter
            dirty={runtimeDirty}
            saving={runtimeSaving}
            pendingRestart={false}
            onSave={onSaveRuntime}
            onRestart={onRestart}
            isRestarting={isRestarting}
            extraActions={<DreamFilesButton />}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}
