// Settings 主入口:调用 useSettingsState hook 获取全部状态与回调,
// 仅保留 JSX 渲染(renderSection + 顶层布局 + 删除 provider 确认 Dialog)。
//
// 行为保持与拆分前完全一致:
//  - 所有 state / useEffect / useCallback / useMemo 已迁出至 ./hooks/useSettingsState
//  - 各 section 通过 props 接收状态与回调
//  - 删除 provider 确认 Dialog 留在此处(与主状态强耦合)
//  - 重新导出 SettingsSectionKey,保证外部 import 路径不变

import { ChevronLeft, Loader2, RotateCcw } from "lucide-react";
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
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import {
  titleForSection,
  type SettingsSectionKey,
  type SettingsViewProps,
} from "./types";
import { SettingsSidebar } from "./components/SettingsSidebar";
import { SettingsGroup, SettingsRow } from "./components/SettingsRow";
import { OverviewSettings } from "./sections/OverviewSettings";
import { AppearanceSettings } from "./sections/AppearanceSettings";
import { AppsSettings } from "./sections/AppsSettings";
import { ModelsSettings } from "./sections/ModelsSettings";
import { ProvidersSettings } from "./sections/ProvidersSettings";
import { AdvancedSettings } from "./sections/AdvancedSettings";
import { NewModelConfigurationDialog } from "./sections/NewModelConfigurationDialog";
import { useSettingsState } from "./hooks/useSettingsState";

// 重新导出共享类型,保证外部 `import { SettingsSectionKey } from "@/components/settings/SettingsView"` 仍可用。
export type { SettingsSectionKey };

