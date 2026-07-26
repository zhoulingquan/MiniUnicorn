// 共享类型、常量与 helper 函数
// 从 SettingsView.tsx 拆分而来,供主组件、各 section 与辅助组件复用。

import {
  Activity,
  Globe2,
  ImageIcon,
  LayoutGrid,
  Palette,
  ShieldCheck,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";

import { STORAGE_KEYS } from "@/lib/storage";
import type { ThemeMode } from "@/hooks/useTheme";
import type {
  NetworkSafetySettingsUpdate,
  RuntimeSettingsUpdate,
  SettingsPayload,
  WebuiDefaultAccessMode,
} from "@/lib/types";

export type SettingsSectionKey =
  | "overview"
  | "appearance"
  | "models"
  | "browser"
  | "images"
  | "advanced"
  | "apps";

export type LocalDensity = "comfortable" | "compact";
export type LocalActivityMode = "auto" | "expanded";

export interface LocalPreferences {
  density: LocalDensity;
  activityMode: LocalActivityMode;
  codeWrap: boolean;
  brandLogos: boolean;
}

export interface AgentSettingsDraft {
  model: string;
  provider: string;
  modelPreset: string;
  presetLabel: string;
  toolHintMaxLength: number;
}

export interface ModelConfigurationDraft {
  label: string;
  provider: string;
  model: string;
  // Per-preset 凭证(仅 custom provider 使用):允许每个 preset 自带独立 endpoint
  apiKey?: string;
  apiBase?: string;
  // 编辑模式时记录原始 preset 名,保存时走 updateModelConfiguration API
  editingPresetName?: string;
}

export type PendingRestartSection = "runtime" | "browser" | "images";
export type PendingRestartSections = Record<PendingRestartSection, boolean>;

export type RestartAwarePayload = {
  requires_restart?: boolean;
  surface?: SettingsPayload["surface"];
  runtime_surface?: SettingsPayload["runtime_surface"];
  runtime_capabilities?: SettingsPayload["runtime_capabilities"];
};

export type ProviderApiType = "auto" | "chat_completions" | "responses";

export type ProviderForm = {
  apiKey: string;
  apiBase: string;
  apiType: ProviderApiType;
  model: string;
};

export interface SettingsViewProps {
  themeMode: ThemeMode;
  initialSection?: SettingsSectionKey;
  showSidebar?: boolean;
  onSetThemeMode: (mode: ThemeMode) => void;
  onBackToChat: () => void;
  onModelNameChange: (modelName: string | null) => void;
  onSettingsChange?: (payload: SettingsPayload) => void;
  onLogout?: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
  hostChromeInset?: boolean;
}

export const LOCAL_PREFS_STORAGE_KEY = STORAGE_KEYS.settingsPreferences;

export const DEFAULT_LOCAL_PREFS: LocalPreferences = {
  density: "comfortable",
  activityMode: "auto",
  codeWrap: true,
  brandLogos: true,
};

export const OPENAI_API_TYPE_OPTIONS: Array<{ value: ProviderApiType; label: string }> = [
  { value: "auto", label: "Auto" },
  { value: "chat_completions", label: "Chat Completions" },
  { value: "responses", label: "Responses" },
];

export const LOCAL_UNCONFIGURED_PROVIDER_ORDER = new Map(
  ["vllm", "ollama", "lm_studio", "atomic_chat", "ovms"].map((name, index) => [
    name,
    index,
  ]),
);

export const EMPTY_PENDING_RESTART_SECTIONS: PendingRestartSections = {
  runtime: false,
  browser: false,
  images: false,
};

export const SETTINGS_NAV_ITEMS: Array<{
  key: SettingsSectionKey;
  icon: LucideIcon;
  fallback: string;
}> = [
  { key: "overview", icon: Activity, fallback: "Overview" },
  { key: "appearance", icon: Palette, fallback: "Appearance" },
  { key: "models", icon: SlidersHorizontal, fallback: "Models" },
  { key: "browser", icon: Globe2, fallback: "Search" },
  { key: "images", icon: ImageIcon, fallback: "Image" },
  { key: "advanced", icon: ShieldCheck, fallback: "Security" },
  { key: "apps", icon: LayoutGrid, fallback: "Apps" },
];

export function readLocalPreferences(): LocalPreferences {
  try {
    const raw = window.localStorage.getItem(LOCAL_PREFS_STORAGE_KEY);
    if (!raw) return DEFAULT_LOCAL_PREFS;
    const parsed = JSON.parse(raw) as Partial<LocalPreferences>;
    return {
      density: parsed.density === "compact" ? "compact" : "comfortable",
      activityMode: parsed.activityMode === "expanded" ? "expanded" : "auto",
      codeWrap: parsed.codeWrap !== false,
      brandLogos: parsed.brandLogos !== false,
    };
  } catch {
    return DEFAULT_LOCAL_PREFS;
  }
}

export function modelPresetValue(payload: SettingsPayload): string {
  return payload.agent.model_preset || "default";
}

export function defaultPreset(
  payload: SettingsPayload,
): SettingsPayload["model_presets"][number] | null {
  return payload.model_presets.find((preset) => preset.is_default) ?? null;
}

export function editableDefaultProvider(payload: SettingsPayload): string {
  const base = defaultPreset(payload);
  return base?.provider ?? payload.agent.provider ?? payload.agent.resolved_provider ?? "";
}

export function visibleWebuiDefaultAccessMode(
  mode: string | null | undefined,
): WebuiDefaultAccessMode {
  return mode === "full" ? "full" : "default";
}

/**
 * 从后端返回的 dream.schedule 字符串里提取 cron 表达式。
 *
 * 后端 `DreamConfig.describe_schedule()` 返回形如 `"cron 0 3 * * *"` 的字符串。
 * 这里去掉前缀 "cron " 得到原始表达式。找不到时回退到默认 "0 3 * * *"。
 */
export function extractDreamCron(schedule: string | undefined | null): string {
  if (!schedule) return "0 3 * * *";
  const match = schedule.match(/^cron\s+(.+)$/i);
  return match ? match[1].trim() : "0 3 * * *";
}

/** Dream 频率预设：用 SegmentedControl 展示的简化选项，避免用户直接写 cron。 */
export const DREAM_FREQ_PRESETS = [
  { value: "daily", label: "每天", cron: "0 3 * * *" },
  { value: "12h", label: "每12h", cron: "0 */12 * * *" },
  { value: "6h", label: "每6h", cron: "0 */6 * * *" },
] as const;

export type DreamFreqValue = (typeof DREAM_FREQ_PRESETS)[number]["value"] | "custom";

/**
 * 把 cron 表达式映射为频率预设值；匹配不上预设则返回 "custom"。
 */
export function cronToFreqValue(cron: string): DreamFreqValue {
  const normalized = cron.trim();
  for (const preset of DREAM_FREQ_PRESETS) {
    if (preset.cron === normalized) return preset.value;
  }
  return "custom";
}

/**
 * 把频率预设值映射为 cron 表达式；"custom" 返回 null（由调用方保留原 cron）。
 */
export function freqValueToCron(value: DreamFreqValue): string | null {
  if (value === "custom") return null;
  const preset = DREAM_FREQ_PRESETS.find((p) => p.value === value);
  return preset ? preset.cron : null;
}

export function titleForSection(section: SettingsSectionKey): string {
  return SETTINGS_NAV_ITEMS.find((item) => item.key === section)?.fallback ?? "Settings";
}

export function orderUnconfiguredProviders(
  providers: SettingsPayload["providers"],
): SettingsPayload["providers"] {
  return providers
    .map((provider, index) => ({ provider, index }))
    .sort((left, right) => {
      const rank = providerVisibilityRank(left.provider) - providerVisibilityRank(right.provider);
      return rank || left.index - right.index;
    })
    .map(({ provider }) => provider);
}

export function providerVisibilityRank(
  provider: SettingsPayload["providers"][number],
): number {
  const localRank = LOCAL_UNCONFIGURED_PROVIDER_ORDER.get(provider.name);
  if (localRank !== undefined) return localRank;
  if ((provider.api_key_required ?? true) === false) return 100;
  return 200;
}

export function modelPresetProviderKey(
  preset: SettingsPayload["model_presets"][number],
  settings: SettingsPayload,
  options: { draftProvider?: string } = {},
): string {
  const provider = options.draftProvider ?? preset.provider;
  if (provider === "auto") {
    return settings.agent.resolved_provider || settings.agent.provider || preset.provider;
  }
  return provider;
}

export type { NetworkSafetySettingsUpdate, RuntimeSettingsUpdate };
