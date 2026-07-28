import { useCallback, useEffect, useState } from "react";
import {
  Loader2,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
  Users,
  Wrench,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ErrorBanner, NoticeBanner } from "@/components/ui/banner";
import { FormField } from "@/components/ui/form-field";
import { IconButton } from "@/components/ui/icon-button";
import { LoadingSpinner } from "@/components/ui/loading-spinner";
import { RefreshIconButton } from "@/components/ui/refresh-icon-button";
import { ResourceDeleteConfirmDialog } from "@/components/ui/resource-delete-confirm-dialog";
import { ViewShell } from "@/components/ui/view-shell";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  deleteAgent,
  fetchAgents,
  generateAgent,
  readAgent,
  saveAgent,
} from "@/lib/api";
import type { AgentInfo } from "@/lib/types";

// Default system prompt is sourced from i18n (agents.defaultSystemPrompt).

interface AgentsViewProps {
  onBack: () => void;
  token: string;
  /** Called when the user picks "use this agent" — closes the view and starts a chat. */
  onUseAgent?: (agentId: string) => void;
}

interface CreateForm {
  name: string;
  description: string;
  model: string;
  tools: string;
  systemPrompt: string;
}

const EMPTY_FORM: CreateForm = {
  name: "",
  description: "",
  model: "",
  tools: "",
  systemPrompt: "",
};

/** Assemble .md file content (YAML frontmatter + body) from form fields.
 *  fallbackPrompt is the i18n default system prompt, used when the field is empty. */
function buildAgentMarkdown(form: CreateForm, fallbackPrompt: string): string {
  const lines: string[] = ["---"];
  lines.push(`name: ${form.name.trim()}`);
  lines.push(`description: ${form.description.trim()}`);
  if (form.model.trim()) {
    lines.push(`model: ${form.model.trim()}`);
  }
  if (form.tools.trim()) {
    lines.push(`tools: ${form.tools.trim()}`);
  }
  lines.push("---", "");
  lines.push(form.systemPrompt.trim() || fallbackPrompt);
  return lines.join("\n");
}

