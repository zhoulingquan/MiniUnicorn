// 已配置 provider 卡片内的"添加模型"内联表单:
// 替代原先的 NewModelConfigurationDialog 弹窗,样式与卡片展开后的
// API Key / API Base / Model ID 字段一致(ClearableInput + rounded-full)。

import { useEffect, useRef } from "react";
import { Loader2, Search } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { ModelConfigurationDraft } from "../types";
import { ClearableInput } from "./SettingsRow";

export interface InlineAddModelFormProps {
  /** 当前 draft(由父组件持有,通过 onChangeDraft 更新)。 */
  draft: ModelConfigurationDraft;
  /** 该 provider 已拉取的可选模型列表(点击 Fetch models 后填充)。 */
  fetchedModels: string[];
  /** 是否正在拉取模型列表。 */
  modelsLoading: boolean;
  /** 是否正在保存。 */
  saving: boolean;
  /** 保存后是否正在查询模型上下文窗口(轮询中)。 */
  learning?: boolean;
  /** 是否为 custom provider(custom 必须填 API Key + API Base)。 */
  isCustom: boolean;
  /** 渲染变体:inline=卡片内折叠表单(带虚线边框),dialog=Dialog 内无边框。 */
  variant?: "inline" | "dialog";
  /** draft 字段变更回调。 */
  onChangeDraft: (next: ModelConfigurationDraft) => void;
  /** 点击 Fetch models 按钮。 */
  onFetchModels: () => void;
  /** 点击保存。 */
  onSave: () => void;
  /** 点击取消。 */
  onCancel: () => void;
}

export function InlineAddModelForm({
  draft,
  fetchedModels,
  modelsLoading,
  saving,
  learning = false,
  isCustom,
  variant = "inline",
  onChangeDraft,
  onFetchModels,
  onSave,
  onCancel,
}: InlineAddModelFormProps) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  // ClearableInput does not forward a ref, so we focus the Model ID input
  // through its wrapping label on mount. This replaces the previous raw
  // `autoFocus` prop (which harms screen-reader / keyboard UX).
  const modelIdLabelRef = useRef<HTMLLabelElement>(null);

  useEffect(() => {
    modelIdLabelRef.current?.querySelector("input")?.focus();
  }, []);

  const canSave =
    Boolean(draft.provider.trim() && draft.model.trim())
    && (!isCustom || (draft.apiKey?.trim() && draft.apiBase?.trim()));

  return (
    <div className={variant === "dialog" ? "space-y-3" : "mt-3 space-y-3 rounded-lg border border-dashed border-border/60 bg-background/60 px-3 py-3"}>
      {/* Model ID + Fetch models 按钮(与卡片内 Model ID 字段一致) */}
      {/* dialog 变体(custom 自定义配置入口):凭证在前,Model ID 在后 */}
      {/* inline 变体(卡片内添加模型):Model ID 在前,凭证在后 */}
      {(() => {
        const modelIdField = (
          <label ref={modelIdLabelRef} className="block space-y-1.5">
            <span className="text-[12px] font-medium text-muted-foreground">
              {tx("settings.byok.modelId", "Model ID")}
            </span>
            <div className="flex gap-2">
              <ClearableInput
                value={draft.model}
                onChange={(event) => onChangeDraft({ ...draft, model: event.target.value })}
                onClear={() => onChangeDraft({ ...draft, model: "" })}
                placeholder={tx("settings.byok.modelIdPlaceholder", "e.g. gpt-4o, deepseek-chat")}
                className="h-9 flex-1 rounded-full text-[13px]"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={onFetchModels}
                disabled={modelsLoading}
                className="h-9 shrink-0 rounded-full px-3 text-[12px]"
              >
                {modelsLoading ? (
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" aria-hidden />
                ) : (
                  <Search className="mr-1 h-3 w-3" aria-hidden />
                )}
                {modelsLoading
                  ? tx("settings.byok.fetchingModels", "Fetching...")
                  : tx("settings.byok.fetchModels", "Fetch models")}
              </Button>
            </div>
            <span className="block text-[11px] text-muted-foreground/80">
              {tx("settings.byok.modelIdHelp", "Set as active model when saving.")}
            </span>
            {fetchedModels.length > 0 ? (
              <div className="mt-1 max-h-[160px] overflow-y-auto rounded-lg border border-border/45 bg-background/60">
                {fetchedModels.map((modelId) => (
                  <button
                    key={modelId}
                    type="button"
                    onClick={() => onChangeDraft({ ...draft, model: modelId })}
                    className={cn(
                      "block w-full truncate px-3 py-1.5 text-left text-[12px] transition-colors hover:bg-muted/50",
                      draft.model === modelId
                        ? "font-semibold text-foreground"
                        : "text-muted-foreground",
                    )}
                    title={modelId}
                  >
                    {modelId}
                  </button>
                ))}
              </div>
            ) : null}
          </label>
        );
        const credentialFields = isCustom ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">
                {tx("settings.byok.apiKey", "API Key")}
              </span>
              <ClearableInput
                type="password"
                value={draft.apiKey ?? ""}
                placeholder="sk-..."
                onChange={(event) => onChangeDraft({ ...draft, apiKey: event.target.value })}
                onClear={() => onChangeDraft({ ...draft, apiKey: "" })}
                className="h-9 rounded-full text-[13px]"
              />
            </label>
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">
                {tx("settings.byok.apiBase", "API Base")}
              </span>
              <ClearableInput
                value={draft.apiBase ?? ""}
                placeholder="https://api.example.com/v1"
                onChange={(event) => onChangeDraft({ ...draft, apiBase: event.target.value })}
                onClear={() => onChangeDraft({ ...draft, apiBase: "" })}
                className="h-9 rounded-full text-[13px]"
              />
            </label>
          </div>
        ) : null;
        // dialog:凭证在前 → Model ID 在后;inline:Model ID 在前 → 凭证在后
        return variant === "dialog" ? (
          <>
            {credentialFields}
            {modelIdField}
          </>
        ) : (
          <>
            {modelIdField}
            {credentialFields}
          </>
        );
      })()}

      {/* 保存 / 取消按钮(右对齐,与卡片底部按钮风格一致) */}
      <div className="flex items-center justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="rounded-full"
          disabled={saving || learning}
          onClick={onCancel}
        >
          {tx("settings.actions.cancel", "Cancel")}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="rounded-full"
          disabled={!canSave || saving || learning}
          onClick={onSave}
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
      </div>
    </div>
  );
}
