// 新建 model 配置 Dialog:ModelsSettings section 内的"添加配置"入口。
// 从 SettingsView.tsx 拆分而来。

import type { Dispatch, SetStateAction } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import type { ModelConfigurationDraft } from "../types";
import { ClearableInput } from "../components/SettingsRow";
import { ProviderPicker } from "../components/ProviderPicker";

export function NewModelConfigurationDialog({
  open,
  draft,
  providers,
  saving,
  learning = false,
  showProviderLogos,
  onOpenChange,
  onChangeDraft,
  onSave,
}: {
  open: boolean;
  draft: ModelConfigurationDraft;
  providers: Array<{ name: string; label: string }>;
  saving: boolean;
  /** 保存后是否正在查询模型上下文窗口(轮询中)。 */
  learning?: boolean;
  showProviderLogos: boolean;
  onOpenChange: (open: boolean) => void;
  onChangeDraft: Dispatch<SetStateAction<ModelConfigurationDraft>>;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const isCustom = draft.provider === "custom";
  // custom provider 必须自带 api_key+api_base(单例未配置);其他 provider 可选自带凭证
  const canSave = Boolean(
    draft.label.trim() && draft.provider.trim() && draft.model.trim()
  ) && (!isCustom || (draft.apiKey?.trim() && draft.apiBase?.trim()));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[520px] p-0">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onSave();
          }}
        >
          <DialogHeader className="border-b border-border/45 px-5 py-4 text-left">
            <DialogTitle className="text-[18px] font-semibold tracking-[-0.01em]">
              {draft.editingPresetName
                ? tx("settings.models.editConfiguration", "Edit model configuration")
                : tx("settings.models.newConfiguration", "New model configuration")}
            </DialogTitle>
            <DialogDescription className="text-[12.5px] leading-5">
              {draft.editingPresetName
                ? tx("settings.models.editConfigurationHelp", "Update the label, model, or credentials for this configuration.")
                : tx("settings.models.newConfigurationHelp", "Save a provider and model as a one-click option.")}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 px-5 py-5">
            <label className="block">
              <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                {tx("settings.models.configurationName", "Name")}
              </span>
              <ClearableInput
                autoFocus
                value={draft.label}
                placeholder={tx("settings.models.configurationNamePlaceholder", "Fast writing")}
                onChange={(event) =>
                  onChangeDraft((prev) => ({ ...prev, label: event.target.value }))
                }
                onClear={() => onChangeDraft((prev) => ({ ...prev, label: "" }))}
                className="h-10 rounded-full px-4 text-[14px]"
              />
            </label>

            <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
              <label className="block">
                <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                  {tx("settings.rows.model", "Model")}
                </span>
                <ClearableInput
                  value={draft.model}
                  placeholder="openai/gpt-4.1"
                  onChange={(event) =>
                    onChangeDraft((prev) => ({ ...prev, model: event.target.value }))
                  }
                  onClear={() => onChangeDraft((prev) => ({ ...prev, model: "" }))}
                  className="h-10 rounded-full px-4 text-[14px]"
                />
              </label>
              <div className="block">
                <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                  {tx("settings.rows.provider", "Provider")}
                </span>
                <ProviderPicker
                  providers={providers}
                  value={draft.provider}
                  emptyLabel={tx("settings.byok.noConfiguredProviders", "No configured providers")}
                  showProviderLogos={showProviderLogos}
                  onChange={(provider) =>
                    onChangeDraft((prev) => ({ ...prev, provider }))
                  }
                />
              </div>
            </div>

            {isCustom ? (
              <div className="grid gap-4 sm:grid-cols-2">
                <label className="block">
                  <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                    {tx("settings.byok.apiKey", "API Key")}
                  </span>
                  <ClearableInput
                    type="password"
                    value={draft.apiKey ?? ""}
                    placeholder="sk-..."
                    onChange={(event) =>
                      onChangeDraft((prev) => ({ ...prev, apiKey: event.target.value }))
                    }
                    onClear={() => onChangeDraft((prev) => ({ ...prev, apiKey: "" }))}
                    className="h-10 rounded-full px-4 text-[14px]"
                  />
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                    {tx("settings.byok.apiBase", "API Base")}
                  </span>
                  <ClearableInput
                    value={draft.apiBase ?? ""}
                    placeholder="https://api.example.com/v1"
                    onChange={(event) =>
                      onChangeDraft((prev) => ({ ...prev, apiBase: event.target.value }))
                    }
                    onClear={() => onChangeDraft((prev) => ({ ...prev, apiBase: "" }))}
                    className="h-10 rounded-full px-4 text-[14px]"
                  />
                </label>
              </div>
            ) : null}
          </div>

          <DialogFooter className="border-t border-border/45 px-5 py-4 sm:space-x-2">
            <Button
              type="button"
              variant="outline"
              disabled={saving || learning}
              onClick={() => onOpenChange(false)}
            >
              {tx("settings.actions.cancel", "Cancel")}
            </Button>
            <Button
              type="submit"
              disabled={!canSave || saving || learning || providers.length === 0}
            >
              {saving || learning ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : null}
              {saving
                ? tx("settings.actions.saving", "Saving...")
                : learning
                  ? tx("settings.actions.queryingContext", "Querying context...")
                  : tx("settings.actions.save", "Save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
