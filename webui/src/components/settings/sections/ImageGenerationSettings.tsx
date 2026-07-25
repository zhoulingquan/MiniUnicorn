// Image generation section: generate_image 工具配置 (启用开关 + 模型预设 + 默认参数)
//
// 与 PlannerConfig (Plan & Execute) 完全同模式:
// - 直接复用 settings.model_presets 作为下拉选项 (即"模型设置已配置区域"的卡片)
// - 选 "主模型" = 走主对话 model_preset; 选其他 = 走对应 preset 的凭证
// - 不维护独立 available_presets 副本,保证下拉内容与模型设置卡片一致

import { useRef, type Dispatch, type SetStateAction } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronDown, Folder, Image as ImageIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { ImageGenerationSettingsUpdate, SettingsPayload } from "@/lib/types";

import { SegmentedControl, ToggleButton } from "../components/SegmentedControl";
import { RestartSettingsFooter } from "../components/RestartSettingsFooter";
import {
  ClearableInput,
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "../components/SettingsRow";
import { ProviderPickerIcon } from "../components/ProviderIcon";
import { cn } from "@/lib/utils";

// 预设图片尺寸简写 (与后端 default_image_size 透传逻辑对齐, 这些字符串原样发给 provider)
const PRESET_SIZES = ["1K", "2K", "4K"] as const;
const SIZE_OPTIONS = [
  { value: "1K", label: "1K" },
  { value: "2K", label: "2K" },
  { value: "4K", label: "4K" },
  { value: "custom", label: "自定义" },
];

export function ImageGenerationSettings({
  form,
  dirty,
  saving,
  requiresRestartPending,
  settings,
  onChangeForm,
  onSave,
  onRestart,
  isRestarting,
}: {
  form: ImageGenerationSettingsUpdate;
  dirty: boolean;
  saving: boolean;
  requiresRestartPending: boolean;
  settings: SettingsPayload;
  onChangeForm: Dispatch<SetStateAction<ImageGenerationSettingsUpdate>>;
  onSave: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  // 隐藏的文件夹选择 input (webkitdirectory 触发系统文件夹选择器)
  // 浏览器安全限制: 只能拿到所选文件夹的名字 (如 "my_images"), 无法拿到绝对路径
  // 该名字会作为 media 根目录下的子目录名使用
  const dirInputRef = useRef<HTMLInputElement>(null);

  const ig = settings.image_generation;
  // 直接复用 settings.model_presets (与"模型设置已配置区域"卡片同源,同 PlannerConfig 模式)
  const presets = settings.model_presets;
  // 主模型选项 (default): 用 agents.defaults 的 model
  const mainModel = settings.agent.model;
  const mainProvider = settings.agent.resolved_provider ?? settings.agent.provider;
  const currentValue = form.preset ?? "default";
  const selectedPreset = presets.find((p) => p.name === currentValue) ?? null;

  const aspectRatioOptions = (ig?.aspect_ratio_options ?? [
    "1:1", "16:9", "9:16", "4:3", "3:4",
  ]).map((value) => ({ value, label: value }));

  // 当前尺寸模式: 预设值 (1K/2K/4K) 或 "custom" (任意自定义分辨率)
  const sizeMode: string = (PRESET_SIZES as readonly string[]).includes(form.defaultImageSize)
    ? form.defaultImageSize
    : "custom";
  const sizeOptions = SIZE_OPTIONS;

  // 完整保存路径: 优先用后端下发的 save_dir_full (绝对路径)
  // 若本地有未保存的 saveDir 改动, 用 save_dir_full 的父目录 + 当前 saveDir 拼接
  const savedFull = ig?.save_dir_full ?? "";
  const displaySavePath = (() => {
    const dir = form.saveDir || "generated";
    if (!savedFull) return dir;
    if (form.saveDir === ig?.save_dir) return savedFull;
    // 本地改动: 替换最后一段
    const parent = savedFull.replace(/[^/]+$/, "");
    return parent + dir;
  })();

  const setField = <K extends keyof ImageGenerationSettingsUpdate>(
    key: K,
    value: ImageGenerationSettingsUpdate[K],
  ) => {
    onChangeForm((prev) => ({ ...prev, [key]: value }));
  };

  // 构造 Provider 选择器的描述行
  let providerDescription: string;
  if (!form.enabled) {
    providerDescription = tx(
      "settings.help.imageGenProviderDisabled",
      "Image generation is off. Toggle enable above after picking a preset.",
    );
  } else if (selectedPreset) {
    providerDescription = t("settings.help.imageGenProviderSelected", {
      defaultValue: "Preset: {{name}} · Model: {{model}}",
      name: selectedPreset.label || selectedPreset.name,
      model: selectedPreset.model,
    });
  } else {
    providerDescription = t("settings.help.imageGenProviderMain", {
      defaultValue: "Using main model: {{model}}",
      model: mainModel || "—",
    });
  }

  return (
    <div className="space-y-7">
      {/* 基础设置 */}
      <section>
        <SettingsSectionTitle>
          {tx("settings.sections.imageGeneration", "Image Generation")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.imageGenEnable", "Enable generate_image")}
            description={tx(
              "settings.help.imageGenEnable",
              "Expose generate_image in chats when a configured model preset is available. Toggle this on after picking a preset below.",
            )}
          >
            <ToggleButton
              checked={form.enabled}
              onChange={(enabled) => setField("enabled", enabled)}
              ariaLabel={tx("settings.rows.imageGenEnable", "Enable generate_image")}
              label={form.enabled ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>

          <SettingsRow
            icon={ImageIcon}
            title={tx("settings.rows.imageGenPreset", "Model Preset")}
            description={providerDescription}
          >
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  variant="outline"
                  disabled={saving}
                  className="h-8 w-[min(220px,42vw)] justify-between rounded-full border-input bg-background px-3 text-[12.5px] font-normal shadow-none hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <ProviderPickerIcon
                      provider={selectedPreset?.provider ?? mainProvider}
                      showBrandLogos
                    />
                    <span className="min-w-0 text-left leading-tight">
                      <span className="block truncate font-medium text-foreground">
                        {selectedPreset
                          ? selectedPreset.label || selectedPreset.name
                          : tx("settings.values.imageGenMain", "Main model")}
                      </span>
                      <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                        {selectedPreset ? selectedPreset.model : mainModel || "—"}
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
                {/* 主模型 (default) 选项 */}
                <DropdownMenuItem
                  onSelect={() => setField("preset", "default")}
                  className={cn(
                    "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-2.5 py-2 text-[13px]",
                    "focus:bg-muted/85 focus:text-foreground",
                    (!currentValue || currentValue === "default") &&
                      "bg-muted/80 text-foreground focus:bg-muted",
                  )}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium">
                      {tx("settings.values.imageGenMain", "Main model")}
                    </span>
                    <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                      {mainModel || "—"}
                    </span>
                  </span>
                  {(!currentValue || currentValue === "default") ? (
                    <Check className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  ) : null}
                </DropdownMenuItem>
                {presets.length > 0 ? <div className="my-1 border-t border-border/55" /> : null}
                {presets.map((preset) => {
                  const selected = preset.name === currentValue;
                  return (
                    <DropdownMenuItem
                      key={preset.name}
                      onSelect={() => setField("preset", preset.name)}
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
          </SettingsRow>
        </SettingsGroup>
      </section>

      {/* 默认参数 */}
      <section>
        <SettingsSectionTitle>
          {tx("settings.sections.imageGenDefaults", "Defaults")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.imageGenAspectRatio", "Default Aspect Ratio")}
            description={tx(
              "settings.help.imageGenAspectRatio",
              "Used when the tool call omits aspect_ratio. Maps to provider-specific sizes.",
            )}
          >
            <SegmentedControl
              value={form.defaultAspectRatio}
              options={aspectRatioOptions}
              onChange={(value) => setField("defaultAspectRatio", value)}
            />
          </SettingsRow>

          <SettingsRow
            title={tx("settings.rows.imageGenImageSize", "Default Image Size")}
            description={tx(
              "settings.help.imageGenImageSize",
              "Size hint like 1K / 2K / 4K or explicit dimensions (1024x1024).",
            )}
          >
            <div className="flex flex-col items-end gap-2">
              <SegmentedControl
                value={sizeMode}
                options={sizeOptions}
                onChange={(value) => {
                  if (value === "custom") {
                    // 切到自定义时,若当前是预设值则清空,否则保留用户已输入的自定义值
                    if ((PRESET_SIZES as readonly string[]).includes(form.defaultImageSize)) {
                      setField("defaultImageSize", "");
                    }
                  } else {
                    setField("defaultImageSize", value);
                  }
                }}
              />
              {sizeMode === "custom" ? (
                <ClearableInput
                  value={form.defaultImageSize}
                  onChange={(e) => setField("defaultImageSize", e.target.value)}
                  onClear={() => setField("defaultImageSize", "")}
                  placeholder="1024x1024"
                  className="h-8 w-[120px] rounded-full text-[12px]"
                />
              ) : null}
            </div>
          </SettingsRow>

          <SettingsRow
            title={tx("settings.rows.imageGenMaxPerTurn", "Max Images Per Turn")}
            description={tx(
              "settings.help.imageGenMaxPerTurn",
              "Cap on count parameter to prevent runaway API bills. Range: 1-8.",
            )}
          >
            <input
              type="number"
              min={1}
              max={8}
              value={form.maxImagesPerTurn}
              onChange={(e) => {
                const v = parseInt(e.target.value, 10);
                if (!Number.isNaN(v)) setField("maxImagesPerTurn", Math.max(1, Math.min(8, v)));
              }}
              className="h-8 w-[80px] rounded-full border border-border/60 bg-background px-3 text-[12px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </SettingsRow>

          <SettingsRow
            title={tx("settings.rows.imageGenSaveDir", "Save Directory")}
            description={`${tx("settings.values.imageGenSaveDirPath", "保存路径")}: ${displaySavePath}`}
          >
            <Button
              type="button"
              variant="outline"
              onClick={() => dirInputRef.current?.click()}
              className="h-8 gap-1.5 rounded-full px-3 text-[12.5px] font-normal shadow-none hover:bg-accent/55"
            >
              <Folder className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
              {tx("settings.actions.imageGenPickFolder", "选择文件夹")}
            </Button>
            {/* 隐藏的文件夹选择器: webkitdirectory 触发系统文件夹选择弹窗 */}
            <input
              ref={dirInputRef}
              type="file"
              {...({ webkitdirectory: "", directory: "" } as React.InputHTMLAttributes<HTMLInputElement>)}
              className="hidden"
              onChange={(event) => {
                const files = event.target.files;
                if (files && files.length > 0) {
                  // webkitRelativePath 格式: "文件夹名/文件1.txt"
                  const folderName = (files[0] as File & { webkitRelativePath?: string })
                    .webkitRelativePath?.split("/")[0];
                  if (folderName) setField("saveDir", folderName);
                }
                // 重置 input 以便重复选择同一文件夹
                event.target.value = "";
              }}
            />
          </SettingsRow>

          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            onSave={onSave}
            onRestart={onRestart}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>

      <p className="max-w-3xl px-1 text-sm leading-6 text-muted-foreground">
        {tx(
          "settings.help.imageGenWorkflow",
          "After enabling, just ask in chat to generate or edit images. Pass prior image paths as reference_images for iterative edits. Model preset is picked above; credentials are reused from your model configuration.",
        )}
      </p>
    </div>
  );
}
