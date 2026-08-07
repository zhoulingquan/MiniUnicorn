// Shared model-preset dropdown for heartbeat, planner, and image generation.
//
// Each caller uses a different sentinel to represent "Main model":
//   heartbeat      → ""       (empty string)
//   planner        → null
//   image gen      → "default" (literal string)
//
// This component normalizes those three sentinels behind one typed interface.
// It does NOT absorb the more complex ModelPresetPicker, which also owns
// model-configuration creation behavior.

import { Check, ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

import { ProviderPickerIcon } from "./ProviderIcon";

export interface ModelPresetSelectProps {
  /** Sentinel value representing "Main model" — one of "", null, or "default". */
  defaultSentinel: string | null;
  /** Currently selected value (preset name, or a value matching the sentinel). */
  value: string | null;
  /** Whether the trigger button is disabled. */
  disabled?: boolean;
  /** Available presets (``is_default`` entries are filtered out automatically). */
  presets: SettingsPayload["model_presets"];
  /** Settings payload — used to resolve the main model/provider for display. */
  settings: SettingsPayload;
  /** Whether to render provider icons next to each option. */
  showProviderIcon?: boolean;
  /** Accessible label for the trigger button. */
  label: string;
  /** Label for the "Main model" option row. */
  defaultOptionLabel: string;
  /** Called with the selected preset name or the ``defaultSentinel``. */
  onChange: (value: string | null) => void;
}

/** True when *value* matches *sentinel* (treating null/"" as equivalent). */
function isDefaultSelected(value: string | null, sentinel: string | null): boolean {
  if (value === sentinel) return true;
  // Both falsy (null, "", undefined) → equivalent "main model" state.
  return !value && !sentinel;
}

export function ModelPresetSelect({
  defaultSentinel,
  value,
  disabled,
  presets,
  settings,
  showProviderIcon,
  label,
  defaultOptionLabel,
  onChange,
}: ModelPresetSelectProps) {
  const visiblePresets = presets.filter((p) => !p.is_default);
  const currentValue = value ?? "";
  const selectedPreset = visiblePresets.find((p) => p.name === currentValue) ?? null;
  const defaultSelected = isDefaultSelected(value, defaultSentinel);
  const mainModel = settings.agent.model;
  const mainProvider = settings.agent.resolved_provider ?? settings.agent.provider;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          aria-label={label}
          className="h-auto w-[min(220px,42vw)] justify-between rounded-full border-input bg-background px-3 py-1.5 text-[12.5px] font-normal shadow-none hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring disabled:bg-muted/45 disabled:text-muted-foreground disabled:opacity-60"
        >
          <span className={cn("flex min-w-0 items-center", showProviderIcon ? "gap-2" : "")}>
            {showProviderIcon ? (
              <ProviderPickerIcon
                provider={selectedPreset?.provider ?? mainProvider}
                showBrandLogos
              />
            ) : null}
            <span className="min-w-0 text-left leading-tight">
              <span className="block truncate font-medium text-foreground">
                {selectedPreset ? selectedPreset.label || selectedPreset.model : defaultOptionLabel}
              </span>
              <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                {selectedPreset ? selectedPreset.model : (mainModel || "—")}
              </span>
            </span>
          </span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[20rem] w-[260px] max-w-[calc(100vw-2rem)] overflow-y-auto"
      >
        <DropdownMenuItem
          onSelect={() => onChange(defaultSentinel)}
          className={cn(
            "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-2.5 py-2 text-[13px]",
            "focus:bg-muted/85 focus:text-foreground",
            defaultSelected && "bg-muted/80 text-foreground focus:bg-muted",
          )}
        >
          <span className="min-w-0">
            <span className="block truncate font-medium">{defaultOptionLabel}</span>
            <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
              {mainModel || "—"}
            </span>
          </span>
          {defaultSelected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
        </DropdownMenuItem>
        {visiblePresets.length > 0 ? <div className="my-1 border-t border-border/55" /> : null}
        {visiblePresets.map((preset) => {
          const selected = preset.name === currentValue;
          return (
            <DropdownMenuItem
              key={preset.name}
              onSelect={() => onChange(preset.name)}
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
  );
}
