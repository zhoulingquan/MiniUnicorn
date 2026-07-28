import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Loader2,
  Package,
  Plus,
  Trash2,
  Upload,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { LoadingSpinner } from "@/components/ui/loading-spinner";
import { RefreshIconButton } from "@/components/ui/refresh-icon-button";
import { ViewShell } from "@/components/ui/view-shell";
import { deleteTool, fetchTools, importToolFile } from "@/lib/api";
import { pickColorByName as pickToolColor } from "@/lib/pick-color";
import type { ToolPayload, ToolsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 工具卡片描述的 i18n helper。
 * 查找顺序：tools.toolDescriptions.{name} → 后端 tool.description */
function useToolDescription() {
  const { t } = useTranslation();
  return (tool: ToolPayload): string => {
    const translated = t(`tools.toolDescriptions.${tool.name}`, {
      defaultValue: "",
    });
    return translated || tool.description;
  };
}

interface ToolsViewProps {
  onBack: () => void;
  token: string;
}

export function ToolsView({ onBack, token }: ToolsViewProps) {
  const { t } = useTranslation();
  const [payload, setPayload] = useState<ToolsPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [importName, setImportName] = useState("");
  const [importContent, setImportContent] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importSuccess, setImportSuccess] = useState<string | null>(null);
  const [actingName, setActingName] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchTools(token);
      setPayload(data);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  // Initial load
  useEffect(() => {
    void load();
  }, [load]);

  const handleFileSelect = async (file: File) => {
    setImportError(null);
    setImportSuccess(null);
    if (!file.name.endsWith(".py")) {
      setImportError(t("tools.error.notPy"));
      return;
    }
    try {
      const text = await file.text();
      setImportContent(text);
      setImportName(file.name);
    } catch {
      setImportError(t("tools.error.readFailed"));
    }
  };

  const handleImport = async () => {
    if (importing) return;
    const name = importName.trim();
    if (!name) {
      setImportError(t("tools.error.nameRequired"));
      return;
    }
    if (!name.endsWith(".py")) {
      setImportError(t("tools.error.notPy"));
      return;
    }
    if (!importContent.trim()) {
      setImportError(t("tools.error.emptyContent"));
      return;
    }
    setImporting(true);
    setImportError(null);
    setImportSuccess(null);
    try {
      const result = await importToolFile(token, name, importContent);
      setImportSuccess(result.message || t("tools.importSuccess"));
      setImportName("");
      setImportContent("");
      setShowImport(false);
      await load();
    } catch (e) {
      setImportError((e as Error).message);
    } finally {
      setImporting(false);
    }
  };

  const handleDelete = async (tool: ToolPayload) => {
    if (actingName) return;
    if (tool.source !== "user") return;
    setActingName(tool.name);
    try {
      await deleteTool(token, tool.name);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setActingName(null);
    }
  };

  const tools = payload?.tools ?? [];
  const builtinTools = tools.filter((t) => t.source === "builtin");
  const userTools = tools.filter((t) => t.source === "user");

  return (
    <ViewShell
      onBack={onBack}
      icon={<Package className="h-4 w-4 text-foreground/80" />}
      title={t("tools.title")}
      actions={
        <>
          <RefreshIconButton onClick={load} loading={loading} />
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1 px-2 text-[11px]"
            onClick={() => {
              setShowImport((v) => !v);
              setImportName("");
              setImportContent("");
              setImportError(null);
              setImportSuccess(null);
            }}
          >
            <Plus className="h-3.5 w-3.5" />
            {t("tools.import")}
          </Button>
        </>
      }
    >
      {error ? (
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
          <p>{error}</p>
          <Button variant="outline" size="sm" onClick={load}>
            {t("tools.retry")}
          </Button>
        </div>
      ) : loading && !payload ? (
        <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
          <LoadingSpinner />
          {t("tools.loading")}
        </div>
      ) : (
        <div className="flex flex-col gap-3">

          {userTools.length > 0 ? (
            <ToolSection
              title={t("tools.userTools")}
              tools={userTools}
              actingName={actingName}
              onDelete={handleDelete}
            />
          ) : null}

          {builtinTools.length > 0 ? (
            <ToolSection
              title={t("tools.builtinTools")}
              tools={builtinTools}
              actingName={actingName}
              onDelete={handleDelete}
            />
          ) : null}

          {tools.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
              <Package className="h-8 w-8 opacity-40" />
              <p>{t("tools.empty")}</p>
            </div>
          ) : null}
        </div>
      )}

      {/* 导入工具弹窗（与 SkillsView 编辑弹窗样式一致） */}
      <Dialog open={showImport} onOpenChange={(o) => setShowImport(o)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="text-sm">{t("tools.importTitle")}</DialogTitle>
          </DialogHeader>
          <ImportForm
            name={importName}
            content={importContent}
            onNameChange={setImportName}
            onContentChange={setImportContent}
            onFileSelect={handleFileSelect}
            onSave={handleImport}
            onCancel={() => {
              setShowImport(false);
              setImportName("");
              setImportContent("");
              setImportError(null);
              setImportSuccess(null);
            }}
            saving={importing}
            error={importError}
            success={importSuccess}
          />
        </DialogContent>
      </Dialog>
    </ViewShell>
  );
}

