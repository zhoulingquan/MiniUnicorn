import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import {
  ArrowUp,
  Loader2,
  Maximize2,
  Minimize2,
  Plus,
  Square,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  WorkspaceAccessMenu,
  WorkspaceProjectPicker,
} from "@/components/thread/WorkspaceControls";
import {
  useAttachedImages,
  type AttachedImage,
  type AttachmentError,
  DOCUMENT_EXTENSIONS,
  MAX_IMAGES_PER_MESSAGE,
} from "@/hooks/useAttachedImages";
import { useClipboardAndDrop } from "@/hooks/useClipboardAndDrop";
import { useSlashCommandPalette } from "@/hooks/useSlashCommandPalette";
import type { SendImage, SendOptions } from "@/hooks/useMiniunicornStream";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { SlashCommand } from "@/lib/types";

import type { ThreadComposerProps } from "./types";
import { AgentSelectionChip } from "./components/AgentSelectionChip";
import { AgentSelectorButton } from "./components/AgentSelectorButton";
import { AttachmentChip, formatBytes } from "./components/AttachmentChip";
import { ComposerModelBadge } from "./components/ComposerModelBadge";
import { ContextChip } from "./components/ContextChip";
import { RunElapsedStrip } from "./components/RunElapsedStrip";
import { SkillSelectorButton } from "./components/SkillSelectorButton";
import { SlashCommandPalette } from "./components/SlashCommandPalette";

/** ``<input accept>``:与后端 MIME 白名单对齐。图片走 image worker,
 * 文档走直接 base64 路径。SVG 被刻意排除以避免嵌入式脚本的 XSS 风险。 */
const ACCEPT_ATTR =
  "image/png,image/jpeg,image/webp,image/gif," +
  "application/pdf," +
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document," +
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet," +
  "application/vnd.openxmlformats-officedocument.presentationml.presentation," +
  "text/plain,text/markdown,text/csv," +
  "application/json,application/xml,text/xml,text/html," +
  "application/x-yaml,text/yaml," +
  "application/octet-stream," +
  DOCUMENT_EXTENSIONS;

