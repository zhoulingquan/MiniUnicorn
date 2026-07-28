// Bootstrap 文件行:Overview 中 AGENTS.md / SOUL.md 编辑入口。
// 从 SettingsView.tsx 拆分而来。

import { useState } from "react";
import { FileText, Loader2, Pencil } from "lucide-react";
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
import { Textarea } from "@/components/ui/textarea";
import { readBootstrapFile, saveBootstrapFile } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

import { OverviewRowIcon } from "./SettingsRow";

export function BootstrapFileRow({ fileName }: { fileName: string }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [exists, setExists] = useState<boolean | null>(null);

  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const openEditor = async () => {
    setOpen(true);
    setLoading(true);
    setError(null);
    try {
      const data = await readBootstrapFile(token, fileName);
      setContent(data.content);
      setExists(data.exists);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    if (!content.trim()) {
      setError(tx("settings.bootstrap.emptyError", "Content must not be empty"));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await saveBootstrapFile(token, fileName, content);
      setExists(true);
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={openEditor}
        className="group flex min-h-[68px] w-full items-center gap-3 px-4 py-3.5 text-left transition-colors hover:bg-muted/30 sm:px-5"
      >
        <OverviewRowIcon icon={FileText} />
        <span className="min-w-0 flex-1">
          <span className="block text-[14px] font-medium leading-5 text-foreground">{fileName}</span>
          <span className="mt-0.5 block truncate text-[12px] leading-5 text-muted-foreground">
            {exists === null
              ? tx("settings.bootstrap.tapToView", "Tap to view / edit")
              : exists
                ? tx("settings.bootstrap.configured", "Configured")
                : tx("settings.bootstrap.notConfigured", "Not configured — using template")}
          </span>
        </span>
        <span className="ml-auto flex min-w-0 items-center gap-2">
          <Pencil className="h-3.5 w-3.5 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground" aria-hidden />
        </span>
      </button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-sm">
              <FileText className="h-4 w-4 text-muted-foreground" />
              {fileName}
            </DialogTitle>
            <DialogDescription>
              {tx(
                "settings.bootstrap.editorDescription",
                "Loaded into the system prompt every turn. Edits apply on next message.",
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            {loading ? (
              <div className="flex h-[360px] items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {tx("settings.bootstrap.loading", "Loading…")}
              </div>
            ) : (
              <Textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder={tx("settings.bootstrap.placeholder", "Markdown content…")}
                className="min-h-[360px] font-mono text-[12px] leading-relaxed"
              />
            )}
            {error && <p className="text-[11px] text-destructive">{error}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)} disabled={saving}>
              {tx("settings.bootstrap.cancel", "Cancel")}
            </Button>
            <Button onClick={handleSave} disabled={saving || loading}>
              {saving ? <Loader2 className="mr-1.5 h-3 w-3 animate-spin" /> : null}
              {saving ? tx("settings.bootstrap.saving", "Saving…") : tx("settings.bootstrap.save", "Save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
