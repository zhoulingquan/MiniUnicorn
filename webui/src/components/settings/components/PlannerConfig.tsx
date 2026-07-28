// Plan & Execute 双模型配置行:Overview section 内嵌使用。
// 开关关闭 → 单模型(主模型规划+执行);开关打开 → 双模型(规划用独立 preset,执行用主模型)。

import { Check, ChevronDown, Compass } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ToggleSwitch } from "@/components/ui/toggle-switch";
import { cn } from "@/lib/utils";
import type { SettingsPayload } from "@/lib/types";

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
  // 过滤掉虚拟 default 条目:本组件已硬编码"主模型"选项,default 冗余。
  const visiblePresets = presets.filter((p) => !p.is_default);
  const currentValue = plannerPreset ?? "";
  const selectedPreset = visiblePresets.find((p) => p.name === currentValue) ?? null;
  const defaultOptionLabel = tx("settings.planner.useMain", "Main model");

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
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="outline"
              disabled={!usePlanner || saving}
              className="h-auto w-[min(220px,42vw)] justify-between rounded-full border-input bg-background px-3 py-1.5 text-[12.5px] font-normal shadow-none hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring disabled:bg-muted/45 disabled:text-muted-foreground disabled:opacity-60"
            >
              <span className="min-w-0 text-left leading-tight">
                <span className="block truncate font-medium text-foreground">
                  {selectedPreset ? selectedPreset.label || selectedPreset.model : defaultOptionLabel}
                </span>
                <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                  {selectedPreset ? selectedPreset.model : (settings.agent.model || "—")}
                </span>
              </span>
              <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="max-h-[20rem] w-[220px] max-w-[calc(100vw-2rem)] overflow-y-auto"
          >
            <DropdownMenuItem
              onSelect={() => onSelectPreset(null)}
              className={cn(
                "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-2.5 py-2 text-[13px]",
                "focus:bg-muted/85 focus:text-foreground",
                !currentValue && "bg-muted/80 text-foreground focus:bg-muted",
              )}
            >
              <span className="min-w-0">
                <span className="block truncate font-medium">{defaultOptionLabel}</span>
                <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                  {settings.agent.model || "—"}
                </span>
              </span>
              {!currentValue ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
            {visiblePresets.length > 0 ? <div className="my-1 border-t border-border/55" /> : null}
            {visiblePresets.map((preset) => {
              const selected = preset.name === currentValue;
              return (
                <DropdownMenuItem
                  key={preset.name}
                  onSelect={() => onSelectPreset(preset.name)}
                  className={cn(
                    "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-2.5 py-2 text-[13px]",
                    "focus:bg-muted/85 focus:text-foreground",
                    selected && "bg-muted/80 text-foreground focus:bg-muted",
                  )}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{preset.label || preset.name}</span>
                    <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                      {preset.model}
                    </span>
                  </span>
                  {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
                </DropdownMenuItem>
              );
            })}
          </DropdownMenuContent>
        </DropdownMenu>
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