export function ThreadComposer({
  onSend,
  disabled,
  placeholder,
  isStreaming = false,
  modelLabel = null,
  modelProvider = null,
  modelProviderLabel = null,
  modelApiBase = null,
  models,
  onSelectModel,
  variant = "thread",
  slashCommands = [],
  onStop,
  runStartedAt = null,
  goalState,
  workspaceScope = null,
  workspaceDefaultScope = null,
  workspaceControls = null,
  workspaceScopeDisabled = false,
  workspaceError = null,
  onWorkspaceScopeChange,
  agents = [],
  selectedAgentId = null,
  onSelectAgent,
  onClearAgent,
  skills = [],
  messageCount = 0,
  contextWindowTokens = null,
  contextUsage = null,
  conversationKey = null,
  prefillText = null,
  onPrefillConsumed,
}: ThreadComposerProps) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const chipRefs = useRef(new Map<string, HTMLButtonElement>());
  const lastConversationKeyRef = useRef<string | null>(conversationKey);

  useEffect(() => {
    if (lastConversationKeyRef.current === conversationKey) return;
    lastConversationKeyRef.current = conversationKey;
    chipRefs.current.clear();
  }, [conversationKey]);

  // 回退后编辑重发:父组件传入 prefillText 时,填入输入框、聚焦、自适应高度,
  // 然后通知父组件清空 prefill 状态(避免重复触发)。
  useEffect(() => {
    if (prefillText == null || prefillText.length === 0) return;
    setValue(prefillText);
    onPrefillConsumed?.();
    const id = requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.style.height = "auto";
      const maxH = expanded ? 400 : 260;
      const minH = expanded ? 200 : 0;
      el.style.height = `${Math.max(minH, Math.min(el.scrollHeight, maxH))}px`;
      // 将光标移到末尾,方便用户继续编辑
      const len = el.value.length;
      el.setSelectionRange(len, len);
      el.focus();
    });
    return () => cancelAnimationFrame(id);
  }, [prefillText, onPrefillConsumed, expanded]);

  const isHero = variant === "hero";
  const showProjectPicker =
    isHero
    && !!workspaceDefaultScope
    && !!onWorkspaceScopeChange
    && workspaceControls?.can_change_project !== false;

  const resolvedPlaceholder = isStreaming
    ? t("thread.composer.placeholderStreaming")
    : placeholder ?? t("thread.composer.placeholderThread");

  const { images, enqueue, remove, clear, encoding, full } =
    useAttachedImages();

  const formatRejection = useCallback(
    (reason: AttachmentError): string => {
      const key = `thread.composer.imageRejected.${reason}`;
      return t(key, { max: MAX_IMAGES_PER_MESSAGE });
    },
    [t],
  );

  const addFiles = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      const { rejected } = enqueue(files);
      if (rejected.length > 0) {
        setInlineError(formatRejection(rejected[0].reason));
      } else {
        setInlineError(null);
      }
    },
    [enqueue, formatRejection],
  );

  const {
    isDragging,
    onPaste,
    onDragEnter,
    onDragOver,
    onDragLeave,
    onDrop,
  } = useClipboardAndDrop(addFiles);

  useEffect(() => {
    if (disabled) return;
    const el = textareaRef.current;
    if (!el) return;
    const id = requestAnimationFrame(() => el.focus());
    return () => cancelAnimationFrame(id);
  }, [disabled]);

  // 展开/收起时重新调整输入框高度
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const maxH = expanded ? 400 : 260;
    const minH = expanded ? 200 : 0;
    el.style.height = `${Math.max(minH, Math.min(el.scrollHeight, maxH))}px`;
  }, [expanded]);

  const readyImages = useMemo(
    () => images.filter((img): img is AttachedImage & { dataUrl: string } =>
      img.status === "ready" && typeof img.dataUrl === "string",
    ),
    [images],
  );
  const hasErrors = images.some((img) => img.status === "error");

  const canSend =
    !disabled
    && !encoding
    && !hasErrors
    && (value.trim().length > 0 || readyImages.length > 0);

  // Resolve the active subagent object (if any) for the chip + send metadata.
  const selectedAgent = useMemo(
    () =>
      selectedAgentId
        ? agents.find((agent) => agent.name === selectedAgentId) ?? null
        : null,
    [agents, selectedAgentId],
  );
  const showAgentSelector = agents.length > 0;
  // 过滤出 available 且未禁用的 skill,作为下拉菜单可选列表。
  const selectableSkills = useMemo(
    () => skills.filter((skill) => skill.available && !skill.disabled),
    [skills],
  );
  const showSkillSelector = selectableSkills.length > 0;
  const handleSelectAgent = useCallback(
    (agentId: string) => {
      // Sentinel emitted by the "clear" menu item: delegate to onClearAgent
      // so callers only need to implement the clear path once.
      if (agentId === "__none__") {
        onClearAgent?.();
        return;
      }
      onSelectAgent?.(agentId);
    },
    [onClearAgent, onSelectAgent],
  );
  const handleClearAgent = useCallback(() => {
    onClearAgent?.();
  }, [onClearAgent]);

  const resizeTextarea = useCallback(() => {
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.style.height = "auto";
      const maxH = expanded ? 400 : 260;
      const minH = expanded ? 200 : 0;
      el.style.height = `${Math.max(minH, Math.min(el.scrollHeight, maxH))}px`;
      el.focus();
    });
  }, [expanded]);

  // 选中 skill 后,在输入框中前置一段提示文字,引导 LLM 主动 read_file
  // 该 skill 的 SKILL.md 并按其指示工作。若输入框已有内容,保留并追加换行,
  // 让用户的原始问题/任务描述跟在 skill 提示之后。
  const handleSelectSkill = useCallback(
    (skillName: string) => {
      const hint = t("thread.composer.skillHint", { name: skillName });
      setValue((prev) => {
        const trimmed = prev.trim();
        if (!trimmed) return hint;
        return `${hint}\n\n${trimmed}`;
      });
      setInlineError(null);
      resizeTextarea();
    },
    [resizeTextarea, t],
  );

  const handleCommandSelected = useCallback(
    (command: SlashCommand) => {
      // /stop 命令只清空输入框(由 Hook 负责 onStop 调用和 recent 管理)
      if (command.command === "/stop") {
        setValue("");
      } else {
        setValue(command.argHint ? `${command.command} ` : command.command);
      }
      setInlineError(null);
    },
    [],
  );

  const {
    filteredSlashCommands,
    showSlashMenu,
    selectedCommandIndex,
    setSelectedCommandIndex,
    slashPaletteLayout,
    chooseSlashCommand,
    reset: resetSlashMenu,
    onKeyDown: handleSlashKeyDown,
  } = useSlashCommandPalette({
    value,
    disabled: !!disabled,
    isStreaming: !!isStreaming,
    onStop,
    slashCommands: slashCommands ?? [],
    modelLabel,
    goalState,
    formRef,
    resizeTextarea,
    onCommandSelected: handleCommandSelected,
  });

  const submit = useCallback(() => {
    if (!canSend) return;
    const trimmed = value.trim();
    // Share the same normalized ``data:`` URL with both the wire payload and
    // the optimistic bubble preview: data URLs are self-contained (no blob
    // lifetime, safe under React StrictMode double-mount) and keep the
    // bubble in sync with whatever the backend actually sees.
    const payload: SendImage[] | undefined =
      readyImages.length > 0
        ? readyImages.map((img) => ({
            media: {
              data_url: img.dataUrl,
              name: img.file.name,
            },
            preview: { url: img.dataUrl, name: img.file.name },
          }))
        : undefined;
    const options: SendOptions | undefined = selectedAgentId
      ? { agentId: selectedAgentId }
      : undefined;
    onSend(trimmed, payload, options);
    setValue("");
    setInlineError(null);
    clear();
    resetSlashMenu();
    resizeTextarea();
  }, [
    canSend,
    clear,
    onSend,
    readyImages,
    resetSlashMenu,
    resizeTextarea,
    selectedAgentId,
    value,
  ]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    // 斜杠命令面板的键盘导航(ArrowUp/Down/Tab/Enter/Escape)由 Hook 处理。
    if (handleSlashKeyDown(e)) return;
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      // 展开模式下回车不提交,改为换行,方便输入大段文字
      if (expanded) return;
      e.preventDefault();
      submit();
    }
  };

  const onInput: React.FormEventHandler<HTMLTextAreaElement> = (e) => {
    const el = e.currentTarget;
    el.style.height = "auto";
    const maxH = expanded ? 400 : 260;
    const minH = expanded ? 200 : 0;
    el.style.height = `${Math.max(minH, Math.min(el.scrollHeight, maxH))}px`;
  };

  const onFilePick: React.ChangeEventHandler<HTMLInputElement> = (e) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    addFiles(files);
  };

  const removeChip = useCallback(
    (id: string) => {
      const { nextFocusId } = remove(id);
      setInlineError(null);
      requestAnimationFrame(() => {
        const el = nextFocusId ? chipRefs.current.get(nextFocusId) : null;
        if (el) {
          el.focus();
        } else {
          textareaRef.current?.focus();
        }
      });
    },
    [remove],
  );

  const onChipKey = useCallback(
    (id: string) => (e: ReactKeyboardEvent<HTMLButtonElement>) => {
      if (
        e.key === "Delete" ||
        e.key === "Backspace" ||
        e.key === "Enter" ||
        e.key === " "
      ) {
        e.preventDefault();
        removeChip(id);
      }
    },
    [removeChip],
  );

  const attachButtonDisabled = disabled || full;
  const showStopButton = isStreaming && !!onStop;
  const centerHeroPlaceholder =
    isHero && value.length === 0 && images.length === 0 && !isStreaming;
  const inputTextClasses = cn(
    "w-full resize-none bg-transparent",
    isHero
      ? cn(
          "min-h-[60px] px-4 text-[14px] leading-5",
          centerHeroPlaceholder ? "pb-2 pt-5" : "pb-1.5 pt-3",
        )
      : "min-h-[42px] px-3.5 pb-1.5 pt-2.5 text-[13px] leading-5",
    expanded && "pr-10",
  );

  return (
    <form
      ref={formRef}
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      className={cn("relative w-full", isHero ? "px-0" : "px-1 pb-1.5 pt-1 sm:px-0")}
    >
      {showSlashMenu ? (
        <SlashCommandPalette
          commands={filteredSlashCommands}
          selectedIndex={selectedCommandIndex}
          layout={slashPaletteLayout}
          isHero={isHero}
          onHover={setSelectedCommandIndex}
          onChoose={chooseSlashCommand}
        />
      ) : null}
      <div
        className={cn(
          "group/composer relative mx-auto flex w-full flex-col overflow-visible transition-all duration-200",
          "after:pointer-events-none after:absolute after:inset-[-1px] after:rounded-[inherit] after:border after:border-blue-300/75 after:opacity-0 after:transition-opacity after:duration-200 focus-within:after:opacity-100 dark:after:border-blue-400/55",
          isHero
            ? "max-w-[44rem] rounded-[22px] border border-black/[0.035] bg-card shadow-[0_20px_55px_rgba(15,23,42,0.08)] dark:border-white/[0.06] dark:shadow-[0_24px_55px_rgba(0,0,0,0.34)]"
            : "max-w-[40rem] rounded-[18px] border border-black/[0.035] bg-card shadow-[0_12px_30px_rgba(15,23,42,0.07)] dark:border-white/[0.06] dark:shadow-[0_16px_34px_rgba(0,0,0,0.28)]",
          "focus-within:border-blue-300/75 dark:focus-within:border-blue-400/55",
          disabled && "opacity-60",
          isDragging && "ring-2 ring-primary/40 motion-reduce:ring-0 motion-reduce:border-primary",
          goalState?.active &&
            "goal-shell-glow ring-1 ring-sky-400/35 motion-reduce:ring-sky-400/25 dark:ring-sky-400/45",
        )}
      >
        {images.length > 0 ? (
          <div
            className="flex flex-wrap gap-2 px-3 pt-3"
            aria-label={t("thread.composer.attachImage")}
          >
            {images.map((img) => (
              <AttachmentChip
                key={img.id}
                image={img}
                labelRemove={t("thread.composer.remove")}
                labelEncoding={t("thread.composer.encoding")}
                normalizedHint={(orig, current) =>
                  t("thread.composer.normalizedSizeHint", {
                    orig: formatBytes(orig),
                    current: formatBytes(current),
                  })
                }
                formatError={formatRejection}
                onRemove={() => removeChip(img.id)}
                onKeyDown={onChipKey(img.id)}
                registerRef={(el) => {
                  if (el) chipRefs.current.set(img.id, el);
                  else chipRefs.current.delete(img.id);
                }}
              />
            ))}
          </div>
        ) : null}
        {runStartedAt != null || goalState?.active ? (
          <RunElapsedStrip startedAt={runStartedAt} goalState={goalState} />
        ) : null}
        {selectedAgent ? (
          <div
            className={cn(
              "flex items-center gap-2 px-3",
              images.length > 0 || runStartedAt != null || goalState?.active
                ? "pt-2"
                : isHero
                  ? "pt-3"
                  : "pt-2.5",
            )}
          >
            <AgentSelectionChip
              description={selectedAgent.description}
              onClear={handleClearAgent}
              clearLabel={t("agents.clear")}
              usingLabel={t("agents.usingAgent", { name: selectedAgent.name })}
            />
          </div>
        ) : null}
        <div className="relative">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              resetSlashMenu();
            }}
            onInput={onInput}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            rows={1}
            placeholder={resolvedPlaceholder}
            disabled={disabled}
            aria-label={t("thread.composer.inputAria")}
            className={cn(
              inputTextClasses,
              "relative z-10 caret-foreground placeholder:text-muted-foreground/70",
              "focus:outline-none focus-visible:outline-none",
              "disabled:cursor-not-allowed",
            )}
          />
          <TooltipProvider delayDuration={200} skipDelayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={() => setExpanded((v) => !v)}
                  aria-label={expanded ? t("thread.composer.collapseInput") : t("thread.composer.expandInput")}
                  className={cn(
                    "absolute right-2 top-2 z-20 grid h-7 w-7 place-items-center rounded-full",
                    "text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  )}
                >
                  {expanded ? (
                    <Minimize2 className="h-4 w-4" aria-hidden />
                  ) : (
                    <Maximize2 className="h-3.5 w-3.5" aria-hidden />
                  )}
                </button>
              </TooltipTrigger>
              <TooltipContent
                side="left"
                align="center"
                sideOffset={8}
                collisionPadding={12}
                className={cn(
                  "rounded-[10px] border-border/60 bg-popover/95 px-2.5 py-1.5",
                  "text-[11.5px] leading-snug text-popover-foreground shadow-md backdrop-blur",
                )}
              >
                {expanded ? t("thread.composer.collapseInputHint") : t("thread.composer.expandInputHint")}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
        {inlineError ? (
          <div
            role="alert"
            className={cn(
              "mx-3 mb-1 rounded-md border border-destructive/40 bg-destructive/8 px-2.5 py-1",
              "text-[11.5px] font-medium text-destructive",
            )}
          >
            {inlineError}
          </div>
        ) : null}
        <div
          className={cn(
            "flex items-center justify-between",
            isHero ? cn("gap-1.5 px-4", showProjectPicker ? "pb-1.5" : "pb-3.5") : "gap-2 px-3 pb-2",
          )}
        >
          <div className={cn("flex min-w-0 flex-1 items-center", isHero ? "gap-1.5" : "gap-2")}>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPT_ATTR}
              multiple
              hidden
              onChange={onFilePick}
            />
            <TooltipProvider delayDuration={200} skipDelayDuration={300}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    disabled={attachButtonDisabled}
                    aria-label={t("thread.composer.attachImage")}
                    onClick={() => fileInputRef.current?.click()}
                    className={cn(
                      "rounded-full text-muted-foreground hover:text-foreground",
                      isHero
                        ? "h-8 w-8 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card"
                        : "h-9 w-9 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
                    )}
                  >
                    <Plus className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent
                  side="top"
                  align="center"
                  sideOffset={8}
                  collisionPadding={12}
                  className={cn(
                    "max-w-[min(22rem,calc(100vw-2rem))] rounded-[10px]",
                    "border-border/60 bg-popover/95 px-2.5 py-1.5",
                    "text-[11.5px] leading-snug text-popover-foreground",
                    "shadow-md backdrop-blur",
                  )}
                >
                  <ul className="flex flex-col gap-1">
                    {[
                      t("thread.composer.attachTooltipLimit", { max: MAX_IMAGES_PER_MESSAGE }),
                      t("thread.composer.attachTooltipImages"),
                      t("thread.composer.attachTooltipDocs"),
                    ].map((line) => (
                      <li key={line} className="flex items-start gap-1.5">
                        <span
                          className="mt-[0.45em] h-1 w-1 shrink-0 rounded-full bg-current opacity-50"
                          aria-hidden
                        />
                        <span className="min-w-0 break-words">{line}</span>
                      </li>
                    ))}
                  </ul>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
            {showAgentSelector ? (
              <AgentSelectorButton
                agents={agents}
                selectedAgentId={selectedAgentId}
                disabled={disabled}
                isHero={isHero}
                onSelect={handleSelectAgent}
                ariaLabel={t("agents.title")}
                emptyLabel={t("agents.empty")}
                clearLabel={t("agents.clear")}
              />
            ) : null}
            {showSkillSelector ? (
              <SkillSelectorButton
                skills={selectableSkills}
                disabled={disabled}
                isHero={isHero}
                onSelect={handleSelectSkill}
                ariaLabel={t("skills.title")}
                emptyLabel={t("skills.empty")}
                builtinBadge={t("skills.builtinBadge")}
                workspaceBadge={t("skills.workspaceBadge")}
              />
            ) : null}
            {workspaceScope ? (
              <WorkspaceAccessMenu
                scope={workspaceScope}
                disabled={disabled || workspaceScopeDisabled}
                canUseFullAccess={workspaceControls?.can_use_full_access !== false}
                isHero={isHero}
                onChange={onWorkspaceScopeChange}
              />
            ) : null}
          </div>
          <div className={cn("flex shrink-0 items-center", isHero ? "gap-1.5" : "gap-2")}>
            {modelLabel ? (
              <ComposerModelBadge
                label={modelLabel}
                provider={modelProvider}
                providerLabel={modelProviderLabel}
                apiBase={modelApiBase}
                models={models}
                onSelect={onSelectModel}
                isHero={isHero}
              />
            ) : null}
            <ContextChip
              messageCount={messageCount}
              contextWindowTokens={contextWindowTokens}
              contextUsage={contextUsage}
              isHero={isHero}
              hasInput={value.trim().length > 0}
            />
            <TooltipProvider delayDuration={200} skipDelayDuration={300}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="inline-flex">
                    <Button
                      type={showStopButton ? "button" : "submit"}
                      size="icon"
                      disabled={showStopButton ? disabled : !canSend}
                      aria-label={showStopButton ? t("thread.composer.stop") : t("thread.composer.send")}
                      onClick={showStopButton ? onStop : undefined}
                      className={cn(
                        "rounded-full transition-transform",
                        showStopButton
                          ? "border border-border/70 bg-card text-foreground/85 shadow-[0_3px_10px_rgba(15,23,42,0.08)] hover:bg-muted/65 hover:text-foreground disabled:text-muted-foreground/50"
                          : isHero
                            ? "border border-foreground bg-foreground text-background shadow-[0_4px_12px_rgba(15,23,42,0.20)] hover:bg-foreground/90 disabled:border-foreground/35 disabled:bg-foreground/35 disabled:text-background/80"
                            : "border border-foreground bg-foreground text-background shadow-[0_3px_10px_rgba(15,23,42,0.18)] hover:bg-foreground/90 disabled:border-foreground/35 disabled:bg-foreground/35 disabled:text-background/80",
                        isHero ? "h-8 w-8" : "h-9 w-9",
                        (canSend || showStopButton) && "hover:scale-[1.03] active:scale-95",
                      )}
                    >
                      {showStopButton ? (
                        <Square className={cn("fill-current stroke-current", isHero ? "h-3 w-3" : "h-3.5 w-3.5")} />
                      ) : isStreaming ? (
                        <Loader2 className={cn(isHero ? "h-4 w-4" : "h-4 w-4", "animate-spin")} />
                      ) : (
                        <ArrowUp className={cn(isHero ? "h-4 w-4" : "h-4 w-4")} />
                      )}
                    </Button>
                  </span>
                </TooltipTrigger>
                <TooltipContent
                  side="top"
                  align="center"
                  sideOffset={8}
                  collisionPadding={12}
                  className={cn(
                    "max-w-[min(22rem,calc(100vw-2rem))] rounded-[10px]",
                    "border-border/60 bg-popover/95 px-2.5 py-1.5",
                    "text-[11.5px] leading-snug text-popover-foreground",
                    "shadow-md backdrop-blur",
                  )}
                >
                  {showStopButton ? (
                    <span>{t("thread.composer.stop")}</span>
                  ) : (
                    <ul className="flex flex-col gap-1">
                      {(expanded
                        ? [
                            t("thread.composer.sendHintNewlineExpanded"),
                            t("thread.composer.sendHintSubmitExpanded"),
                          ]
                        : [
                            t("thread.composer.sendHintEnter"),
                            t("thread.composer.sendHintNewline"),
                          ]
                      ).map((line) => (
                        <li key={line} className="flex items-start gap-1.5">
                          <span
                            className="mt-[0.45em] h-1 w-1 shrink-0 rounded-full bg-current opacity-50"
                            aria-hidden
                          />
                          <span className="min-w-0 break-words">{line}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        </div>
        <WorkspaceProjectPicker
          isHero={isHero}
          disabled={disabled || workspaceScopeDisabled}
          scope={workspaceScope}
          defaultScope={workspaceDefaultScope}
          controls={workspaceControls}
          error={workspaceError}
          onChange={onWorkspaceScopeChange}
        />
      </div>
    </form>
  );
}
