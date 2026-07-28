// Plan & Execute 双模型配置行:Overview section 内嵌使用。
// 开关关闭 → 单模型(主模型规划+执行);开关打开 → 双模型(规划用独立 preset,执行用主模型)。

import { Compass } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ToggleSwitch } from "@/components/ui/toggle-switch";
import type { SettingsPayload } from "@/lib/types";

import { ModelPresetSelect } from "./ModelPresetSelect";
import { SettingsRow } from "./SettingsRow";

/**
 * Plan & Execute 双模型配置。
 *
 * - 开关关闭(use_planner=false):Plan & Execute 关闭,走纯 ReAct 循环。
 * - 开关打开 + 未选 preset:启用 Plan & Execute,但规划用主模型(单模型双角色)。
 * - 开关打开 + 选了 preset:真正双模型——规划用 preset,执行用主模型。
 *
 * 任何变更都需要重启 gateway 生效,由调用方处理重启提示。
 */
export function PlannerConfig({
  settings,
  usePlanner,
  plannerPreset,
  onToggle,
  onSelectPreset,
  saving,
}: {
  settings: SettingsPayload;
  usePlanner: boolean;
  /** 当前选中的 planner preset 名称;null/空 = 使用主模型。 */
  plannerPreset: string | null;
  onToggle: (enabled: boolean) => void;
  onSelectPreset: (presetName: string | null) => void;
  saving?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const presets = settings.model_presets;
  const visiblePresets = presets.filter((p) => !p.is_default);
  const currentValue = plannerPreset ?? "";
  const selectedPreset = visiblePresets.find((p) => p.name === currentValue) ?? null;

  // 描述行:展示当前状态
  let description: string;
  if (!usePlanner) {
    description = tx("settings.planner.disabledHint", "ReAct only. Toggle on to enable plan-and-execute.");
  } else if (selectedPreset) {
    description = t("settings.planner.dualModelHint", {
      defaultValue: "Planner: {{model}} · Executor: main model",
      model: selectedPreset.model,
    });
  } else {
    description = tx("settings.planner.singleModelHint", "Planner: main model · Executor: main model");
  }

  return (
    <SettingsRow
      icon={Compass}
      title={tx("settings.planner.title", "Plan & Execute")}
      description={description}
    >
      <div className="flex items-center gap-2">
        <ModelPresetSelect
          defaultSentinel={null}
          value={plannerPreset}
          disabled={!usePlanner || saving}
          presets={presets}
          settings={settings}
          label={tx("settings.planner.title", "Plan & Execute")}
          defaultOptionLabel={tx("settings.planner.useMain", "Main model")}
          onChange={onSelectPreset}
        />
        <ToggleSwitch
          checked={usePlanner}
          disabled={saving}
          onClick={() => onToggle(!usePlanner)}
          ariaLabel={tx("settings.planner.toggleAria", "Toggle plan-and-execute")}
        />
      </div>
    </SettingsRow>
  );
}