export function SettingsView({
  themeMode,
  initialSection = "overview",
  showSidebar = true,
  onSetThemeMode,
  onBackToChat,
  onModelNameChange,
  onSettingsChange,
  onLogout,
  onRestart,
  isRestarting = false,
  hostChromeInset = false,
  sidebarCollapsed = false,
  onToggleSidebar,
}: SettingsViewProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const state = useSettingsState({
    token,
    initialSection,
    onSettingsChange,
    onModelNameChange,
    onRestart,
  });

  const renderSection = () => {
    if (!state.settings) return null;
    switch (state.activeSection) {
      case "overview":
        return (
          <OverviewSettings
            settings={state.settings}
            requiresRestart={state.hasPendingRestart}
            onRestart={state.restartViaSettingsSurface}
            isRestarting={isRestarting || state.hostEngineApplying}
            showBrandLogos={state.localPrefs.brandLogos}
            onSelectSection={state.setActiveSection}
            runtimeForm={state.runtime.runtimeForm}
            runtimeDirty={state.runtime.runtimeDirty}
            runtimeSaving={state.runtime.runtimeSaving}
            onChangeRuntimeForm={state.runtime.setRuntimeForm}
            onSaveRuntime={state.runtime.saveRuntimeSettings}
            plannerSaving={state.runtime.plannerSaving}
            onSavePlanner={state.runtime.savePlannerSettings}
          />
        );
      case "appearance":
        return (
          <AppearanceSettings
            themeMode={themeMode}
            onSetThemeMode={onSetThemeMode}
            localPrefs={state.localPrefs}
            onChangeLocalPrefs={state.setLocalPrefs}
          />
        );
      case "models":
        return (
          <div className="space-y-8">
            <ModelsSettings
              form={state.models.form}
              setForm={state.models.setForm}
              settings={state.settings}
              dirty={state.models.modelDirty}
              saving={state.models.saving}
              contextWindowLearning={state.models.contextWindowLearning}
              contextWindowLearnTimeout={state.models.contextWindowLearnTimeout}
              showBrandLogos={state.localPrefs.brandLogos}
              onSave={state.models.saveModelSettings}
              onSaveContextWindow={state.models.saveContextWindow}
              onCreateConfiguration={state.models.openModelConfigurationDialog}
            />
            <ProvidersSettings
              settings={state.settings}
              expandedProvider={state.models.expandedProvider}
              providerForms={state.models.providerForms}
              visibleProviderKeys={state.models.visibleProviderKeys}
              editingProviderKeys={state.models.editingProviderKeys}
              providerSaving={state.models.providerSaving}
              providerSaved={state.models.providerSaved}
              learningProvider={state.models.learningProvider}
              timeoutProvider={state.models.timeoutProvider}
              showBrandLogos={state.localPrefs.brandLogos}
              onToggleProvider={state.models.handleToggleProvider}
              onToggleProviderKey={state.models.toggleProviderKeyVisibility}
              onToggleProviderKeyEditing={state.models.toggleProviderKeyEditing}
              onChangeProviderForm={state.models.changeProviderForm}
              onSaveProvider={state.models.saveProvider}
              onRequestDeleteProvider={(provider) => state.models.setProviderToDelete(provider)}
              onAddModelToProvider={state.models.openModelConfigurationForProvider}
              onActivatePreset={state.models.activateModelPreset}
              onDeletePreset={(presetName) => state.models.deletePreset(presetName)}
              inlineAddModelProvider={state.models.inlineAddModelProvider}
              inlineAddModelDraft={state.models.inlineAddModelDraft}
              inlineAddModelModels={state.models.inlineAddModelModels}
              inlineAddModelModelsLoading={state.models.inlineAddModelModelsLoading}
              inlineAddModelSaving={state.models.inlineAddModelSaving}
              onChangeInlineAddModelDraft={state.models.setInlineAddModelDraft}
              onCancelInlineAddModel={state.models.cancelInlineAddModel}
              onSaveInlineAddModel={state.models.saveInlineAddModel}
              onFetchInlineAddModelModels={state.models.fetchInlineAddModelModels}
              customConfigOpen={state.models.customConfigOpen}
              customConfigDraft={state.models.customConfigDraft}
              customConfigSaving={state.models.customConfigSaving}
              customConfigModels={state.models.customConfigModels}
              customConfigModelsLoading={state.models.customConfigModelsLoading}
              onOpenCustomConfig={state.models.openCustomConfig}
              onChangeCustomConfigDraft={state.models.setCustomConfigDraft}
              onCancelCustomConfig={state.models.cancelCustomConfig}
              onSaveCustomConfig={state.models.saveCustomConfig}
              onFetchCustomConfigModels={state.models.fetchCustomConfigModels}
              onDeleteAllProviders={state.models.deleteAllProviders}
              deletingAllProviders={state.models.deletingAllProviders}
            />
          </div>
        );
      case "advanced":
        return (
          <AdvancedSettings
            form={state.advanced.networkSafetyForm}
            dirty={state.advanced.networkSafetyDirty}
            saving={state.advanced.networkSafetySaving}
            isNativeHostSurface={(state.settings.surface ?? state.settings.runtime_surface) === "native"}
            onChangeForm={state.advanced.setNetworkSafetyForm}
            onSave={state.advanced.saveNetworkSafetySettings}
            onRestart={state.restartViaSettingsSurface}
            isRestarting={isRestarting || state.hostEngineApplying}
            requiresRestartPending={state.pendingRestartSections.runtime}
          />
        );
      case "apps":
        return <AppsSettings />;
      default:
        return null;
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-[radial-gradient(circle_at_50%_0%,hsl(var(--muted))_0%,hsl(var(--background))_42%)] md:flex-row">
      {showSidebar ? (
        <SettingsSidebar
          activeSection={state.activeSection}
          onSelectSection={state.setActiveSection}
          onLogout={onLogout}
          hostChromeInset={hostChromeInset}
          collapsed={sidebarCollapsed}
          onToggleSidebar={onToggleSidebar}
        />
      ) : null}

      <NewModelConfigurationDialog
        open={state.models.modelConfigurationOpen}
        draft={state.models.modelConfigurationForm}
        providers={state.models.configuredModelProviderOptions}
        saving={state.models.modelConfigurationSaving}
        showProviderLogos={state.localPrefs.brandLogos}
        onOpenChange={state.models.setModelConfigurationOpen}
        onChangeDraft={state.models.setModelConfigurationForm}
        onSave={state.models.handleCreateModelConfiguration}
        learning={
          state.models.contextWindowLearning
          && !!state.models.modelConfigurationForm.provider
          && state.models.learningProvider === state.models.modelConfigurationForm.provider
        }
      />

      <Dialog
        open={state.models.providerToDelete !== null}
        onOpenChange={(open) => {
          if (!open && !state.models.providerDeleting) state.models.setProviderToDelete(null);
        }}
      >
        <DialogContent className="max-w-[520px]">
          <DialogHeader>
            <DialogTitle>
              {t("settings.byok.deleteConfirmTitle", { defaultValue: "Delete provider configuration" })}
            </DialogTitle>
            <DialogDescription>
              {t("settings.byok.deleteConfirmDescription", {
                defaultValue:
                  "This will clear the provider's API key, API base, and associated model configurations. This action cannot be undone.",
              })}
            </DialogDescription>
          </DialogHeader>
          {/* 列出将一并删除的 preset 列表,避免级联删除造成意外损失 */}
          {(() => {
            const provider = state.settings?.providers.find(
              (p) => p.name === state.models.providerToDelete,
            );
            const affectedPresets = provider?.presets ?? [];
            if (affectedPresets.length === 0) return null;
            return (
              <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2">
                <p className="mb-1.5 text-[12px] font-medium text-destructive">
                  {t("settings.byok.affectedPresets", {
                    defaultValue: "The following {{count}} model configuration(s) will also be deleted:",
                    count: affectedPresets.length,
                  })}
                </p>
                <ul className="space-y-1 text-[12px] text-muted-foreground">
                  {affectedPresets.map((preset) => (
                    <li key={preset.name} className="flex items-center gap-2">
                      <span className="truncate font-medium text-foreground">{preset.label}</span>
                      <span className="truncate text-muted-foreground">· {preset.model}</span>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })()}
          <DialogFooter className="gap-2">
            <Button
              variant="outline"
              onClick={() => state.models.setProviderToDelete(null)}
              disabled={state.models.providerDeleting}
            >
              {t("settings.bootstrap.cancel", { defaultValue: "Cancel" })}
            </Button>
            <Button
              variant="destructive"
              onClick={state.models.confirmDeleteProvider}
              disabled={state.models.providerDeleting}
            >
              {state.models.providerDeleting ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : null}
              {state.models.providerDeleting
                ? t("settings.byok.deleting", { defaultValue: "Deleting..." })
                : t("settings.byok.deleteConfirmAction", { defaultValue: "Delete" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 保存需要重启的设置后弹出询问对话框 */}
      <Dialog
        open={state.restartConfirmOpen}
        onOpenChange={(open) => {
          if (!open && !isRestarting && !state.hostEngineApplying) state.cancelRestart();
        }}
      >
        <DialogContent className="max-w-[440px]" showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>
              {t("settings.restart.confirmTitle", { defaultValue: "Restart required" })}
            </DialogTitle>
            <DialogDescription>
              {t("settings.restart.confirmDescription", {
                defaultValue:
                  "The configuration has been saved. Restart the engine now to apply the changes, or do it later from the status bar.",
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-2">
            <Button
              variant="outline"
              onClick={state.cancelRestart}
              disabled={isRestarting || state.hostEngineApplying}
            >
              {t("settings.restart.later", { defaultValue: "Later" })}
            </Button>
            <Button
              onClick={state.confirmRestart}
              disabled={isRestarting || state.hostEngineApplying}
            >
              {(isRestarting || state.hostEngineApplying) && (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              )}
              {(isRestarting || state.hostEngineApplying)
                ? t("app.system.restarting", { defaultValue: "Restarting..." })
                : t("app.system.restart", { defaultValue: "Restart" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <main className="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
        <div
          className={cn(
            "mx-auto w-full max-w-[920px] px-5 py-8 sm:px-8 lg:py-12",
            hostChromeInset && "pt-[4.25rem] sm:pt-[4.25rem] lg:pt-[4.75rem]",
          )}
        >
          <div className="mb-7">
            <Button
              size="sm"
              variant="ghost"
              onClick={onBackToChat}
              className="mb-3 gap-1.5 rounded-full text-muted-foreground hover:text-foreground"
              title={t("settings.backToChat")}
            >
              <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
              {t("settings.backToChat")}
            </Button>
            <div className="flex items-end justify-between gap-3">
              <h1 className="text-[28px] font-semibold leading-tight tracking-[-0.02em] text-foreground sm:text-[34px]">
                {state.text(`settings.nav.${state.activeSection}`, titleForSection(state.activeSection))}
              </h1>
              <div className="flex shrink-0 items-center gap-2 pb-1.5">
                {state.activeSection === "overview" ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={state.restartViaSettingsSurface}
                    disabled={!state.hasPendingRestart || isRestarting || state.hostEngineApplying}
                    className={cn(
                      "shrink-0 rounded-full",
                      !state.hasPendingRestart && "opacity-40 cursor-not-allowed hover:bg-transparent",
                    )}
                    title={state.hasPendingRestart ? undefined : t("settings.values.ready", { defaultValue: "Ready" })}
                  >
                    {isRestarting || state.hostEngineApplying ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                    ) : (
                      <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                    )}
                    {isRestarting || state.hostEngineApplying
                      ? t("app.system.restarting")
                      : t("app.system.restart")}
                  </Button>
                ) : null}
              </div>
            </div>
          </div>

          {state.loading ? (
            <div className="flex h-48 items-center justify-center rounded-[24px] border border-border/50 bg-card/75 text-sm text-muted-foreground shadow-[0_20px_70px_rgba(15,23,42,0.07)]">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              {t("settings.status.loading")}
            </div>
          ) : state.error && !state.settings ? (
            <SettingsGroup>
              <SettingsRow title={t("settings.status.loadError")}>
                <span className="max-w-[520px] text-sm text-muted-foreground">{state.error}</span>
              </SettingsRow>
            </SettingsGroup>
          ) : state.settings ? (
            <div className="space-y-5">
              {state.error ? (
                <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
                  {state.error}
                </div>
              ) : null}
              {renderSection()}
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}
