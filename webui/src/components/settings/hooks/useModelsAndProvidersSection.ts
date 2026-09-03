// Models + Providers section 子 hook：
// 当前模型选择、上下文窗口学习、provider 卡片管理、model configuration dialog、
// inline 添加模型、custom 自定义配置入口等全部状态与回调。
//
// 从 useSettingsState 抽取，降低主 hook 复杂度。
// Models 和 Providers 在 SettingsView 中共同渲染于 "models" section，且共享
// contextWindowLearning / pollContextWindowLearning / handleCreateModelConfiguration，
// 紧密耦合，因此合并为一个子 hook。
//
// 行为保持与拆分前一致：
//   - form 初值、dirty 判定、save 流程、轮询逻辑原样迁入
//   - form 同步改为监听 settings 变化（原 applyPayload 中的 setForm 逻辑移至此处 useEffect）
//   - providerForms 同步 effect 原样迁入

import { Dispatch, SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  createModelConfiguration,
  deleteAllProviders as deleteAllProvidersApi,
  deleteModelConfiguration,
  deleteProviderSettings,
  fetchProviderModels,
  fetchSettings,
  updateModelConfiguration,
  updateProviderSettings,
  updateSettings,
} from "@/lib/api";
import { STORAGE_KEYS } from "@/lib/storage";
import type { SettingsPayload } from "@/lib/types";

import {
  defaultPreset,
  editableDefaultProvider,
  modelPresetValue,
  type AgentSettingsDraft,
  type ModelConfigurationDraft,
  type ProviderForm,
} from "../types";
import type { UseSectionShared } from "./useWebSearchSection";

/** 主 hook 传入的共享依赖 + 本 section 独有依赖 */
export interface UseModelsAndProvidersParams extends UseSectionShared {
  onModelNameChange: (modelName: string | null) => void;
}

/** Models + Providers section 暴露的状态与回调 */
export interface ModelsAndProvidersSectionState {
  // agent form (current model selection)
  form: AgentSettingsDraft;
  setForm: Dispatch<SetStateAction<AgentSettingsDraft>>;
  saving: boolean;
  modelDirty: boolean;
  saveModelSettings: () => Promise<void>;
  saveContextWindow: (value: number) => Promise<void>;

  // context window learning
  contextWindowLearning: boolean;
  learningProvider: string | null;
  timeoutProvider: string | null;
  contextWindowLearnTimeout: boolean;

  // model configuration dialog
  modelConfigurationOpen: boolean;
  setModelConfigurationOpen: Dispatch<SetStateAction<boolean>>;
  modelConfigurationForm: ModelConfigurationDraft;
  setModelConfigurationForm: Dispatch<SetStateAction<ModelConfigurationDraft>>;
  modelConfigurationSaving: boolean;
  openModelConfigurationDialog: () => void;
  handleCreateModelConfiguration: (overrideDraft?: ModelConfigurationDraft) => Promise<{ ok: boolean; learning: boolean }>;
  configuredModelProviderOptions: Array<{ name: string; label: string }>;

  // inline add model
  inlineAddModelProvider: string | null;
  inlineAddModelDraft: ModelConfigurationDraft;
  setInlineAddModelDraft: Dispatch<SetStateAction<ModelConfigurationDraft>>;
  inlineAddModelModels: string[];
  inlineAddModelModelsLoading: boolean;
  inlineAddModelSaving: boolean;
  openModelConfigurationForProvider: (providerName: string) => void;
  cancelInlineAddModel: () => void;
  saveInlineAddModel: () => Promise<void>;
  fetchInlineAddModelModels: () => Promise<void>;

  // custom config entry
  customConfigOpen: boolean;
  customConfigDraft: ModelConfigurationDraft;
  setCustomConfigDraft: Dispatch<SetStateAction<ModelConfigurationDraft>>;
  customConfigModels: string[];
  customConfigModelsLoading: boolean;
  customConfigSaving: boolean;
  openCustomConfig: () => void;
  cancelCustomConfig: () => void;
  saveCustomConfig: () => Promise<void>;
  fetchCustomConfigModels: () => Promise<void>;

  // provider cards
  expandedProvider: string | null;
  setExpandedProvider: Dispatch<SetStateAction<string | null>>;
  providerForms: Record<string, ProviderForm>;
  visibleProviderKeys: Record<string, boolean>;
  editingProviderKeys: Record<string, boolean>;
  providerSaving: string | null;
  providerSaved: Record<string, boolean>;
  providerModels: Record<string, string[]>;
  providerModelsLoading: string | null;
  handleToggleProvider: (entryKey: string) => void;
  toggleProviderKeyVisibility: (providerName: string) => void;
  toggleProviderKeyEditing: (providerName: string) => void;
  changeProviderForm: (providerName: string, value: Partial<ProviderForm>) => void;
  saveProvider: (providerName: string) => Promise<void>;
  fetchProviderModelList: (providerName: string) => Promise<void>;

