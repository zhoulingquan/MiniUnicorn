// Model preset 选择下拉:ModelsSettings section 内嵌使用。
// 从 SettingsView.tsx 拆分而来。

import { Check, ChevronDown, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { providerDisplayLabel } from "@/lib/provider-brand";
import { cn } from "@/lib/utils";
import type { SettingsPayload } from "@/lib/types";

import { modelPresetProviderKey } from "../types";
import { ProviderPickerIcon } from "./ProviderIcon";

export function ModelPresetPicker({
  presets,
  value,
  settings,
  draftModel,
  draftProvider,
  showProviderLogos,
  onChange,
  onCreateConfiguration,
}: {
  presets: SettingsPayload["model_presets"];
  value: string;
  settings: SettingsPayload;
  draftModel: string;
  draftProvider: string;
  showProviderLogos: boolean;
  onChange: (preset: string) => void;
  onCreateConfiguration: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  // default 虚拟条目代表"主配置"(agents.defaults)。当用户选中命名预设后,
  // 主配置不再使用,default 卡片 model 为空,显示冗余,过滤掉。
  // 仅当 default 自身是 active(主配置正在使用)时才保留。
  const visiblePresets = presets.filter((p) => !p.is_default || p.active);
  const selectedPreset = visiblePresets.find((preset) => preset.name === value) ?? null;
  const isDefaultFallback = value === "default" || !value;
  const defaultLabel = tx("settings.models.defaultPreset", "Default configuration");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={!visiblePresets.length && isDefaultFallback}>
        <Button
          type="button"
          variant="outline"
          disabled={!visiblePresets.length && isDefaultFallback}
          className={cn(
            "h-10 w-[min(260px,60vw)] justify-between rounded-full border-input bg-background px-3 text-[13px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          {selectedPreset ? (
            <ModelPresetOptionContent
              preset={selectedPreset}
              settings={settings}
              draftModel={draftModel}
              draftProvider={draftProvider}
              showProviderLogos={showProviderLogos}
              compact
            />
          ) : isDefaultFallback ? (
            <span className="flex min-w-0 items-center gap-2.5">
              <ProviderPickerIcon provider="auto" showBrandLogos={showProviderLogos} />
              <span className="min-w-0 text-left leading-tight">
                <span className="block truncate font-medium text-foreground">
                  {draftModel || defaultLabel}
                </span>
                <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                  {defaultLabel}
                </span>
              </span>
            </span>
          ) : (
            <span className="truncate text-muted-foreground">
              {tx("settings.models.selectModel", "Select model")}
            </span>
          )}
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[20rem] w-[280px] max-w-[calc(100vw-2rem)] overflow-y-auto"
      >
        {visiblePresets.map((preset) => {
          const selected = preset.name === value;
          return (
            <DropdownMenuItem
              key={preset.name}
              onSelect={() => onChange(preset.name)}
              className={cn(
                "flex cursor-default items-center justify-between gap-3 rounded-[12px] px-2.5 py-2 text-[13px]",
                "focus:bg-muted/85 focus:text-foreground",
                selected && "bg-muted/80 text-foreground focus:bg-muted",
              )}
            >
              <ModelPresetOptionContent
                preset={preset}
                settings={settings}
                draftModel={draftModel}
                draftProvider={draftProvider}
                showProviderLogos={showProviderLogos}
              />
              {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
          );
        })}
        <div className="mt-1 border-t border-border/55 pt-1">
          <DropdownMenuItem
            onSelect={onCreateConfiguration}
            className={cn(
              "flex cursor-default items-center gap-2 rounded-[12px] px-2.5 py-2 text-[13px] font-medium",
              "text-foreground focus:bg-muted/85 focus:text-foreground",
            )}
          >
            <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground">
              <Plus className="h-3.5 w-3.5" aria-hidden />
            </span>
            <span>{tx("settings.models.addConfiguration", "Add configuration")}</span>
          </DropdownMenuItem>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ModelPresetOptionContent({
  preset,
  settings,
  draftModel,
  draftProvider,
  showProviderLogos,
  compact = false,
}: {
  preset: SettingsPayload["model_presets"][number];
  settings: SettingsPayload;
  draftModel: string;
  draftProvider: string;
  showProviderLogos: boolean;
  compact?: boolean;
}) {
  const provider = modelPresetProviderKey(preset, settings, {
    draftProvider: preset.is_default ? draftProvider : undefined,
  });
  // compact(触发器):default preset 用 draftModel 显示当前选中模型;
  // 非 compact(下拉列表项):default preset 用 preset.label("Default"),
  // 避免 draftModel 恰好等于某具名 preset 的 model 时两个选项显示相同文案。
  const model = preset.is_default ? (compact ? draftModel : "") : preset.model;
  const providerName = providerDisplayLabel(settings.providers, provider);
  return (
    <span className="flex min-w-0 items-center gap-2.5">
      <ProviderPickerIcon provider={provider} showBrandLogos={showProviderLogos} />
      <span className="min-w-0 text-left leading-tight">
        <span className="block truncate font-medium text-foreground">{model || preset.label}</span>
        <span
          className={cn(
            "mt-0.5 block truncate text-muted-foreground",
            compact ? "text-[11.5px]" : "text-[12px]",
          )}
        >
          {providerName}
          {preset.label ? ` · ${preset.label}` : ""}
        </span>
      </span>
    </span>
  );
}
