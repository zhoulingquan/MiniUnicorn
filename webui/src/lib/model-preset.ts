// Pure helpers for resolving the active model preset and provider from a
// SettingsPayload. Extracted from ThreadShell so that App.tsx (the
// authentication/bootstrap shell) can import these without pulling the
// entire chat shell into the initial bundle chunk.
//
// This module must stay dependency-light: it only imports from provider-brand
// and types, both of which are small leaf modules.

import { inferProviderFromModelName } from "@/lib/provider-brand";
import type { SettingsPayload } from "@/lib/types";

export function activeModelPreset(
  settings: SettingsPayload | null,
): SettingsPayload["model_presets"][number] | null {
  if (!settings) return null;
  const configured = settings.agent.model_preset || "default";
  return (
    settings.model_presets.find((preset) => preset.name === configured)
    ?? settings.model_presets.find((preset) => preset.active)
    ?? null
  );
}

export function resolvedModelProvider(
  settings: SettingsPayload | null,
  modelName: string | null,
): string | null {
  const preset = activeModelPreset(settings);
  const rawProvider = preset?.provider || settings?.agent.provider || null;
  if (rawProvider === "auto") {
    return settings?.agent.resolved_provider || inferProviderFromModelName(modelName) || null;
  }
  // custom 命名 preset:返回虚拟 row name(custom__<preset_name>),
  // 让 header 下拉和设置页匹配到对应的独立虚拟卡片
  if (rawProvider === "custom" && preset && !preset.is_default) {
    return `custom__${preset.name}`;
  }
  return rawProvider || inferProviderFromModelName(modelName);
}