  // preset activate / delete
  activateModelPreset: (presetName: string) => Promise<void>;
  deletePreset: (presetName: string) => Promise<void>;

  // custom preset label (legacy saveCustomConfiguration)
  customPresetLabel: string;
  setCustomPresetLabel: Dispatch<SetStateAction<string>>;
  saveCustomConfiguration: () => Promise<void>;

  // delete provider
  providerToDelete: string | null;
  setProviderToDelete: Dispatch<SetStateAction<string | null>>;
  providerDeleting: boolean;
  confirmDeleteProvider: () => Promise<void>;

  // 一键清除所有 provider 配置
  deleteAllProviders: () => Promise<void>;
  deletingAllProviders: boolean;
}

export function useModelsAndProvidersSection(
  params: UseModelsAndProvidersParams,
): ModelsAndProvidersSectionState {
  const {
    settings,
    token,
    setError,
    applyPayload,
    setPendingRestartSections,
    maybeRestartHostEngine,
    onModelNameChange,
  } = params;

  const { t } = useTranslation();

  // === agent form (current model selection) ===
  const [form, setForm] = useState<AgentSettingsDraft>({
    model: "",
    provider: "",
    modelPreset: "default",
    presetLabel: "Default",
    toolHintMaxLength: 40,
  });
  const [saving, setSaving] = useState(false);

  // === context window learning ===
  const [contextWindowLearning, setContextWindowLearning] = useState(false);
  const [learningProvider, setLearningProvider] = useState<string | null>(null);
  const [timeoutProvider, setTimeoutProvider] = useState<string | null>(null);
  const [contextWindowLearnTimeout, setContextWindowLearnTimeout] = useState(false);
  const learningPollCancelRef = useRef<boolean>(false);

  // === model configuration dialog ===
  const [modelConfigurationOpen, setModelConfigurationOpen] = useState(false);
  const [modelConfigurationSaving, setModelConfigurationSaving] = useState(false);
  const [modelConfigurationForm, setModelConfigurationForm] = useState<ModelConfigurationDraft>({
    label: "",
    provider: "",
    model: "",
  });

  // === inline add model ===
  const [inlineAddModelProvider, setInlineAddModelProvider] = useState<string | null>(null);
  const [inlineAddModelDraft, setInlineAddModelDraft] = useState<ModelConfigurationDraft>({
    label: "",
    provider: "",
    model: "",
  });
  const [inlineAddModelModels, setInlineAddModelModels] = useState<string[]>([]);
  const [inlineAddModelModelsLoading, setInlineAddModelModelsLoading] = useState(false);
  const [inlineAddModelSaving, setInlineAddModelSaving] = useState(false);

  // === custom config entry ===
  const [customConfigOpen, setCustomConfigOpen] = useState(false);
  const [customConfigDraft, setCustomConfigDraft] = useState<ModelConfigurationDraft>({
    label: "",
    provider: "custom",
    model: "",
    apiKey: "",
    apiBase: "",
  });
  const [customConfigModels, setCustomConfigModels] = useState<string[]>([]);
  const [customConfigModelsLoading, setCustomConfigModelsLoading] = useState(false);
  const [customConfigSaving, setCustomConfigSaving] = useState(false);

  // === provider cards ===
  const [providerSaving, setProviderSaving] = useState<string | null>(null);
  const [providerSaved, setProviderSaved] = useState<Record<string, boolean>>({});
  const [providerModels, setProviderModels] = useState<Record<string, string[]>>({});
  const [providerModelsLoading, setProviderModelsLoading] = useState<string | null>(null);
  const [providerToDelete, setProviderToDelete] = useState<string | null>(null);
  const [providerDeleting, setProviderDeleting] = useState(false);
  const [deletingAllProviders, setDeletingAllProviders] = useState(false);
  const [customPresetLabel, setCustomPresetLabel] = useState("");
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [providerForms, setProviderForms] = useState<Record<string, ProviderForm>>({});
  const [visibleProviderKeys, setVisibleProviderKeys] = useState<Record<string, boolean>>({});
  const [editingProviderKeys, setEditingProviderKeys] = useState<Record<string, boolean>>({});

  const settingsProviders = settings?.providers;
  const settingsModelPresets = settings?.model_presets;
  const settingsAgent = settings?.agent;

  // 监听 settings 变化同步 form（原 applyPayload 中的 setForm 逻辑，移至此处）
  useEffect(() => {
    if (!settings) return;
    const fallbackDefault = defaultPreset(settings);
    const activePresetName = modelPresetValue(settings);
    const activePreset =
      settings.model_presets.find((preset) => preset.name === activePresetName) ?? fallbackDefault;
    setForm({
      model: activePreset?.model ?? settings.agent.model,
      provider: activePreset?.is_default
        ? editableDefaultProvider(settings)
        : activePreset?.provider ?? editableDefaultProvider(settings),
      modelPreset: activePresetName,
      presetLabel: activePreset?.label ?? activePresetName,
      toolHintMaxLength: settings.agent.tool_hint_max_length,
    });
  }, [settings]);

  // 同步 providerForms / providerSaved（原 useSettingsState 中的 effect，原样迁入）
  useEffect(() => {
    if (!settingsProviders || !settingsModelPresets || !settingsAgent) return;
    setProviderForms((prev) => {
      const next = { ...prev };
      for (const provider of settingsProviders) {
        if (provider.preset_name) {
          const preset = settingsModelPresets.find((p) => p.name === provider.preset_name);
          const existing = next[provider.name];
          next[provider.name] = {
            apiKey: existing?.apiKey ?? "",
            apiBase: existing?.apiBase || preset?.api_base || provider.api_base || "",
            apiType: "auto",
            model: existing?.model || preset?.model || provider.model || "",
          };
          continue;
        }
        const activePreset = settingsModelPresets.find((p) => p.active);
        const matchingPreset =
          activePreset && (activePreset.provider === provider.name || (activePreset.is_default && settingsAgent.provider === provider.name))
            ? activePreset
            : settingsModelPresets.find((p) => !p.is_default && p.provider === provider.name);
        const inferredModel =
          prev[provider.name]?.model ??
          (matchingPreset ? matchingPreset.model : "") ??
          (settingsAgent.provider === provider.name ? settingsAgent.model : "") ??
          "";
        next[provider.name] = {
          apiKey: next[provider.name]?.apiKey ?? "",
          apiBase: next[provider.name]?.apiBase ??
            (provider.api_base ?? provider.default_api_base ?? ""),
          apiType: next[provider.name]?.apiType ?? provider.api_type ?? "auto",
          model: next[provider.name]?.model ?? inferredModel,
        };
      }
      return next;
    });
    setProviderSaved((prev) => {
      const next = { ...prev };
      for (const provider of settingsProviders) {
        if (next[provider.name] === undefined) {
          next[provider.name] = provider.configured;
        }
      }
      return next;
    });
  }, [settingsProviders, settingsModelPresets, settingsAgent]);

  // 清理旧的 localStorage provider models 缓存（原 useSettingsState 中的 effect）
  useEffect(() => {
    try {
      localStorage.removeItem(STORAGE_KEYS.providerModels);
    } catch {
      // ignore
    }
  }, []);

  const modelDirty = useMemo(() => {
    if (!settings) return false;
    return form.modelPreset !== modelPresetValue(settings);
  }, [form, settings]);

  const configuredModelProviderOptions = useMemo(
    () =>
      settingsProviders
        ?.filter((provider) => provider.configured)
        .map((provider) => ({ name: provider.name, label: provider.label })) ?? [],
    [settingsProviders],
  );

  // 轮询 settings，直到目标模型的上下文窗口学习完成
  const pollContextWindowLearning = useCallback(
    async (modelName: string, providerName: string | null = null) => {
      learningPollCancelRef.current = false;
      const maxAttempts = 40;
      const intervalMs = 1500;
      for (let i = 0; i < maxAttempts; i++) {
        if (learningPollCancelRef.current) return;
        await new Promise((r) => setTimeout(r, intervalMs));
        if (learningPollCancelRef.current) return;
        try {
          const fresh = await fetchSettings(token);
          if (learningPollCancelRef.current) return;
          let status: string = "unknown";
          if (fresh.agent.model === modelName) {
            status = fresh.agent.resolved_context_window_status ?? "unknown";
          } else {
            const preset = fresh.model_presets.find((p) => p.model === modelName);
            status = preset?.resolved_context_window_status ?? "unknown";
          }
          if (status === "learned" || status === "configured") {
            applyPayload(fresh);
            setContextWindowLearning(false);
            setLearningProvider(null);
            setTimeoutProvider(null);
            return;
          }
          applyPayload(fresh);
        } catch {
          // 轮询期间的错误忽略
        }
      }
      try {
        const fresh = await fetchSettings(token);
        if (!learningPollCancelRef.current) applyPayload(fresh);
      } catch {
        // 忽略
      }
      if (!learningPollCancelRef.current) {
        setContextWindowLearning(false);
        setLearningProvider(null);
        setContextWindowLearnTimeout(true);
        setTimeoutProvider(providerName);
      }
    },
    [applyPayload, token],
  );

  const saveModelSettings = useCallback(async () => {
    if (!settings || !modelDirty || saving) return;
    const modelChanged = form.model !== settings.agent.model;
    const matchingPreset = settings.model_presets.find((p) => p.model === form.model);
    const targetStatus = matchingPreset?.resolved_context_window_status ?? "unknown";
    const willQueryContext = modelChanged && targetStatus === "unknown";
    learningPollCancelRef.current = true;
    setSaving(true);
    setContextWindowLearning(willQueryContext);
    if (willQueryContext) {
      setContextWindowLearnTimeout(false);
      setTimeoutProvider(null);
      setLearningProvider(null);
    }
    try {
      const payload: SettingsPayload = await updateSettings(token, {
        modelPreset: form.modelPreset,
      });
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      setError(null);
      if (willQueryContext) {
        pollContextWindowLearning(form.model);
      } else {
        setContextWindowLearning(false);
      }
    } catch (err) {
      setError((err as Error).message);
      setContextWindowLearning(false);
    } finally {
      setSaving(false);
    }
  }, [settings, modelDirty, saving, form, token, applyPayload, onModelNameChange, setError, pollContextWindowLearning]);

  const saveContextWindow = useCallback(
    async (value: number) => {
      if (!settings) return;
      const selectedPreset = settings.model_presets.find((preset) => preset.name === form.modelPreset);
      let payload: SettingsPayload;
      if (selectedPreset && !selectedPreset.is_default) {
        payload = await updateModelConfiguration(token, {
          name: selectedPreset.name,
          contextWindowTokens: value,
        });
      } else {
        payload = await updateSettings(token, {
          modelPreset: form.modelPreset,
          contextWindowTokens: value,
        });
      }
      applyPayload(payload);
      setError(null);
    },
    [settings, token, form.modelPreset, applyPayload, setError],
  );

  const handleCreateModelConfiguration = useCallback(
    async (overrideDraft?: ModelConfigurationDraft): Promise<{ ok: boolean; learning: boolean }> => {
      if (modelConfigurationSaving) return { ok: false, learning: false };
      const draft = overrideDraft ?? modelConfigurationForm;
      const label = draft.label.trim();
      const provider = draft.provider.trim();
      const model = draft.model.trim();
      if (!label || !provider || !model) return { ok: false, learning: false };
      const editingName = draft.editingPresetName;
      const isNewModel = !editingName;
      // 不在此处预设 learningProvider:需先调用 API,根据后端返回的 status 判断
      // 模型是否已学习过(已学习则后端立即返回 "learned"/"configured")。
      // 若提前设置 learningProvider,会对已学习模型显示"查询中"状态,
      // 而第一次轮询(1500ms 后)就会重置,造成"一闪而过"。
      setModelConfigurationSaving(true);
      try {
        const payload: SettingsPayload = editingName
          ? await updateModelConfiguration(token, {
              name: editingName,
              label,
              provider,
              model,
              apiKey: draft.apiKey,
              apiBase: draft.apiBase,
            })
          : await createModelConfiguration(token, {
              label,
              provider,
              model,
              apiKey: draft.apiKey,
              apiBase: draft.apiBase,
            });
        applyPayload(payload);
        onModelNameChange(payload.agent.model || null);
        setError(null);
        // 检查后端返回的 resolved_context_window_status:
        // - "learned"/"configured":模型已学习过,无需轮询,直接收起表单
        // - "unknown":模型未学习过,需要轮询并保持表单展开显示"查询中"
        const preset = payload.model_presets.find((p) => p.model === model);
        const agentStatus =
          payload.agent.model === model
            ? payload.agent.resolved_context_window_status
            : undefined;
        const status = preset?.resolved_context_window_status ?? agentStatus ?? "unknown";
        const alreadyLearned = status === "learned" || status === "configured";
        const needLearning = isNewModel && !alreadyLearned;
        if (needLearning) {
          learningPollCancelRef.current = true;
          setContextWindowLearning(true);
          setLearningProvider(provider);
          setContextWindowLearnTimeout(false);
          setTimeoutProvider(null);
          pollContextWindowLearning(model, provider);
        }
        // 不需要轮询(已学习或编辑模式)时收起 Dialog
        if (!needLearning) {
          setModelConfigurationOpen(false);
        }
        return { ok: true, learning: needLearning };
      } catch (err) {
        setError((err as Error).message);
        setContextWindowLearning(false);
        setLearningProvider(null);
        return { ok: false, learning: false };
      } finally {
        setModelConfigurationSaving(false);
      }
    },
    [modelConfigurationSaving, modelConfigurationForm, token, applyPayload, onModelNameChange, setError, pollContextWindowLearning],
  );

  const openModelConfigurationDialog = useCallback(() => {
    if (!settings) return;
    const currentProvider = settings.agent.provider;
    const provider =
      configuredModelProviderOptions.find((option) => option.name === currentProvider)?.name ??
      configuredModelProviderOptions[0]?.name ??
      "";
    setModelConfigurationForm({
      label: "",
      provider,
      model: "",
      apiKey: "",
      apiBase: "",
      editingPresetName: undefined,
    });
    setModelConfigurationOpen(true);
  }, [settings, configuredModelProviderOptions]);

  const openModelConfigurationForProvider = useCallback((providerName: string) => {
    setInlineAddModelDraft({
      label: "",
      provider: providerName,
      model: "",
      apiKey: "",
      apiBase: "",
      editingPresetName: undefined,
    });
    setInlineAddModelModels([]);
    setInlineAddModelProvider(providerName);
  }, []);

  const cancelInlineAddModel = useCallback(() => {
    setInlineAddModelProvider(null);
    setInlineAddModelModels([]);
  }, []);

  const saveInlineAddModel = useCallback(async () => {
    if (inlineAddModelSaving) return;
    setInlineAddModelSaving(true);
    try {
      const model = inlineAddModelDraft.model.trim();
      // 未配置非 custom provider:InlineAddModelForm 不显示凭证字段,
      // 此时用用户在卡片上填入 providerForms 的 apiKey/apiBase 兜底,
      // 保证保存时凭证一并写入(draft 优先,providerForms 兜底)。
      const providerForm = providerForms[inlineAddModelDraft.provider];
      const draftWithLabel: ModelConfigurationDraft = {
        ...inlineAddModelDraft,
        label: model,
        apiKey: inlineAddModelDraft.apiKey?.trim() || providerForm?.apiKey?.trim() || undefined,
        apiBase: inlineAddModelDraft.apiBase?.trim() || providerForm?.apiBase?.trim() || undefined,
      };
      const result = await handleCreateModelConfiguration(draftWithLabel);
      if (result.ok) {
        // 不立即收起 inline form:若后端触发上下文窗口学习,
        // 保持表单展开显示"查询中"状态,等轮询结束后由 pollContextWindowLearning 重置 learningProvider。
        // 仅在未触发学习时才收起。
        if (!result.learning) {
          setInlineAddModelProvider(null);
          setInlineAddModelModels([]);
        }
      }
    } finally {
      setInlineAddModelSaving(false);
    }
  }, [inlineAddModelSaving, inlineAddModelDraft, providerForms, handleCreateModelConfiguration]);

  const fetchInlineAddModelModels = useCallback(async () => {
    if (inlineAddModelModelsLoading) return;
    const providerName = inlineAddModelDraft.provider;
    if (!providerName) return;
    // 未配置非 custom provider:InlineAddModelForm 不显示凭证字段,
    // 用用户在卡片上填入 providerForms 的 apiKey/apiBase 拉取模型列表,
    // 否则后端会回退用 default_api_base + 空 key 凑数请求(draft 优先,providerForms 兜底)。
    const providerForm = providerForms[providerName];
    setInlineAddModelModelsLoading(true);
    try {
      const models = await fetchProviderModels(token, providerName, {
        apiKey: inlineAddModelDraft.apiKey?.trim() || providerForm?.apiKey?.trim() || undefined,
        apiBase: inlineAddModelDraft.apiBase?.trim() || providerForm?.apiBase?.trim() || undefined,
      });
      setInlineAddModelModels(models ?? []);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
      setInlineAddModelModels([]);
    } finally {
      setInlineAddModelModelsLoading(false);
    }
  }, [inlineAddModelModelsLoading, inlineAddModelDraft, providerForms, token, setError]);

  // === custom 自定义配置入口 ===
  const openCustomConfig = useCallback(() => {
    setCustomConfigDraft({
      label: "",
      provider: "custom",
      model: "",
      apiKey: "",
      apiBase: "",
    });
    setCustomConfigModels([]);
    setCustomConfigOpen(true);
  }, []);

  const cancelCustomConfig = useCallback(() => {
    setCustomConfigOpen(false);
    setCustomConfigModels([]);
  }, []);

  const saveCustomConfig = useCallback(async () => {
    if (customConfigSaving) return;
    setCustomConfigSaving(true);
    try {
      const model = customConfigDraft.model.trim();
      const draftWithLabel: ModelConfigurationDraft = {
        ...customConfigDraft,
        label: model,
      };
      const result = await handleCreateModelConfiguration(draftWithLabel);
      if (result.ok) {
        // 若触发上下文窗口学习,保持 Dialog 展开显示"查询中",轮询结束后由 pollContextWindowLearning 重置状态。
        if (!result.learning) {
          setCustomConfigOpen(false);
          setCustomConfigModels([]);
        }
      }
    } finally {
      setCustomConfigSaving(false);
    }
  }, [customConfigSaving, customConfigDraft, handleCreateModelConfiguration]);

  const fetchCustomConfigModels = useCallback(async () => {
    if (customConfigModelsLoading) return;
    setCustomConfigModelsLoading(true);
    try {
      const models = await fetchProviderModels(token, "custom", {
        apiKey: customConfigDraft.apiKey?.trim() || undefined,
        apiBase: customConfigDraft.apiBase?.trim() || undefined,
      });
      setCustomConfigModels(models ?? []);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
      setCustomConfigModels([]);
    } finally {
      setCustomConfigModelsLoading(false);
    }
  }, [customConfigModelsLoading, customConfigDraft, token, setError]);

  const activateModelPreset = useCallback(async (presetName: string) => {
    if (!settings) return;
    setProviderSaving("__preset_activate__");
    try {
      const payload: SettingsPayload = await updateSettings(token, { modelPreset: presetName });
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setProviderSaving(null);
    }
  }, [settings, token, applyPayload, onModelNameChange, setPendingRestartSections, maybeRestartHostEngine, setError]);

  const deletePreset = useCallback(async (presetName: string) => {
    if (!settings || providerSaving) return;
    setProviderSaving("__preset_activate__");
    try {
      const payload: SettingsPayload = await deleteModelConfiguration(token, presetName);
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setProviderSaving(null);
    }
  }, [settings, providerSaving, token, applyPayload, onModelNameChange, setPendingRestartSections, maybeRestartHostEngine, setError]);

  const resetProviderDraft = useCallback((providerName: string) => {
    const provider = settingsProviders?.find((item) => item.name === providerName);
    if (!provider) return;
    if (provider.preset_name) {
      const preset = settingsModelPresets?.find((p) => p.name === provider.preset_name);
      setProviderForms((prev) => ({
        ...prev,
        [providerName]: {
          apiKey: "",
          apiBase: preset?.api_base || provider.api_base || "",
          apiType: "auto",
          model: preset?.model || provider.model || "",
        },
      }));
      setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: false }));
      setEditingProviderKeys((prev) => ({ ...prev, [providerName]: false }));
      setProviderModels((prev) => {
        if (!(providerName in prev)) return prev;
        const next = { ...prev };
        delete next[providerName];
        return next;
      });
      return;
    }
    const activePreset = settingsModelPresets?.find((p) => p.active);
    const matchingPreset =
      activePreset && (activePreset.provider === providerName || (activePreset.is_default && settingsAgent?.provider === providerName))
        ? activePreset
        : settingsModelPresets?.find((p) => !p.is_default && p.provider === providerName);
    setProviderForms((prev) => ({
      ...prev,
      [providerName]: {
        apiKey: "",
        apiBase: provider.api_base ?? provider.default_api_base ?? "",
        apiType: provider.api_type ?? "auto",
        model: matchingPreset?.model ?? (settingsAgent?.provider === providerName ? (settingsAgent.model || "") : ""),
      },
    }));
    setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: false }));
    setEditingProviderKeys((prev) => ({ ...prev, [providerName]: false }));
    setProviderModels((prev) => {
      if (!(providerName in prev)) return prev;
      const next = { ...prev };
      delete next[providerName];
      return next;
    });
  }, [settingsProviders, settingsModelPresets, settingsAgent]);

  const handleToggleProvider = useCallback((entryKey: string) => {
    const providerName = entryKey.split("__")[0];
    if (expandedProvider) resetProviderDraft(expandedProvider.split("__")[0]);
    if (expandedProvider === entryKey) {
      setExpandedProvider(null);
      if (inlineAddModelProvider) cancelInlineAddModel();
    } else {
      setProviderModels((prev) => {
        if (!(providerName in prev)) return prev;
        const next = { ...prev };
        delete next[providerName];
        return next;
      });
      setExpandedProvider(entryKey);
    }
  }, [expandedProvider, resetProviderDraft, inlineAddModelProvider, cancelInlineAddModel]);

  const toggleProviderKeyVisibility = useCallback((providerName: string) => {
    setVisibleProviderKeys((prev) => {
      const isVisible = prev[providerName];
      return { ...prev, [providerName]: !isVisible };
    });
  }, []);

  const toggleProviderKeyEditing = useCallback((providerName: string) => {
    setEditingProviderKeys((prev) => {
      const nextEditing = !prev[providerName];
      if (!nextEditing) {
        setProviderForms((forms) => ({
          ...forms,
          [providerName]: {
            apiKey: "",
            apiBase: forms[providerName]?.apiBase ?? "",
            apiType: forms[providerName]?.apiType ?? "auto",
            model: forms[providerName]?.model ?? "",
          },
        }));
        setVisibleProviderKeys((visible) => ({ ...visible, [providerName]: false }));
      }
      return { ...prev, [providerName]: nextEditing };
    });
  }, []);

  const changeProviderForm = useCallback((providerName: string, value: Partial<ProviderForm>) => {
    setProviderForms((prev) => ({
      ...prev,
      [providerName]: {
        apiKey: prev[providerName]?.apiKey ?? "",
        apiBase: prev[providerName]?.apiBase ?? "",
        apiType: prev[providerName]?.apiType ?? "auto",
        model: prev[providerName]?.model ?? "",
        ...value,
      },
    }));
    setProviderSaved((prev) => ({ ...prev, [providerName]: false }));
  }, []);

  const saveProvider = useCallback(async (providerName: string) => {
    if (providerSaving) return;
    const provider = settings?.providers.find((item) => item.name === providerName);
    if (!provider) return;
    const providerForm = providerForms[providerName] ?? { apiKey: "", apiBase: "", apiType: "auto", model: "" };
    const apiKey = providerForm.apiKey.trim();
    const apiKeyRequired = provider.api_key_required ?? true;
    if (!provider.configured && apiKeyRequired && !apiKey) {
      setError(t("settings.byok.apiKeyRequired"));
      return;
    }
    const modelId = providerForm.model.trim();
    const modelChanged = !!modelId && modelId !== (settings?.agent.model ?? "");
    const matchingPreset = settings?.model_presets.find((p) => p.model === modelId);
    const targetStatus = matchingPreset?.resolved_context_window_status ?? "unknown";
    const willQueryContext = modelChanged && targetStatus === "unknown";
    if (willQueryContext) {
      learningPollCancelRef.current = true;
      setLearningProvider(providerName);
      setTimeoutProvider(null);
      setContextWindowLearning(true);
      setContextWindowLearnTimeout(false);
    }
    setProviderSaving(providerName);
    try {
      let payload: SettingsPayload;
      if (provider.preset_name) {
        payload = await updateModelConfiguration(token, {
          name: provider.preset_name,
          model: modelId || undefined,
          apiKey: apiKey || undefined,
          apiBase: providerForm.apiBase.trim() || undefined,
        });
      } else {
        payload = await updateProviderSettings(token, {
          provider: providerName,
          apiKey: apiKey || undefined,
          apiBase: providerForm.apiBase.trim(),
          apiType: providerForm.apiType,
        });
        if (modelId) {
          payload = await updateSettings(token, {
            provider: providerName,
            model: modelId,
          });
        }
      }
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setProviderForms((prev) => ({
        ...prev,
        [providerName]: {
          apiKey: "",
          apiBase: providerForm.apiBase.trim(),
          apiType: providerForm.apiType,
          model: modelId,
        },
      }));
      setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: false }));
      setEditingProviderKeys((prev) => ({ ...prev, [providerName]: false }));
      setProviderSaved((prev) => ({ ...prev, [providerName]: true }));
      setProviderModels((prev) => {
        if (!(providerName in prev)) return prev;
        const next = { ...prev };
        delete next[providerName];
        return next;
      });
      setExpandedProvider(null);
      setError(null);
      if (willQueryContext) {
        pollContextWindowLearning(modelId, providerName);
      } else {
        setLearningProvider(null);
        setTimeoutProvider(null);
        setContextWindowLearning(false);
      }
    } catch (err) {
      setError((err as Error).message);
      setLearningProvider(null);
      setTimeoutProvider(null);
      setContextWindowLearning(false);
    } finally {
      setProviderSaving(null);
    }
  }, [providerSaving, settings, providerForms, token, applyPayload, onModelNameChange, setPendingRestartSections, maybeRestartHostEngine, setError, pollContextWindowLearning, t]);

  const confirmDeleteProvider = useCallback(async () => {
    const providerName = providerToDelete;
    if (!providerName || providerDeleting) return;
    const provider = settings?.providers.find((item) => item.name === providerName);
    setProviderDeleting(true);
    try {
      const payload: SettingsPayload = provider?.preset_name
        ? await deleteModelConfiguration(token, provider.preset_name)
        : await deleteProviderSettings(token, providerName);
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      resetProviderDraft(providerName);
      setProviderSaved((prev) => ({ ...prev, [providerName]: false }));
      setExpandedProvider(null);
      setProviderToDelete(null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setProviderDeleting(false);
    }
  }, [providerToDelete, providerDeleting, settings, token, applyPayload, onModelNameChange, resetProviderDraft, setError]);

  const saveCustomConfiguration = useCallback(async () => {
    if (providerSaving) return;
    const providerForm = providerForms["custom"] ?? { apiKey: "", apiBase: "", apiType: "auto", model: "" };
    const label = customPresetLabel.trim();
    const apiKey = providerForm.apiKey.trim();
    const apiBase = providerForm.apiBase.trim();
    const model = providerForm.model.trim();
    if (!label) {
      setError(t("settings.byok.customLabelRequired", { defaultValue: "Label is required" }));
      return;
    }
    if (!apiBase) {
      setError(t("settings.byok.apiBaseRequired", { defaultValue: "API base is required" }));
      return;
    }
    if (!model) {
      setError(t("settings.byok.modelIdRequired", { defaultValue: "Model ID is required" }));
      return;
    }
    const matchingPreset = settings?.model_presets.find((p) => p.model === model);
    const targetStatus = matchingPreset?.resolved_context_window_status ?? "unknown";
    const willQueryContext = targetStatus === "unknown";
    if (willQueryContext) {
      learningPollCancelRef.current = true;
      setLearningProvider("custom");
      setTimeoutProvider(null);
      setContextWindowLearning(true);
      setContextWindowLearnTimeout(false);
    }
    setProviderSaving("custom");
    try {
      const payload: SettingsPayload = await createModelConfiguration(token, {
        label,
        provider: "custom",
        model,
        apiKey: apiKey || undefined,
        apiBase,
      });
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      setProviderForms((prev) => ({
        ...prev,
        custom: { apiKey: "", apiBase: "", apiType: "auto", model: "" },
      }));
      setCustomPresetLabel("");
      setVisibleProviderKeys((prev) => ({ ...prev, custom: false }));
      setProviderSaved((prev) => ({ ...prev, custom: false }));
      setExpandedProvider(null);
      setError(null);
      if (willQueryContext) {
        pollContextWindowLearning(model, "custom");
      } else {
        setLearningProvider(null);
        setTimeoutProvider(null);
        setContextWindowLearning(false);
      }
    } catch (err) {
      setError((err as Error).message);
      setLearningProvider(null);
      setTimeoutProvider(null);
      setContextWindowLearning(false);
    } finally {
      setProviderSaving(null);
    }
  }, [providerSaving, providerForms, customPresetLabel, settings, token, applyPayload, onModelNameChange, setError, pollContextWindowLearning, t]);

  const fetchProviderModelList = useCallback(async (providerName: string) => {
    if (providerModelsLoading) return;
    const provider = settings?.providers.find((item) => item.name === providerName);
    if (!provider) return;
    const providerForm = providerForms[providerName];
    const apiProviderName = provider.preset_name ? (provider.provider ?? providerName) : providerName;
    setProviderModelsLoading(providerName);
    try {
      const models = await fetchProviderModels(token, apiProviderName, {
        apiKey: providerForm?.apiKey.trim() || undefined,
        apiBase: providerForm?.apiBase.trim() || undefined,
      });
      setProviderModels((prev) => ({ ...prev, [providerName]: models }));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
      setProviderModels((prev) => ({ ...prev, [providerName]: [] }));
    } finally {
      setProviderModelsLoading(null);
    }
  }, [providerModelsLoading, settings, providerForms, token, setError]);

  const deleteAllProviders = useCallback(async () => {
    if (deletingAllProviders) return;
    setDeletingAllProviders(true);
    try {
      const payload: SettingsPayload = await deleteAllProvidersApi(token);
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      setProviderForms({});
      setProviderSaved({});
      setProviderModels({});
      setVisibleProviderKeys({});
      setEditingProviderKeys({});
      setExpandedProvider(null);
      setInlineAddModelProvider(null);
      setCustomConfigOpen(false);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setDeletingAllProviders(false);
    }
  }, [deletingAllProviders, token, applyPayload, onModelNameChange, setError]);

  return {
    form,
    setForm,
    saving,
    modelDirty,
    saveModelSettings,
    saveContextWindow,
    contextWindowLearning,
    learningProvider,
    timeoutProvider,
    contextWindowLearnTimeout,
    modelConfigurationOpen,
    setModelConfigurationOpen,
    modelConfigurationForm,
    setModelConfigurationForm,
    modelConfigurationSaving,
    openModelConfigurationDialog,
    handleCreateModelConfiguration,
    configuredModelProviderOptions,
    inlineAddModelProvider,
    inlineAddModelDraft,
    setInlineAddModelDraft,
    inlineAddModelModels,
    inlineAddModelModelsLoading,
    inlineAddModelSaving,
    openModelConfigurationForProvider,
    cancelInlineAddModel,
    saveInlineAddModel,
    fetchInlineAddModelModels,
    customConfigOpen,
    customConfigDraft,
    setCustomConfigDraft,
    customConfigModels,
    customConfigModelsLoading,
    customConfigSaving,
    openCustomConfig,
    cancelCustomConfig,
    saveCustomConfig,
    fetchCustomConfigModels,
    expandedProvider,
    setExpandedProvider,
    providerForms,
    visibleProviderKeys,
    editingProviderKeys,
    providerSaving,
    providerSaved,
    providerModels,
    providerModelsLoading,
    handleToggleProvider,
    toggleProviderKeyVisibility,
    toggleProviderKeyEditing,
    changeProviderForm,
    saveProvider,
    fetchProviderModelList,
    activateModelPreset,
    deletePreset,
    customPresetLabel,
    setCustomPresetLabel,
    saveCustomConfiguration,
    providerToDelete,
    setProviderToDelete,
    providerDeleting,
    confirmDeleteProvider,
    deleteAllProviders,
    deletingAllProviders,
  };
}
