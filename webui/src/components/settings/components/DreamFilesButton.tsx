// Dream 生成文件查看入口:Overview section 中"View Files"按钮 + Dialog。
// 从 SettingsView.tsx 拆分而来。

import { useState } from "react";
import { FileText, FolderOpen, Loader2, Moon } from "lucide-react";
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
import { listDreamFiles, readDreamFile, type DreamFileEntry } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

/**
 * Dream 生成文件查看入口。点击打开 Dialog,列出 Dream 流程生成/维护的
 * 记忆文件,选中文件后加载内容并以只读方式展示(Markdown 渲染或文本视图)。
 */
export function DreamFilesButton() {
  const { t } = useTranslation();
  const { token } = useClient();
  const [open, setOpen] = useState(false);
  const [loadingList, setLoadingList] = useState(false);
  const [files, setFiles] = useState<DreamFileEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [loadingContent, setLoadingContent] = useState(false);
  const [exists, setExists] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  /** 取文件作用说明,优先 i18n,缺失时返回空串(不显示)。 */
  const fileDesc = (name: string): string =>
    t(`settings.dream.files.desc.${name}`, { defaultValue: "" });

  const loadList = async () => {
    setLoadingList(true);
    setError(null);
    try {
      const payload = await listDreamFiles(token);
      setFiles(payload.files ?? []);
      // 默认选中第一个已存在的文件
      const firstExisting = (payload.files ?? []).find((f) => f.exists);
      if (firstExisting) {
        setSelected(firstExisting.name);
        void loadContent(firstExisting.name);
      } else {
        setSelected(null);
        setContent("");
        setExists(null);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoadingList(false);
    }
  };

  const loadContent = async (name: string) => {
    setLoadingContent(true);
    setError(null);
    try {
      const data = await readDreamFile(token, name);
      setContent(data.content);
      setExists(data.exists);
    } catch (e) {
      setError((e as Error).message);
      setExists(false);
    } finally {
      setLoadingContent(false);
    }
  };

  const handleOpen = async () => {
    setOpen(true);
    await loadList();
  };

  const handleSelect = (name: string) => {
    setSelected(name);
    void loadContent(name);
  };

  const isJsonl = (name: string) => name.endsWith(".jsonl");
  const lineCount = content ? content.split("\n").length : 0;

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="gap-1.5 rounded-full"
        onClick={handleOpen}
        aria-label={tx("settings.dream.files.button", "View Files")}
        title={tx("settings.dream.files.button", "View Files")}
      >
        <FolderOpen className="h-3.5 w-3.5" aria-hidden />
        <span>
          {tx("settings.dream.files.button", "View Files")}
        </span>
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-4xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-sm">
              <Moon className="h-4 w-4 text-muted-foreground" />
              {tx("settings.dream.files.title", "Memory Diagnostics")}
            </DialogTitle>
            <DialogDescription>
              {tx(
                "settings.dream.files.description",
                "Read-only governed storage, Dream inputs, and legacy import sources.",
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="flex min-h-[420px] max-h-[60vh] flex-col gap-3 sm:flex-row">
            {/* 文件列表 */}
            <div className="flex shrink-0 flex-col gap-1 sm:w-64 sm:border-r sm:border-border/45 sm:pr-3">
              <div className="px-1 pb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {tx("settings.dream.files.listHeader", "Files")}
              </div>
              {loadingList ? (
                <div className="flex items-center justify-center py-8 text-[12px] text-muted-foreground">
                  <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
                  {tx("settings.dream.files.loading", "Loading…")}
                </div>
              ) : files.length === 0 ? (
                <div className="px-1 py-6 text-[12px] text-muted-foreground">
                  {tx("settings.dream.files.empty", "No files available.")}
                </div>
              ) : (
                <div className="flex flex-col gap-0.5 overflow-y-auto">
                  {files.map((f) => {
                    const active = selected === f.name;
                    const desc = fileDesc(f.name);
                    return (
                      <button
                        key={f.name}
                        type="button"
                        onClick={() => handleSelect(f.name)}
                        className={cn(
                          "flex flex-col items-start gap-0.5 rounded-[8px] px-2.5 py-2 text-left transition-colors",
                          active
                            ? "bg-muted/70 text-foreground"
                            : "hover:bg-muted/40 text-foreground/80",
                          !f.exists && "opacity-55",
                        )}
                      >
                        <span className="flex w-full items-center gap-1.5">
                          <FileText
                            className={cn(
                              "h-3.5 w-3.5 shrink-0",
                              f.exists ? "text-foreground/60" : "text-muted-foreground/50",
                            )}
                            aria-hidden
                          />
                          <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium leading-5">
                            {f.name}
                          </span>
                          {!f.exists ? (
                            <span className="shrink-0 text-[10px] text-muted-foreground/70">
                              {tx("settings.dream.files.notExists", "N/A")}
                            </span>
                          ) : null}
                        </span>
                        {f.exists ? (
                          <span className="pl-5 text-[10.5px] leading-4 text-muted-foreground/80">
                            {f.size_human || `${f.size} B`}
                            {f.modified_at_human ? ` · ${f.modified_at_human}` : ""}
                          </span>
                        ) : null}
                        {desc ? (
                          <span className="pl-5 text-[10.5px] leading-4 text-muted-foreground/70 line-clamp-2">
                            {desc}
                          </span>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* 内容视图 */}
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              {selected ? (
                <>
                  <div className="flex items-center justify-between gap-2 border-b border-border/40 pb-1.5">
                    <span className="truncate font-mono text-[12px] text-foreground/85">
                      {selected}
                    </span>
                    {isJsonl(selected) && content ? (
                      <span className="shrink-0 text-[10.5px] text-muted-foreground/80">
                        {lineCount} {tx("settings.dream.files.lines", "lines")}
                      </span>
                    ) : null}
                  </div>
                  {fileDesc(selected) ? (
                    <p className="rounded-md bg-muted/40 px-2.5 py-1.5 text-[11px] leading-relaxed text-muted-foreground">
                      {fileDesc(selected)}
                    </p>
                  ) : null}
                  {loadingContent ? (
                    <div className="flex flex-1 items-center justify-center text-[12px] text-muted-foreground">
                      <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
                      {tx("settings.dream.files.loading", "Loading…")}
                    </div>
                  ) : exists === false ? (
                    <div className="flex flex-1 items-center justify-center px-4 text-center text-[12px] text-muted-foreground">
                      {tx(
                        "settings.dream.files.notExistsHint",
                        "This file has not been created yet.",
                      )}
                    </div>
                  ) : content ? (
                    <Textarea
                      value={content}
                      readOnly
                      placeholder={tx("settings.dream.files.placeholder", "File content…")}
                      className={cn(
                        "min-h-[360px] flex-1 resize-none font-mono text-[12px] leading-relaxed",
                        "bg-muted/20 focus-visible:ring-1",
                      )}
                    />
                  ) : (
                    <div className="flex flex-1 items-center justify-center text-[12px] text-muted-foreground">
                      {tx("settings.dream.files.emptyContent", "File is empty.")}
                    </div>
                  )}
                </>
              ) : (
                <div className="flex flex-1 items-center justify-center text-[12px] text-muted-foreground">
                  {tx(
                    "settings.dream.files.selectHint",
                    "Select a file from the list to view its content.",
                  )}
                </div>
              )}
              {error ? <p className="text-[11px] text-destructive">{error}</p> : null}
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setOpen(false)}
            >
              {tx("settings.dream.files.close", "Close")}
            </Button>
            <Button
              onClick={loadList}
              disabled={loadingList}
            >
              <Loader2
                className={cn("mr-1.5 h-3 w-3", loadingList ? "animate-spin" : "hidden")}
              />
              {tx("settings.dream.files.refresh", "Refresh")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