export function AgentsView({ onBack, token, onUseAgent }: AgentsViewProps) {
  const { t } = useTranslation();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentInfo | null>(null);

  // Create dialog (form-based)
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CreateForm>(EMPTY_FORM);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Edit dialog (raw .md editor, like skills)
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorName, setEditorName] = useState("");
  const [editorContent, setEditorContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [editorError, setEditorError] = useState<string | null>(null);

  // Generate dialog (AI-powered)
  const [generateOpen, setGenerateOpen] = useState(false);
  const [generateDescription, setGenerateDescription] = useState("");
  const [generateContent, setGenerateContent] = useState("");
  const [generateName, setGenerateName] = useState("");
  const [generating, setGenerating] = useState(false);
  const [generateSaving, setGenerateSaving] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [generateStage, setGenerateStage] = useState<"input" | "preview">("input");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchAgents(token);
      setAgents(data.agents);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setCreateForm({ ...EMPTY_FORM, systemPrompt: t("agents.defaultSystemPrompt") });
    setCreateError(null);
    setCreateOpen(true);
  };

  const handleCreate = async () => {
    const name = createForm.name.trim();
    if (!name) {
      setCreateError(t("agents.nameLabel") + " " + t("agents.namePlaceholder"));
      return;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) {
      setCreateError(t("agents.nameHelp"));
      return;
    }
    if (!createForm.description.trim()) {
      setCreateError(t("agents.descriptionField") + " " + t("agents.namePlaceholder"));
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const content = buildAgentMarkdown(createForm, t("agents.defaultSystemPrompt"));
      await saveAgent(token, name, content);
      setCreateOpen(false);
      setNotice(t("agents.saved"));
      await load();
    } catch (e) {
      setCreateError(`${t("agents.saveError")}: ${(e as Error).message}`);
    } finally {
      setCreating(false);
    }
  };

  const openEditor = async (agent: AgentInfo) => {
    setEditorError(null);
    setEditorName(agent.name);
    setEditorContent("");
    setEditorOpen(true);
    try {
      const data = await readAgent(token, agent.name);
      setEditorContent(data.content);
    } catch (e) {
      setEditorError((e as Error).message);
    }
  };

  const handleSave = async () => {
    const name = editorName.trim();
    if (!name) {
      setEditorError(t("agents.nameLabel") + " " + t("agents.namePlaceholder"));
      return;
    }
    if (!editorContent.trim()) {
      setEditorError(t("agents.editorPlaceholder"));
      return;
    }
    setSaving(true);
    setEditorError(null);
    try {
      await saveAgent(token, name, editorContent);
      setEditorOpen(false);
      setNotice(t("agents.saved"));
      await load();
    } catch (e) {
      setEditorError(`${t("agents.saveError")}: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    try {
      await deleteAgent(token, deleteTarget.name);
      setDeleteTarget(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
      setDeleteTarget(null);
    }
  };

  const openGenerate = () => {
    setGenerateDescription("");
    setGenerateContent("");
    setGenerateName("");
    setGenerateError(null);
    setGenerateStage("input");
    setGenerateOpen(true);
  };

  const handleGenerate = async () => {
    const description = generateDescription.trim();
    if (!description) {
      setGenerateError(t("agents.generateDescription"));
      return;
    }
    setGenerating(true);
    setGenerateError(null);
    try {
      const result = await generateAgent(description, token);
      setGenerateContent(result.content);
      setGenerateName(result.name);
      setGenerateStage("preview");
    } catch (e) {
      setGenerateError(`${t("agents.saveError")}: ${(e as Error).message}`);
    } finally {
      setGenerating(false);
    }
  };

  const handleGenerateSave = async () => {
    const name = generateName.trim();
    if (!name || !generateContent.trim()) {
      setGenerateError(t("agents.editorPlaceholder"));
      return;
    }
    setGenerateSaving(true);
    setGenerateError(null);
    try {
      await saveAgent(token, name, generateContent);
      setGenerateOpen(false);
      setNotice(t("agents.saved"));
      await load();
    } catch (e) {
      setGenerateError(`${t("agents.saveError")}: ${(e as Error).message}`);
    } finally {
      setGenerateSaving(false);
    }
  };

  return (
    <ViewShell
      onBack={onBack}
      icon={<Users className="h-4 w-4 text-foreground/80" />}
      title={t("agents.title")}
      actions={
        <>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2 text-[11px]"
            onClick={openGenerate}
          >
            <Sparkles className="h-3 w-3 text-sky-500" />
            {t("agents.generate")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2 text-[11px]"
            onClick={openCreate}
          >
            <Plus className="h-3 w-3" />
            {t("agents.create")}
          </Button>
          <RefreshIconButton onClick={load} loading={loading} />
        </>
      }
    >
      {notice && (
        <NoticeBanner className="mb-3">{notice}</NoticeBanner>
      )}
      {error && (
        <ErrorBanner className="mb-3">{error}</ErrorBanner>
      )}

      {loading && agents.length === 0 ? (
        <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
          <LoadingSpinner />
          {t("agents.loading")}
        </div>
      ) : agents.length === 0 ? (
        <div className="py-12 text-center text-sm text-muted-foreground">
          {t("agents.empty")}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
          {agents.map((agent) => (
            <AgentCard
              key={agent.name}
              agent={agent}
              onEdit={() => openEditor(agent)}
              onDelete={() => setDeleteTarget(agent)}
              onUseAgent={onUseAgent}
            />
          ))}
        </div>
      )}

      {/* Create dialog (form-based) */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="text-sm">{t("agents.create")}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <FormField label={t("agents.nameLabel")} required>
              <Input
                value={createForm.name}
                onChange={(e) => setCreateForm((f) => ({ ...f, name: e.target.value }))}
                placeholder={t("agents.namePlaceholder")}
                className="h-8 text-xs"
              />
              <p className="mt-1 text-[10px] text-muted-foreground/60">{t("agents.nameHelp")}</p>
            </FormField>
            <FormField label={t("agents.descriptionField")} required>
              <Input
                value={createForm.description}
                onChange={(e) => setCreateForm((f) => ({ ...f, description: e.target.value }))}
                placeholder={t("agents.descriptionPlaceholder")}
                className="h-8 text-xs"
              />
            </FormField>
            <div className="grid grid-cols-2 gap-3">
              <FormField label={t("agents.model")}>
                <Input
                  value={createForm.model}
                  onChange={(e) => setCreateForm((f) => ({ ...f, model: e.target.value }))}
                  placeholder={t("agents.modelPlaceholder")}
                className="h-8 text-xs"
                />
              </FormField>
              <FormField label={t("agents.tools")}>
                <Input
                  value={createForm.tools}
                  onChange={(e) => setCreateForm((f) => ({ ...f, tools: e.target.value }))}
                  placeholder={t("agents.toolsPlaceholder")}
                className="h-8 text-xs"
                />
              </FormField>
            </div>
            <FormField label={t("agents.systemPrompt")}>
              <Textarea
                value={createForm.systemPrompt}
                onChange={(e) => setCreateForm((f) => ({ ...f, systemPrompt: e.target.value }))}
                placeholder={t("agents.systemPromptPlaceholder")}
                className="min-h-[160px] font-mono text-[11px] leading-relaxed"
              />
            </FormField>
            {createError && (
              <p className="text-[11px] text-destructive">{createError}</p>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              {t("agents.cancel")}
            </Button>
            <Button onClick={handleCreate} disabled={creating}>
              {creating ? <Loader2 className="mr-1.5 h-3 w-3 animate-spin" /> : null}
              {creating ? t("agents.saving") : t("agents.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit dialog (raw .md editor) */}
      <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-sm">
              <Users className="h-4 w-4 text-sky-500" />
              {t("agents.edit")} · {editorName}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-2">
            <Textarea
              value={editorContent}
              onChange={(e) => setEditorContent(e.target.value)}
              placeholder={t("agents.editorPlaceholder")}
              className="min-h-[360px] font-mono text-[11px] leading-relaxed"
            />
            {editorError && (
              <p className="text-[11px] text-destructive">{editorError}</p>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditorOpen(false)}>
              {t("agents.cancel")}
            </Button>
            <Button onClick={handleSave} disabled={saving}>
              {saving ? <Loader2 className="mr-1.5 h-3 w-3 animate-spin" /> : null}
              {saving ? t("agents.saving") : t("agents.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Generate dialog (AI-powered) */}
      <Dialog open={generateOpen} onOpenChange={setGenerateOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-sm">
              <Sparkles className="h-4 w-4 text-sky-500" />
              {t("agents.generate")}
            </DialogTitle>
          </DialogHeader>
          {generateStage === "input" ? (
            <div className="space-y-2">
              <Textarea
                value={generateDescription}
                onChange={(e) => setGenerateDescription(e.target.value)}
                placeholder={t("agents.generateDescription")}
                className="min-h-[140px] text-[12px] leading-relaxed"
                disabled={generating}
              />
              {generateError && (
                <p className="text-[11px] text-destructive">{generateError}</p>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              <Textarea
                value={generateContent}
                onChange={(e) => setGenerateContent(e.target.value)}
                placeholder={t("agents.editorPlaceholder")}
                className="min-h-[320px] font-mono text-[11px] leading-relaxed"
              />
              {generateError && (
                <p className="text-[11px] text-destructive">{generateError}</p>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setGenerateOpen(false)}>
              {t("agents.cancel")}
            </Button>
            {generateStage === "input" ? (
              <Button onClick={handleGenerate} disabled={generating}>
                {generating ? <Loader2 className="mr-1.5 h-3 w-3 animate-spin" /> : <Sparkles className="mr-1.5 h-3 w-3" />}
                {generating ? t("agents.generateLoading") : t("agents.generate")}
              </Button>
            ) : (
              <Button onClick={handleGenerateSave} disabled={generateSaving}>
                {generateSaving ? <Loader2 className="mr-1.5 h-3 w-3 animate-spin" /> : null}
                {generateSaving ? t("agents.saving") : t("agents.save")}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ResourceDeleteConfirmDialog
        open={deleteTarget !== null}
        resourceName={deleteTarget?.name ?? ""}
        icon={Users}
        titleKey="agents.deleteDialogTitle"
        descriptionKey="agents.deleteDescription"
        cancelKey="agents.deleteCancel"
        confirmKey="agents.deleteConfirm"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={handleDeleteConfirm}
      />
    </ViewShell>
  );
}

/* ─── Agent Card ──────────────────────────────────────────── */

function AgentCard({
  agent,
  onEdit,
  onDelete,
  onUseAgent,
}: {
  agent: AgentInfo;
  onEdit: () => void;
  onDelete: () => void;
  onUseAgent?: (agentName: string) => void;
}) {
  const { t } = useTranslation();
  const tools = agent.tools;
  return (
    <div className="group flex flex-col rounded-lg border border-border/60 bg-background px-3 py-2.5 transition-colors hover:border-sky-500/40">
      <div className="flex items-start gap-2">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-sky-500/10 text-sky-600 dark:text-sky-400">
          <Users className="h-3.5 w-3.5" />
        </div>
        <div className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium leading-tight" title={agent.name}>
            {agent.name}
          </span>
          <p className="mt-0.5 line-clamp-2 text-xs leading-snug text-muted-foreground">
            {agent.description || "—"}
          </p>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1">
        {agent.model ? (
          <Badge className="bg-violet-500/10 text-violet-600 dark:text-violet-400">
            {agent.model}
          </Badge>
        ) : null}
        {tools === null ? (
          <Badge className="bg-blue-500/10 text-blue-600 dark:text-blue-400">
            <Wrench className="mr-0.5 h-2.5 w-2.5" />
            {t("agents.allTools")}
          </Badge>
        ) : tools.length === 0 ? (
          <Badge className="bg-muted/60 text-muted-foreground/70">
            {t("agents.noTools")}
          </Badge>
        ) : (
          tools.map((tool) => (
            <Badge
              key={tool}
              className="bg-muted/60 text-muted-foreground/80"
            >
              {tool}
            </Badge>
          ))
        )}
      </div>

      <div className="mt-2 flex items-center justify-between gap-0.5">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1 px-2 text-[11px] text-sky-600 hover:!bg-sky-500/10 hover:!text-sky-600 dark:text-sky-400"
          onClick={() => onUseAgent?.(agent.name)}
        >
          <Sparkles className="h-3 w-3" />
          {t("agents.useAgent")}
        </Button>
        <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
          <IconButton title={t("agents.edit")} onClick={onEdit}>
            <Pencil className="h-3 w-3" />
          </IconButton>
          <IconButton
            title={t("agents.delete")}
            onClick={onDelete}
            className="hover:!bg-destructive/10 hover:!text-destructive"
          >
            <Trash2 className="h-3 w-3" />
          </IconButton>
        </div>
      </div>
    </div>
  );
}
