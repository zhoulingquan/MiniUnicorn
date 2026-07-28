import { useEffect, useState } from "react";
import { Check, ChevronDown, Loader2, Plus, PlugZap, Upload } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
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
import { FormField } from "@/components/ui/form-field";
import { Input } from "@/components/ui/input";
import { LoadingSpinner } from "@/components/ui/loading-spinner";
import { RefreshIconButton } from "@/components/ui/refresh-icon-button";
import { Textarea } from "@/components/ui/textarea";
import { ViewShell } from "@/components/ui/view-shell";
import {
  fetchMcpPresets,
  runMcpPresetAction,
  saveCustomMcpServer,
  importMcpConfig,
  updateMcpServerTools,
} from "@/lib/api";
import {
  isMcpPresetsPayload,
  MCP_PRESETS_CHANGED_EVENT,
} from "@/lib/mcp-preset-events";
import type { McpPresetInfo, McpPresetsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

import { PresetCard } from "./components/PresetCard";
import { ServerCard } from "./components/ServerCard";

type TransportType = "stdio" | "sse" | "streamableHttp";

interface McpViewProps {
  onBack: () => void;
  token: string;
}

interface CustomServerForm {
  name: string;
  transport: TransportType;
  command: string;
  args: string;
  env: string;
  cwd: string;
  url: string;
  headers: string;
  tool_timeout: string;
  enabled_tools: string;
}

const EMPTY_FORM: CustomServerForm = {
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  env: "",
  cwd: "",
  url: "",
  headers: "",
  tool_timeout: "30",
  enabled_tools: "*",
};

export function McpView({ onBack, token }: McpViewProps) {
  const { t } = useTranslation();
  const [payload, setPayload] = useState<McpPresetsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<CustomServerForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Import modal state
  const [showImport, setShowImport] = useState(false);
  const [importText, setImportText] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);

  // Active tab: "all" | "connected"
  const [activeTab, setActiveTab] = useState<"all" | "connected">("all");

  // Preset inline form state
  const [expandedPreset, setExpandedPreset] = useState<string | null>(null);
  const [presetFormValues, setPresetFormValues] = useState<
    Record<string, Record<string, string>>
  >({});
  const [enablingPreset, setEnablingPreset] = useState<string | null>(null);
  const [presetError, setPresetError] = useState<string | null>(null);

  // Per-server state
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [testResults, setTestResults] = useState<
    Record<string, { ok: boolean; message: string; toolCount?: number }>
  >({});
  const [showTools, setShowTools] = useState<Record<string, boolean>>({});
  const [enabledToolsCache, setEnabledToolsCache] = useState<
    Record<string, string[]>
  >({});
  const [savingTools, setSavingTools] = useState<Record<string, boolean>>({});
  // 每个服务保存工具失败时返回的错误信息(按 server 名存储)
  const [toolsError, setToolsError] = useState<Record<string, string>>({});

  const loadPresets = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMcpPresets(token);
      setPayload(data);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadPresets();
  }, [token]);

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (isMcpPresetsPayload(detail)) setPayload(detail);
    };
    window.addEventListener(MCP_PRESETS_CHANGED_EVENT, handler);
    return () => window.removeEventListener(MCP_PRESETS_CHANGED_EVENT, handler);
  }, []);

  const handleRemove = async (name: string) => {
    setActing(name);
    try {
      const updated = await runMcpPresetAction(token, "remove", name, {});
      setPayload(updated);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setActing(null);
    }
  };

  const handleEnablePreset = async (server: McpPresetInfo) => {
    const fields = server.required_fields ?? [];
    if (fields.length > 0 && expandedPreset !== server.name) {
      setExpandedPreset(server.name);
      setPresetError(null);
      if (!presetFormValues[server.name]) {
        const init: Record<string, string> = {};
        fields.forEach((f) => (init[f.name] = ""));
        setPresetFormValues((prev) => ({ ...prev, [server.name]: init }));
      }
      return;
    }

    const values: Record<string, string> = {};
    if (fields.length > 0) {
      const formVals = presetFormValues[server.name] ?? {};
      const missing = fields.find(
        (f) => f.required && !(formVals[f.name] ?? "").trim(),
      );
      if (missing) {
        setPresetError(t("mcp.presetFields"));
        return;
      }
      fields.forEach((f) => {
        const v = (formVals[f.name] ?? "").trim();
        if (v) values[f.name] = v;
      });
    }

    setEnablingPreset(server.name);
    setPresetError(null);
    try {
      const updated = await runMcpPresetAction(token, "enable", server.name, values);
      setPayload(updated);
      setExpandedPreset(null);
      setPresetFormValues((prev) => {
        const next = { ...prev };
        delete next[server.name];
        return next;
      });
      // 启用后自动进行连接测试
      void handleTest(server.name);
    } catch (e) {
      setPresetError((e as Error).message);
    } finally {
      setEnablingPreset(null);
    }
  };

  const handleSaveCustom = async () => {
    if (!form.name.trim()) {
      setFormError(t("mcp.validation.nameRequired"));
      return;
    }
    if (form.transport === "stdio" && !form.command.trim()) {
      setFormError(t("mcp.validation.commandRequired"));
      return;
    }
    if ((form.transport === "sse" || form.transport === "streamableHttp") && !form.url.trim()) {
      setFormError(t("mcp.validation.urlRequired"));
      return;
    }

    if (form.args.trim()) {
      try {
        JSON.parse(form.args);
      } catch {
        setFormError(t("mcp.validation.invalidJsonArgs"));
        return;
      }
    }
    if (form.env.trim()) {
      try {
        JSON.parse(form.env);
      } catch {
        setFormError(t("mcp.validation.invalidJsonEnv"));
        return;
      }
    }
    if (form.headers.trim()) {
      try {
        JSON.parse(form.headers);
      } catch {
        setFormError(t("mcp.validation.invalidJsonHeaders"));
        return;
      }
    }

    setSaving(true);
    setFormError(null);
    try {
      const values: Record<string, string> = {
        name: form.name.trim(),
        transport: form.transport,
        tool_timeout: form.tool_timeout || "30",
      };
      if (form.transport === "stdio") {
        values.command = form.command.trim();
        if (form.args.trim()) values.args = form.args.trim();
        if (form.env.trim()) values.env = form.env.trim();
        if (form.cwd.trim()) values.cwd = form.cwd.trim();
      } else {
        values.url = form.url.trim();
        if (form.headers.trim()) values.headers = form.headers.trim();
      }
      if (form.enabled_tools.trim()) {
        values.enabled_tools = form.enabled_tools.trim();
      }

      const updated = await saveCustomMcpServer(token, values);
      setPayload(updated);
      setShowForm(false);
      setForm(EMPTY_FORM);
    } catch (e) {
      setFormError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const handleImport = async () => {
    if (!importText.trim()) {
      // 导入内容为空时给出对应提示,避免误显示"headers JSON 无效"
      setImportError(t("mcp.validation.importEmpty"));
      return;
    }
    setImporting(true);
    setImportError(null);
    try {
      const updated = await importMcpConfig(token, importText);
      setPayload(updated);
      setShowImport(false);
      setImportText("");
    } catch (e) {
      setImportError((e as Error).message);
    } finally {
      setImporting(false);
    }
  };

  const handleTest = async (name: string) => {
    setTesting((prev) => ({ ...prev, [name]: true }));
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[name];
      return next;
    });
    try {
      const updated = await runMcpPresetAction(token, "test", name, {});
      setPayload(updated);
      const server = updated.presets.find((s) => s.name === name);
      if (server) {
        if (server.status === "configured" && server.tool_count != null) {
          setTestResults((prev) => ({
            ...prev,
            [name]: {
              ok: true,
              message: t("mcp.testSuccess", { count: server.tool_count }),
              toolCount: server.tool_count,
            },
          }));
        } else if (server.status === "configured") {
          setTestResults((prev) => ({
            ...prev,
            [name]: { ok: true, message: t("mcp.testOk") },
          }));
        } else {
          setTestResults((prev) => ({
            ...prev,
            [name]: {
              ok: false,
              message: server.error || t("mcp.testFailed"),
            },
          }));
        }
      }
    } catch (e) {
      setTestResults((prev) => ({
        ...prev,
        [name]: { ok: false, message: (e as Error).message },
      }));
    } finally {
      setTesting((prev) => ({ ...prev, [name]: false }));
    }
  };

  const toggleToolsPanel = (server: McpPresetInfo) => {
    const name = server.name;
    setShowTools((prev) => ({ ...prev, [name]: !prev[name] }));
    if (!enabledToolsCache[name]) {
      const current =
        server.enabled_tools && server.enabled_tools.length > 0
          ? server.enabled_tools.includes("*") && (server.tool_names?.length ?? 0) > 0
            ? [...(server.tool_names ?? [])]
            : [...server.enabled_tools]
          : [];
      setEnabledToolsCache((prev) => ({ ...prev, [name]: current }));
    }
  };

  const handleToggleTool = (name: string, tool: string) => {
    setEnabledToolsCache((prev) => {
      const current = prev[name] ?? [];
      const next = current.includes(tool)
        ? current.filter((item) => item !== tool)
        : [...current, tool];
      return { ...prev, [name]: next };
    });
  };

  const handleSelectAllTools = (name: string, tools: string[]) => {
    setEnabledToolsCache((prev) => ({ ...prev, [name]: [...tools] }));
  };

  const handleClearTools = (name: string) => {
    setEnabledToolsCache((prev) => ({ ...prev, [name]: [] }));
  };

  const handleSaveTools = async (name: string) => {
    const tools = enabledToolsCache[name] ?? [];
    setSavingTools((prev) => ({ ...prev, [name]: true }));
    // 进入保存流程时清除上一次的错误信息
    setToolsError((prev) => {
      const next = { ...prev };
      delete next[name];
      return next;
    });
    try {
      const updated = await updateMcpServerTools(token, name, tools);
      setPayload(updated);
      setShowTools((prev) => ({ ...prev, [name]: false }));
    } catch (e) {
      // 记录错误并就近在工具面板提示用户,避免静默吞错
      console.error("Failed to save MCP tools for", name, e);
      setToolsError((prev) => ({ ...prev, [name]: (e as Error).message }));
    } finally {
      setSavingTools((prev) => ({ ...prev, [name]: false }));
    }
  };

  const allServers = payload?.presets ?? [];
  const connectedServers = allServers.filter(
    (s) => s.installed || s.status === "configured",
  );
  const installed = connectedServers.filter((s) => s.status === "configured");

  const renderServerCard = (server: McpPresetInfo) => (
    <ServerCard
      key={server.name}
      server={server}
      acting={acting}
      testing={testing[server.name] ?? false}
      testResult={testResults[server.name]}
      showTools={showTools[server.name] ?? false}
      enabledToolsCache={enabledToolsCache[server.name] ?? null}
      savingTools={savingTools[server.name] ?? false}
      toolsError={toolsError[server.name] ?? null}
      onRemove={handleRemove}
      onTest={() => handleTest(server.name)}
      onToggleTools={() => toggleToolsPanel(server)}
      onToggleTool={(tool) => handleToggleTool(server.name, tool)}
      onSelectAll={() =>
        handleSelectAllTools(server.name, server.tool_names ?? [])
      }
      onClear={() => handleClearTools(server.name)}
      onSaveTools={() => handleSaveTools(server.name)}
    />
  );

  return (
    <ViewShell
      onBack={onBack}
      icon={<PlugZap className="h-4 w-4 text-foreground/80" />}
      title={t("mcp.title")}
      actions={<RefreshIconButton onClick={loadPresets} loading={loading} />}
      bodyClassName="flex flex-col p-0"
    >
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {loading && !payload ? (
          <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
            <LoadingSpinner />
            {t("mcp.loading")}
          </div>
        ) : error ? (
          <div className="flex flex-col items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
            <p>{error}</p>
            <Button variant="outline" size="sm" onClick={loadPresets}>
              {t("mcp.retry")}
            </Button>
          </div>
        ) : (
          <div className="flex h-full flex-col">
            {/* Tab switcher */}
            <div className="mb-3 flex w-fit items-center gap-0.5 rounded-md bg-muted/40 p-0.5">
              <button
                type="button"
                onClick={() => setActiveTab("all")}
                className={cn(
                  "rounded px-2.5 py-1 text-xs font-medium transition-colors",
                  activeTab === "all"
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground/70 hover:text-foreground",
                )}
              >
                {t("mcp.allServices")}
                <span className="ml-1 text-[10px] text-muted-foreground/50">
                  ({allServers.length})
                </span>
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("connected")}
                className={cn(
                  "rounded px-2.5 py-1 text-xs font-medium transition-colors",
                  activeTab === "connected"
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground/70 hover:text-foreground",
                )}
              >
                {t("mcp.connected")}
                <span className="ml-1 text-[10px] text-muted-foreground/50">
                  ({connectedServers.length})
                </span>
              </button>
            </div>

            {/* All services tab */}
            {activeTab === "all" && (
              <div className="space-y-2">
                {allServers.length === 0 ? (
                  <p className="py-8 text-center text-sm text-muted-foreground/50">
                    {t("mcp.noPresets")}
                  </p>
                ) : (
                  <div className="grid grid-cols-4 gap-1.5">
                    {allServers.map((server) => {
                      const isConnected = server.installed || server.status === "configured";
                      return isConnected ? (
                        renderServerCard(server)
                      ) : (
                        <PresetCard
                          key={server.name}
                          server={server}
                          expanded={expandedPreset === server.name}
                          formValues={presetFormValues[server.name] ?? {}}
                          enabling={enablingPreset === server.name}
                          error={expandedPreset === server.name ? presetError : null}
                          onEnable={() => handleEnablePreset(server)}
                          onRemove={() => handleRemove(server.name)}
                          onFieldChange={(field, value) =>
                            setPresetFormValues((prev) => ({
                              ...prev,
                              [server.name]: {
                                ...(prev[server.name] ?? {}),
                                [field]: value,
                              },
                            }))
                          }
                          onCancel={() => {
                            setExpandedPreset(null);
                            setPresetError(null);
                            setPresetFormValues((prev) => {
                              const next = { ...prev };
                              delete next[server.name];
                              return next;
                            });
                          }}
                        />
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            {/* Connected services tab */}
            {activeTab === "connected" && (
              <div className="space-y-2">
                {connectedServers.length === 0 ? (
                  <p className="py-8 text-center text-sm text-muted-foreground/50">
                    {t("mcp.empty")}
                  </p>
                ) : (
                  <div className="grid grid-cols-4 gap-1.5">
                    {connectedServers.map((server) => renderServerCard(server))}
                  </div>
                )}
              </div>
            )}

            {/* Add custom server form — moved to Dialog at end of component */}
          </div>
        )}
      </div>

      <div className="border-t px-4 py-2.5">
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground/70">
            {installed.length} {t("mcp.connected").toLowerCase()}
          </span>
          <div className="flex items-center gap-1.5">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 px-2 text-[11px] text-muted-foreground/70 hover:text-foreground"
              onClick={() => {
                setShowImport(true);
                setImportError(null);
                setImportText("");
              }}
            >
              <Upload className="h-3.5 w-3.5" />
              {t("mcp.importConfig")}
            </Button>
            <Button
              size="sm"
              className="h-7 gap-1 px-2 text-[11px]"
              onClick={() => setShowForm(true)}
            >
              <Plus className="h-3.5 w-3.5" />
              {t("mcp.addServer")}
            </Button>
          </div>
        </div>
      </div>

      {/* Import config modal */}
      <Dialog open={showImport} onOpenChange={setShowImport}>
        <DialogContent className="max-w-lg" showCloseButton={false}>
          <DialogHeader>
            <DialogTitle className="text-sm">{t("mcp.importConfig")}</DialogTitle>
          </DialogHeader>
          <Textarea
            placeholder={t("mcp.importPlaceholder")}
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
            className="min-h-[160px] text-[12px] font-mono"
          />
          {importError && (
            <p className="mt-2 text-[11px] text-destructive">{importError}</p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setShowImport(false);
                setImportText("");
                setImportError(null);
              }}
            >
              {t("mcp.form.cancel")}
            </Button>
            <Button
              disabled={importing || !importText.trim()}
              onClick={handleImport}
            >
              {importing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Upload className="h-3 w-3" />}
              {t("mcp.import")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 添加自定义 MCP 服务弹窗（与 SkillsView 编辑弹窗样式一致） */}
      <Dialog open={showForm} onOpenChange={(o) => setShowForm(o)}>
        <DialogContent className="max-w-3xl" showCloseButton={false}>
          <DialogHeader>
            <DialogTitle className="text-sm">{t("mcp.addServer")}</DialogTitle>
          </DialogHeader>
          <div className="space-y-2">
            <FormField label={t("mcp.form.name")} required>
              <Input
                placeholder={t("mcp.form.namePlaceholder")}
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                className="h-8 text-[12.5px]"
              />
            </FormField>

            <FormField label={t("mcp.form.transport")} required>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="outline"
                    className="h-8 w-full justify-between text-[12.5px] font-normal"
                  >
                    {form.transport}
                    <ChevronDown className="h-3.5 w-3.5 opacity-50" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent className="min-w-[160px]">
                  {(["stdio", "sse", "streamableHttp"] as TransportType[]).map((tp) => (
                    <DropdownMenuItem
                      key={tp}
                      onClick={() => setForm((f) => ({ ...f, transport: tp }))}
                      className="text-[12.5px]"
                    >
                      {tp}
                      {form.transport === tp && <Check className="ml-auto h-3 w-3" />}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            </FormField>

            {form.transport === "stdio" ? (
              <>
                <FormField label={t("mcp.form.command")} required>
                  <Input
                    placeholder="npx"
                    value={form.command}
                    onChange={(e) => setForm((f) => ({ ...f, command: e.target.value }))}
                    className="h-8 text-[12.5px] font-mono"
                  />
                </FormField>
                <FormField label={t("mcp.form.args")}>
                  <Textarea
                    placeholder='["-y", "@playwright/mcp@latest"]'
                    value={form.args}
                    onChange={(e) => setForm((f) => ({ ...f, args: e.target.value }))}
                    className="min-h-[56px] text-[12px] font-mono"
                  />
                </FormField>
                <FormField label={t("mcp.form.env")}>
                  <Textarea
                    placeholder='{"API_KEY": "sk-..."}'
                    value={form.env}
                    onChange={(e) => setForm((f) => ({ ...f, env: e.target.value }))}
                    className="min-h-[56px] text-[12px] font-mono"
                  />
                </FormField>
                <FormField label={t("mcp.form.cwd")}>
                  <Input
                    placeholder="/path/to/working/dir"
                    value={form.cwd}
                    onChange={(e) => setForm((f) => ({ ...f, cwd: e.target.value }))}
                    className="h-8 text-[12.5px] font-mono"
                  />
                </FormField>
              </>
            ) : (
              <>
                <FormField label={t("mcp.form.url")} required>
                  <Input
                    placeholder="https://example.com/mcp"
                    value={form.url}
                    onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))}
                    className="h-8 text-[12.5px] font-mono"
                  />
                </FormField>
                <FormField label={t("mcp.form.headers")}>
                  <Textarea
                    placeholder='{"Authorization": "Bearer ..."}'
                    value={form.headers}
                    onChange={(e) => setForm((f) => ({ ...f, headers: e.target.value }))}
                    className="min-h-[56px] text-[12px] font-mono"
                  />
                </FormField>
              </>
            )}

            <FormField label={t("mcp.form.toolTimeout")}>
              <Input
                type="number"
                min={5}
                max={600}
                value={form.tool_timeout}
                onChange={(e) => setForm((f) => ({ ...f, tool_timeout: e.target.value }))}
                className="h-8 w-24 text-[12.5px]"
              />
            </FormField>

            <FormField label={t("mcp.form.enabledTools")}>
              <Input
                placeholder="*"
                value={form.enabled_tools}
                onChange={(e) => setForm((f) => ({ ...f, enabled_tools: e.target.value }))}
                className="h-8 text-[12.5px]"
              />
            </FormField>

            {formError && (
              <p className="text-[11px] text-destructive">{formError}</p>
            )}
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setShowForm(false);
                setForm(EMPTY_FORM);
                setFormError(null);
              }}
            >
              {t("mcp.form.cancel")}
            </Button>
            <Button
              disabled={saving}
              onClick={handleSaveCustom}
            >
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              {t("mcp.form.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ViewShell>
  );
}
