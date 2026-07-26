import { useMemo, useState } from "react";
import { Check, Copy, ExternalLink, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { MarkdownText } from "@/components/MarkdownText";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useVersionCheck, type UpdateInfo } from "@/hooks/useVersionCheck";
import { cn } from "@/lib/utils";

interface VersionBadgeProps {
  /** 当前版本号(来自后端 boot.version)。 */
  version: string | null;
  /** updater.json 的 URL,默认同源 /updater.json。 */
  updaterUrl?: string;
}

/**
 * 版本号徽章 + 新版本提示弹窗。
 *
 * 设计参考 QwenPaw PR #715:
 *  - logo 旁显示 "v{version}",有更新时右上角红点 + 紫色高亮
 *  - 点击版本号打开 Modal,显示 release notes + 多部署方式升级命令
 *  - 用户可一键复制对应升级命令,手动执行
 *
 * Tauri 桌面端后续接入 @tauri-apps/plugin-updater 时,
 * 点击版本号将改为调用原生 updater 自动下载安装(见 handleUpdate 内的 isTauri 分支)。
 */
export function VersionBadge({ version, updaterUrl }: VersionBadgeProps) {
  const { t, i18n } = useTranslation();
  const updateInfo = useVersionCheck(version, updaterUrl);
  const [open, setOpen] = useState(false);

  const hasUpdate = updateInfo?.hasUpdate ?? false;
  const requiresForce = updateInfo?.requiresForceUpdate ?? false;

  // 当前语言对应的 release notes(zh-CN 优先 zh,en 兜底)
  const notesMarkdown = useMemo(() => {
    if (!updateInfo) return "";
    const lang = i18n.language?.startsWith("zh") ? "zh" : "en";
    return updateInfo.notes[lang] ?? updateInfo.notes.en ?? "";
  }, [updateInfo, i18n.language]);

  if (!version) return null;

  const handleClick = () => {
    if (!hasUpdate && !requiresForce) return;
    setOpen(true);
  };

  return (
    <>
      <TooltipProvider delayDuration={200} skipDelayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={handleClick}
              aria-label={
                hasUpdate
                  ? t("version.updateAvailable", {
                      defaultValue: "发现新版本 v{{version}}",
                      version: updateInfo?.latestVersion,
                    })
                  : t("version.current", {
                      defaultValue: "当前版本 v{{version}}",
                      version,
                    })
              }
              className={cn(
                "relative inline-flex items-center text-xs font-medium shrink-0",
                "transition-colors duration-200",
                hasUpdate || requiresForce
                  ? "cursor-pointer text-primary hover:text-primary/80"
                  : "cursor-default text-muted-foreground",
              )}
            >
              <span>v{version}</span>
              {(hasUpdate || requiresForce) && (
                <span
                  aria-hidden
                  className={cn(
                    "absolute -right-1.5 -top-1.5 h-2 w-2 rounded-full",
                    requiresForce ? "bg-destructive" : "bg-primary",
                    "ring-2 ring-background",
                  )}
                />
              )}
            </button>
          </TooltipTrigger>
          <TooltipContent side="bottom">
            {hasUpdate || requiresForce
              ? t("version.updateAvailable", {
                  defaultValue: "发现新版本 v{{version}}",
                  version: updateInfo?.latestVersion,
                })
              : t("version.upToDate", { defaultValue: "已是最新版本" })}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <UpdateDialog
        open={open}
        onOpenChange={setOpen}
        updateInfo={updateInfo}
        notesMarkdown={notesMarkdown}
      />
    </>
  );
}

/** 新版本提示弹窗:release notes + 多部署方式升级命令。 */
function UpdateDialog({
  open,
  onOpenChange,
  updateInfo,
  notesMarkdown,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  updateInfo: UpdateInfo | null;
  notesMarkdown: string;
}) {
  const { t } = useTranslation();

  if (!updateInfo) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <RefreshCw className="h-4 w-4 text-primary" aria-hidden />
            {t("version.dialog.title", {
              defaultValue: "发现新版本 v{{version}}",
              version: updateInfo.latestVersion,
            })}
          </DialogTitle>
          <DialogDescription>
            {t("version.dialog.description", {
              defaultValue:
                "当前版本 v{{current}},最新版本 v{{latest}}。请根据你的部署方式选择对应的升级命令。",
              current: updateInfo.currentVersion,
              latest: updateInfo.latestVersion,
            })}
          </DialogDescription>
        </DialogHeader>

        {/* release notes:Markdown 渲染 */}
        {notesMarkdown ? (
          <div className="max-h-[240px] overflow-y-auto rounded-md border border-border/60 bg-muted/30 p-3">
            <div className="prose prose-sm dark:prose-invert max-w-none">
              <MarkdownText>{notesMarkdown}</MarkdownText>
            </div>
          </div>
        ) : null}

        {/* 升级命令列表:按部署方式分类 */}
        <div className="space-y-2">
          <p className="text-sm font-medium text-foreground">
            {t("version.dialog.upgradeMethods", { defaultValue: "选择你的部署方式:" })}
          </p>
          <UpgradeCommand
            label="pip"
            command="pip install --upgrade miniunicorn"
          />
          <UpgradeCommand
            label="npm"
            command="npm install -g miniunicorn@latest"
          />
          <UpgradeCommand
            label="Docker"
            command="docker pull agentscope/miniunicorn:latest"
          />
          <UpgradeCommand
            label={t("version.dialog.source", { defaultValue: "源码" })}
            command="git pull origin main && pip install -e ."
          />
        </div>

        <p className="text-xs text-muted-foreground">
          {t("version.dialog.restartHint", {
            defaultValue: "升级后请重启服务:miniunicorn gateway",
          })}
        </p>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() =>
              window.open(updateInfo.releaseUrl, "_blank", "noopener,noreferrer")
            }
          >
            <ExternalLink className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {t("version.dialog.viewRelease", { defaultValue: "查看发布说明" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** 单条升级命令行:左侧 label + 命令文本 + 右侧复制按钮。 */
function UpgradeCommand({ label, command }: { label: string; command: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(command).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="flex items-center gap-2">
      <span className="w-16 shrink-0 text-xs font-medium text-muted-foreground">
        {label}
      </span>
      <code className="flex-1 truncate rounded-md bg-muted px-2 py-1.5 font-mono text-xs text-foreground">
        {command}
      </code>
      <Tooltip delayDuration={200}>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            onClick={handleCopy}
            className="h-7 w-7 shrink-0 rounded-md"
            aria-label={copied ? t("common.copied") : t("common.copy")}
          >
            {copied ? (
              <Check className="h-3.5 w-3.5 text-emerald-600" aria-hidden />
            ) : (
              <Copy className="h-3.5 w-3.5" aria-hidden />
            )}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {copied ? t("common.copied") : t("common.copy")}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}
