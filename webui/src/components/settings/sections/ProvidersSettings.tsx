// Providers section:已配置/未配置两个分组,每个 provider 卡片支持
// API Key / API Base / Model 编辑、OAuth 登录登出、删除、模型列表抓取。
// 从 SettingsView.tsx 拆分而来。

import { useMemo, useState } from "react";
import {
  ChevronDown,
  Eye,
  EyeOff,
  Loader2,
  Pencil,
  Plus,
  Trash2,
} from "lucide-react";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import type { SettingsPayload } from "@/lib/types";

import {
  OPENAI_API_TYPE_OPTIONS,
  orderUnconfiguredProviders,
  type ProviderForm,
  type ModelConfigurationDraft,
} from "../types";
import { ClearableInput, StatusPill } from "../components/SettingsRow";
import { InlineAddModelForm } from "../components/InlineAddModelForm";
import { ProviderIcon } from "../components/ProviderIcon";
import { ProviderSection } from "../components/ProviderSection";
import { ProviderPresetList } from "../components/ProviderPresetList";

export function ProvidersSettings({
  settings,
  expandedProvider,
  providerForms,
  visibleProviderKeys,
  editingProviderKeys,
  providerSaving,
  providerSaved,
  learningProvider,
  timeoutProvider,
  showBrandLogos,
  onToggleProvider,
  onToggleProviderKey,
  onToggleProviderKeyEditing,
  onChangeProviderForm,
  onSaveProvider,
  onProviderOAuthLogin,
  onProviderOAuthLogout,
  onRequestDeleteProvider,
  onAddModelToProvider,
  onActivatePreset,
  onDeletePreset,
  inlineAddModelProvider,
  inlineAddModelDraft,
  inlineAddModelModels,
  inlineAddModelModelsLoading,
  inlineAddModelSaving,
  onChangeInlineAddModelDraft,
  onCancelInlineAddModel,
  onSaveInlineAddModel,
  onFetchInlineAddModelModels,
  customConfigOpen,
  customConfigDraft,
  customConfigSaving,
  customConfigModels,
  customConfigModelsLoading,
  onOpenCustomConfig,
  onChangeCustomConfigDraft,
  onCancelCustomConfig,
  onSaveCustomConfig,
  onFetchCustomConfigModels,
  onDeleteAllProviders,
  deletingAllProviders,
}: {
  settings: SettingsPayload;
  expandedProvider: string | null;
  providerForms: Record<string, ProviderForm>;
  visibleProviderKeys: Record<string, boolean>;
  editingProviderKeys: Record<string, boolean>;
  providerSaving: string | null;
  providerSaved: Record<string, boolean>;
  learningProvider: string | null;
  timeoutProvider: string | null;
  showBrandLogos: boolean;
  onToggleProvider: (provider: string) => void;
  onToggleProviderKey: (provider: string) => void;
  onToggleProviderKeyEditing: (provider: string) => void;
  onChangeProviderForm: (provider: string, value: Partial<ProviderForm>) => void;
  onSaveProvider: (provider: string) => void;
  onProviderOAuthLogin: (provider: string) => void;
  onProviderOAuthLogout: (provider: string) => void;
  onRequestDeleteProvider: (provider: string) => void;
  onAddModelToProvider: (providerName: string) => void;
  onActivatePreset: (presetName: string) => void;
  onDeletePreset: (presetName: string) => void;
  /** 当前正在 inline 添加模型的 provider 名(null 表示无)。 */
  inlineAddModelProvider: string | null;
  /** inline 添加模型表单的 draft。 */
  inlineAddModelDraft: ModelConfigurationDraft;
  /** inline 表单拉取的模型列表。 */
  inlineAddModelModels: string[];
  /** inline 表单是否正在拉取模型列表。 */
  inlineAddModelModelsLoading: boolean;
  /** inline 表单是否正在保存。 */
  inlineAddModelSaving: boolean;
  /** inline 表单 draft 变更回调。 */
  onChangeInlineAddModelDraft: (draft: ModelConfigurationDraft) => void;
  /** 取消 inline 添加模型(收起表单)。 */
  onCancelInlineAddModel: () => void;
  /** 保存 inline 添加模型。 */
  onSaveInlineAddModel: () => void;
  /** 为 inline 表单拉取模型列表。 */
  onFetchInlineAddModelModels: () => void;
  /** custom 自定义配置 Dialog 是否打开。 */
  customConfigOpen: boolean;
  /** custom 配置 Dialog 的 draft。 */
  customConfigDraft: ModelConfigurationDraft;
  /** custom 配置 Dialog 是否正在保存。 */
  customConfigSaving: boolean;
  /** custom 配置 Dialog 拉取的模型列表。 */
  customConfigModels: string[];
  /** custom 配置 Dialog 是否正在拉取模型列表。 */
  customConfigModelsLoading: boolean;
  /** 打开 custom 自定义配置 Dialog。 */
  onOpenCustomConfig: () => void;
  /** custom 配置 Dialog draft 变更回调。 */
  onChangeCustomConfigDraft: (draft: ModelConfigurationDraft) => void;
  /** 取消 custom 配置(关闭 Dialog)。 */
  onCancelCustomConfig: () => void;
  /** 保存 custom 配置(触发上下文查询,成功后在已配置区域生成新卡片)。 */
  onSaveCustomConfig: () => void;
  /** 为 custom 配置 Dialog 拉取模型列表。 */
  onFetchCustomConfigModels: () => void;
  /** 一键清除所有 provider 配置。 */
  onDeleteAllProviders: () => Promise<void>;
  /** 是否正在清除所有 provider 配置。 */
  deletingAllProviders: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [deleteAllConfirmOpen, setDeleteAllConfirmOpen] = useState(false);
  // custom provider 始终保留在未配置区域作为"添加 provider"入口(configured=false)。
  // 有 preset 时同时在已配置区域显示(展示 preset 列表)。
  // 两个区域的 custom 卡片用 entryKey(provider.name + configured)区分,避免 expanded 冲突。
  const configuredProviders = useMemo(
    () => settings.providers.filter((provider) => provider.configured),
    [settings.providers],
  );
  // 未配置区域:排除 custom(custom 改为虚线框 + 号入口,不显示常规卡片)
  const unconfiguredProviders = useMemo(
    () => orderUnconfiguredProviders(
      settings.providers.filter((provider) => !provider.configured && provider.name !== "custom"),
    ),
    [settings.providers],
  );
  // 生成 entryKey:同一 provider 在已配置/未配置区域用不同 key 区分 expanded 状态。
  const entryKey = (provider: SettingsPayload["providers"][number]) =>
    `${provider.name}__${provider.configured ? "cfg" : "add"}`;
  const renderProviderRow = (provider: SettingsPayload["providers"][number]) => {
    const ekey = entryKey(provider);
    const expanded = expandedProvider === ekey;
    const isConfigured = provider.configured;
    const form = providerForms[provider.name] ?? {
      apiKey: "",
      apiBase: provider.api_base ?? provider.default_api_base ?? "",
      apiType: provider.api_type ?? "auto",
      model: "",
    };
    const saving = providerSaving === provider.name;
    const saved = !!providerSaved[provider.name];
    const isOauthProvider = provider.auth_type === "oauth";
    const keyVisible = !!visibleProviderKeys[provider.name];
    const editingKey = !isConfigured || !!editingProviderKeys[provider.name];
    const apiKeyRequired = provider.api_key_required ?? true;
    const apiKey = form.apiKey.trim();
    const apiBase = form.apiBase.trim();
    const missingRequiredApiKey = !isOauthProvider && apiKeyRequired && !isConfigured && !apiKey;
    const missingOptionalCredential =
      !isOauthProvider && !apiKeyRequired && !isConfigured && !apiKey && !apiBase;
    return (
      <div key={entryKey(provider)} className="divide-y divide-border/45">
        <button
          type="button"
          onClick={() => onToggleProvider(entryKey(provider))}
          className="flex min-h-[70px] w-full items-center justify-between gap-4 px-4 py-3 text-left transition-colors hover:bg-muted/35 sm:px-5"
        >
          <span className="flex min-w-0 items-center gap-3">
            <ProviderIcon
              provider={provider.name}
              showBrandLogos={showBrandLogos}
              label={provider.label}
              apiBase={provider.api_base || provider.default_api_base}
            />
            <span className="min-w-0">
              <span className="block truncate text-[15px] font-semibold leading-5 text-foreground">
                {provider.label}
              </span>
              <span className="block truncate text-[12px] text-muted-foreground">
                {provider.api_base || provider.default_api_base || provider.name}
              </span>
            </span>
          </span>
          <StatusPill tone={isConfigured ? "success" : "neutral"}>
            {isOauthProvider
              ? isConfigured
                ? tx("settings.oauth.signedIn", "Signed in")
                : tx("settings.oauth.notSignedIn", "Not signed in")
              : isConfigured
                ? t("settings.byok.configured")
                : t("settings.byok.notConfigured")}
          </StatusPill>
        </button>

        {expanded ? (
          <div className="space-y-3 bg-muted/18 px-4 py-4 sm:px-5">
            {isOauthProvider ? (
              <div className="flex flex-col gap-3 rounded-[18px] border border-border/45 bg-background/75 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <p className="text-[13px] font-semibold text-foreground">
                    {tx("settings.oauth.authentication", "OAuth authentication")}
                  </p>
                  <p className="mt-1 truncate text-[12px] text-muted-foreground">
                    {provider.configured
                      ? t("settings.oauth.signedInAs", {
                          account: provider.oauth_account || provider.label,
                          defaultValue: "Signed in as {{account}}",
                        })
                      : tx("settings.oauth.signInHelp", "Sign in from this device; no API key is stored in config.")}
                  </p>
                </div>
                <div className="flex shrink-0 justify-end gap-2">
                  {provider.configured ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onProviderOAuthLogout(provider.name)}
                      disabled={saving}
                      className="rounded-full"
                    >
                      {tx("settings.oauth.signOut", "Sign out")}
                    </Button>
                  ) : null}
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => onProviderOAuthLogin(provider.name)}
                    disabled={saving || !provider.oauth_login_supported}
                    className="rounded-full"
                  >
                    {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
                    {saving
                      ? tx("settings.oauth.signingIn", "Signing in...")
                      : provider.configured
                        ? tx("settings.oauth.signInAgain", "Sign in again")
                        : tx("settings.oauth.signIn", "Sign in")}
                  </Button>
                </div>
              </div>
            ) : (
              <>
            {/* API Key / API Base:所有 provider 一致显示(含 custom)。
                custom 的 preset 自带凭证会覆盖单例值,单例凭证作为新 preset 的默认预填。 */}
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">
                {t("settings.byok.apiKey")}
              </span>
              <div className="relative">
                {editingKey ? (
                  <ClearableInput
                    type={keyVisible ? "text" : "password"}
                    value={form.apiKey}
                    onChange={(event) =>
                      onChangeProviderForm(provider.name, { apiKey: event.target.value })
                    }
                    onClear={() => onChangeProviderForm(provider.name, { apiKey: "" })}
                    placeholder={
                      isConfigured
                        ? t("settings.byok.apiKeyConfiguredPlaceholder")
                        : t("settings.byok.apiKeyPlaceholder")
                    }
                    className="h-9 rounded-full text-[13px]"
                    trailingSlot={
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => onToggleProviderKey(provider.name)}
                        aria-label={
                          keyVisible
                            ? t("settings.byok.hideApiKey")
                            : t("settings.byok.showApiKey")
                        }
                        className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                      >
                        {keyVisible ? (
                          <EyeOff className="h-3.5 w-3.5" aria-hidden />
                        ) : (
                          <Eye className="h-3.5 w-3.5" aria-hidden />
                        )}
                      </Button>
                    }
                  />
                ) : (
                  <>
                    <div className="flex h-9 items-center rounded-full border border-input bg-background px-3 pr-11 text-[13px] text-muted-foreground">
                      {provider.api_key_hint ?? t("settings.byok.configuredKeyHint")}
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => onToggleProviderKeyEditing(provider.name)}
                      aria-label={t("settings.actions.edit")}
                      className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      <Pencil className="h-3.5 w-3.5" aria-hidden />
                    </Button>
                  </>
                )}
              </div>
            </label>
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">
                {t("settings.byok.apiBase")}
              </span>
              <ClearableInput
                value={form.apiBase}
                onChange={(event) =>
                  onChangeProviderForm(provider.name, { apiBase: event.target.value })
                }
                onClear={() => onChangeProviderForm(provider.name, { apiBase: "" })}
                placeholder={provider.default_api_base ?? t("settings.byok.apiBasePlaceholder")}
                className="h-9 rounded-full text-[13px]"
              />
            </label>
            {/* Model ID 输入栏已移除:模型配置统一通过"添加模型"按钮(InlineAddModelForm)管理,
                已配置的模型显示在下方 ProviderPresetList 中。 */}
            {provider.name === "openai" ? (
              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("settings.byok.apiType", "API type")}
                </span>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      type="button"
                      variant="outline"
                      className="h-9 w-full justify-between rounded-full px-3 text-[13px]"
                    >
                      <span>
                        {OPENAI_API_TYPE_OPTIONS.find((option) => option.value === form.apiType)?.label ??
                          form.apiType}
                      </span>
                      <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="min-w-[220px]">
                    {OPENAI_API_TYPE_OPTIONS.map((option) => (
                      <DropdownMenuItem
                        key={option.value}
                        onSelect={() => onChangeProviderForm(provider.name, { apiType: option.value })}
                      >
                        {option.label}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              </label>
            ) : null}
            {/* provider 卡片:展示该 provider 下挂载的 preset 列表(多模型支持)。
                所有 provider(含未配置、custom)一致显示,未配置时也可"添加模型"。 */}
            <ProviderPresetList
              presets={provider.presets ?? []}
              saving={saving || providerSaving === "__preset_activate__"}
              onDelete={(presetName) => onDeletePreset(presetName)}
              onActivate={(presetName) => onActivatePreset(presetName)}
              onAdd={() => onAddModelToProvider(provider.name)}
            />
            {/* inline 折叠展开式添加模型表单(替代弹窗):
                当 inlineAddModelProvider 等于当前 provider 名时展开。 */}
            {inlineAddModelProvider === provider.name ? (
              <InlineAddModelForm
                draft={inlineAddModelDraft}
                fetchedModels={inlineAddModelModels}
                modelsLoading={inlineAddModelModelsLoading}
                saving={inlineAddModelSaving}
                learning={learningProvider === provider.name}
                isCustom={provider.name === "custom"}
                onChangeDraft={onChangeInlineAddModelDraft}
                onFetchModels={onFetchInlineAddModelModels}
                onSave={onSaveInlineAddModel}
                onCancel={onCancelInlineAddModel}
              />
            ) : null}
            <div className="flex items-center justify-end gap-2">
              {/* 保存按钮:所有 provider 一致显示 */}
              <Button
                size="sm"
                variant="outline"
                onClick={() => onSaveProvider(provider.name)}
                disabled={
                  saving ||
                  saved ||
                  missingRequiredApiKey ||
                  missingOptionalCredential
                }
                className={cn(
                  "rounded-full",
                  saved && "opacity-50 cursor-not-allowed",
                )}
                title={timeoutProvider === provider.name ? t("settings.actions.queryTimeout") : undefined}
              >
                {saving
                  ? (learningProvider === provider.name
                      ? t("settings.actions.queryingContext")
                      : t("settings.actions.saving"))
                  : (learningProvider === provider.name
                      ? t("settings.actions.queryingContext")
                      : saved
                        ? tx("settings.providers.saved", "Saved")
                        : tx("settings.providers.saveProvider", "Save provider"))}
              </Button>
              {/* 已配置卡片:显示删除按钮(清除凭证 + 关联 model_preset,移回未配置)。
                  所有 provider(含 custom)一致显示。 */}
              {isConfigured ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onRequestDeleteProvider(provider.name)}
                  disabled={saving}
                  className="rounded-full"
                  title={tx("settings.byok.delete", "Delete")}
                >
                  <Trash2 className="mr-1 h-3.5 w-3.5" aria-hidden />
                  {tx("settings.byok.delete", "Delete")}
                </Button>
              ) : null}
            </div>
              </>
            )}
          </div>
        ) : null}
      </div>
    );
  };
  return (
    <div className="space-y-6">
      <p className="max-w-[42rem] text-[13px] leading-6 text-muted-foreground">
        {t("settings.byok.description")}
      </p>
      <ProviderSection
        title={t("settings.byok.configuredSection")}
        count={configuredProviders.length}
        empty={t("settings.byok.noConfiguredProviders")}
        showCount
        action={
          <Button
            variant="outline"
            size="sm"
            disabled={configuredProviders.length === 0 || deletingAllProviders}
            onClick={() => setDeleteAllConfirmOpen(true)}
            className="h-7 rounded-full px-3 text-[12px]"
          >
            {deletingAllProviders ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" aria-hidden />
            ) : (
              <Trash2 className="mr-1 h-3 w-3" aria-hidden />
            )}
            {tx("settings.byok.clearAll", "清除全部")}
          </Button>
        }
      >
        {configuredProviders.map(renderProviderRow)}
      </ProviderSection>
      <ProviderSection
        title={t("settings.byok.notConfiguredSection")}
        count={unconfiguredProviders.length}
        empty={t("settings.byok.noConfiguredProviders")}
      >
        {unconfiguredProviders.map(renderProviderRow)}
        {/* 自定义 provider 入口:虚线框 + 号,点击弹出 LLM 配置 Dialog。
            保存后触发上下文查询,成功后在已配置区域生成新卡片(如 Agnes-ai)。 */}
        <button
          type="button"
          onClick={onOpenCustomConfig}
          className="flex min-h-[70px] w-full items-center justify-center gap-2 rounded-2xl border border-dashed border-border/60 px-4 py-3 text-muted-foreground transition-colors hover:border-foreground/40 hover:bg-muted/30 hover:text-foreground sm:px-5"
        >
          <Plus className="h-5 w-5" aria-hidden />
          <span className="text-[14px] font-medium">
            {tx("settings.byok.addCustomProvider", "Add custom provider")}
          </span>
        </button>
      </ProviderSection>
      {/* custom 自定义配置 Dialog:与 InlineAddModelForm 字段一致(Model ID + API Key + API Base),
          保存逻辑复用 handleCreateModelConfiguration,触发上下文查询并在已配置区域生成新卡片。 */}
      <Dialog open={customConfigOpen} onOpenChange={(open) => { if (!open) onCancelCustomConfig(); }}>
        <DialogContent className="max-w-[520px] p-0">
          <DialogHeader className="px-5 pb-0 pt-5 text-left">
            <DialogTitle className="text-[18px] font-semibold tracking-[-0.01em]">
              {tx("settings.byok.addCustomProvider", "Add custom provider")}
            </DialogTitle>
          </DialogHeader>
          <div className="px-5 pb-5 pt-3">
            <InlineAddModelForm
              draft={customConfigDraft}
              fetchedModels={customConfigModels}
              modelsLoading={customConfigModelsLoading}
              saving={customConfigSaving}
              learning={learningProvider === "custom"}
              isCustom
              variant="dialog"
              onChangeDraft={onChangeCustomConfigDraft}
              onFetchModels={onFetchCustomConfigModels}
              onSave={onSaveCustomConfig}
              onCancel={onCancelCustomConfig}
            />
          </div>
        </DialogContent>
      </Dialog>
      {/* 一键清除所有 provider 配置确认对话框 */}
      <Dialog
        open={deleteAllConfirmOpen}
        onOpenChange={(open) => {
          if (!open && !deletingAllProviders) setDeleteAllConfirmOpen(false);
        }}
      >
        <DialogContent className="max-w-[440px]">
          <DialogHeader>
            <DialogTitle>
              {tx("settings.byok.clearAllConfirmTitle", "清除所有 Provider 配置")}
            </DialogTitle>
            <DialogDescription>
              {tx("settings.byok.clearAllConfirmDesc", "将清空所有 provider 的 API Key、API Base 和模型配置,恢复到初始状态。此操作不可撤销。")}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button
              variant="outline"
              onClick={() => setDeleteAllConfirmOpen(false)}
              disabled={deletingAllProviders}
              className="rounded-full"
            >
              {t("settings.bootstrap.cancel", { defaultValue: "Cancel" })}
            </Button>
            <Button
              variant="destructive"
              disabled={deletingAllProviders}
              onClick={async () => {
                await onDeleteAllProviders();
                setDeleteAllConfirmOpen(false);
              }}
              className="rounded-full"
            >
              {deletingAllProviders ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : null}
              {deletingAllProviders
                ? t("settings.byok.clearing", { defaultValue: "清除中..." })
                : tx("settings.byok.clearAll", "清除全部")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