interface ToolSectionProps {
  title: string;
  tools: ToolPayload[];
  actingName: string | null;
  onDelete: (tool: ToolPayload) => void;
}

function ToolSection({ title, tools, actingName, onDelete }: ToolSectionProps) {
  return (
    <section className="flex flex-col gap-1.5">
      <h2 className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70">
        {title} ({tools.length})
      </h2>
      <div className="grid grid-cols-4 gap-1.5">
        {tools.map((tool) => (
          <ToolCard
            key={`${tool.source}-${tool.name}`}
            tool={tool}
            acting={actingName === tool.name}
            onDelete={() => onDelete(tool)}
          />
        ))}
      </div>
    </section>
  );
}

interface ToolCardProps {
  tool: ToolPayload;
  acting: boolean;
  onDelete: () => void;
}

function ToolCard({ tool, acting, onDelete }: ToolCardProps) {
  // 子组件直接调用 useTranslation,保留 i18next 的类型推断
  const { t } = useTranslation();
  const getToolDescription = useToolDescription();
  const sourceLabel =
    tool.source === "user" ? t("tools.badge.user") : t("tools.badge.builtin");

  const iconColor = pickToolColor(tool.name);

  return (
    <div
      className={cn(
        "group flex flex-col transition-colors",
        "rounded-lg border px-2.5 py-2",
        tool.loaded
          ? "border-border/60 bg-background hover:border-violet-500/40"
          : "border-amber-500/30 bg-amber-500/[0.03]",
      )}
    >
      <div className="flex items-start gap-2">
        <div
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-[10px] font-bold text-white"
          style={{ backgroundColor: iconColor }}
        >
          {tool.name.charAt(0).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1">
            <span className="truncate text-sm font-medium leading-tight" title={tool.name}>
              {tool.name}
            </span>
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1">
            <span
              className={cn(
                "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                tool.source === "user"
                  ? "bg-blue-500/10 text-blue-600 dark:text-blue-400"
                  : "bg-muted text-muted-foreground",
              )}
            >
              {sourceLabel}
            </span>
            {!tool.loaded ? (
              <span className="shrink-0 rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-600 dark:text-amber-400">
                {t("tools.badge.notLoaded")}
              </span>
            ) : null}
            {tool.read_only ? (
              <span className="shrink-0 rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
                {t("tools.badge.readOnly")}
              </span>
            ) : null}
          </div>
        </div>
        {tool.source === "user" ? (
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-muted-foreground hover:text-red-600 hover:bg-red-500/10"
            onClick={onDelete}
            disabled={acting}
            title={t("tools.delete")}
          >
            {acting ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Trash2 className="h-3 w-3" />
            )}
          </Button>
        ) : null}
      </div>

      <p className="mt-1.5 line-clamp-2 text-xs leading-snug text-muted-foreground">
        {getToolDescription(tool) || t("tools.noDescription")}
      </p>
    </div>
  );
}

interface ImportFormProps {
  name: string;
  content: string;
  onNameChange: (v: string) => void;
  onContentChange: (v: string) => void;
  onFileSelect: (file: File) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  error: string | null;
  success: string | null;
}

function ImportForm({
  name,
  content,
  onNameChange,
  onContentChange,
  onFileSelect,
  onSave,
  onCancel,
  saving,
  error,
  success,
}: ImportFormProps) {
  // 子组件直接调用 useTranslation,保留 i18next 的类型推断
  const { t } = useTranslation();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-1.5">
        <label className="mb-1 block text-[11px] font-medium text-muted-foreground">
          {t("tools.field.filename")}
        </label>
        <div className="flex items-center gap-2">
          <Input
            value={name}
            onChange={(e) => onNameChange(e.target.value)}
            placeholder="my_tool.py"
            className="h-8 text-xs"
          />
          <Button
            variant="outline"
            size="sm"
            className="h-8 gap-1.5"
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload className="h-3.5 w-3.5" />
            {t("tools.field.chooseFile")}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".py"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onFileSelect(file);
              e.target.value = "";
            }}
          />
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="mb-1 block text-[11px] font-medium text-muted-foreground">
          {t("tools.field.content")}
        </label>
        <textarea
          value={content}
          onChange={(e) => onContentChange(e.target.value)}
          placeholder={t("tools.field.contentPlaceholder")}
          rows={10}
          className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 font-mono text-[11px] leading-relaxed"
        />
      </div>

      <div className="rounded-md bg-muted/50 px-3 py-2 text-[11px] text-muted-foreground">
        {t("tools.field.hint")}
      </div>

      {error ? (
        <div className="flex items-center gap-2 rounded-md bg-destructive/10 px-3 py-2 text-[11px] text-destructive">
          <AlertCircle className="h-3.5 w-3.5" />
          {error}
        </div>
      ) : null}

      {success ? (
        <div className="flex items-center gap-2 rounded-md bg-green-500/10 px-3 py-2 text-[11px] text-green-600 dark:text-green-400">
          <AlertCircle className="h-3.5 w-3.5" />
          {success}
        </div>
      ) : null}

      <DialogFooter>
        <Button variant="outline" onClick={onCancel} disabled={saving}>
          {t("tools.cancel")}
        </Button>
        <Button onClick={onSave} disabled={saving}>
          {saving ? (
            <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />
          ) : null}
          {t("tools.save")}
        </Button>
      </DialogFooter>
    </div>
  );
}
