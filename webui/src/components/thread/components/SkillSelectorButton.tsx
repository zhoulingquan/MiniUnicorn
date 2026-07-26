import { Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { SkillInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface SkillSelectorButtonProps {
  /** 已过滤好的可选 skill 列表（通常为 available && !disabled）。 */
  skills: SkillInfo[];
  disabled?: boolean;
  isHero: boolean;
  onSelect: (skillName: string) => void;
  ariaLabel: string;
  emptyLabel: string;
  builtinBadge: string;
  workspaceBadge: string;
}

/** 输入框左下角的 skill 选择按钮(Sparkles 图标),点击展开下拉菜单。
 *  - 选中 skill 后调用 ``onSelect(skill.name)``,由主组件把提示文字插入输入框,
 *    引导 LLM 主动 ``read_file`` 该 skill 的 SKILL.md 并按其指示工作。 */
export function SkillSelectorButton({
  skills,
  disabled,
  isHero,
  onSelect,
  ariaLabel,
  emptyLabel,
  builtinBadge,
  workspaceBadge,
}: SkillSelectorButtonProps) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          disabled={disabled}
          aria-label={ariaLabel}
          title={ariaLabel}
          className={cn(
            "rounded-full transition-colors",
            isHero
              ? "h-8 w-8 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card"
              : "h-9 w-9 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
            "text-muted-foreground hover:text-foreground",
          )}
        >
          <Sparkles className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[22rem] max-w-[calc(100vw-1rem)]">
        <DropdownMenuLabel className="text-[11px] uppercase tracking-wide text-muted-foreground">
          {ariaLabel}
        </DropdownMenuLabel>
        {skills.length === 0 ? (
          <div className="px-2.5 py-2 text-[12px] text-muted-foreground">
            {emptyLabel}
          </div>
        ) : (
          skills.map((skill) => (
            <DropdownMenuItem
              key={skill.name}
              onSelect={() => onSelect(skill.name)}
              className="flex flex-col items-start gap-0.5 py-2"
            >
              <span className="flex w-full items-center gap-1.5 text-[13px] font-medium text-foreground">
                <Sparkles className="h-3.5 w-3.5 shrink-0 text-amber-500" aria-hidden />
                <span className="truncate">{skill.name}</span>
                <span
                  className={cn(
                    "ml-auto shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
                    skill.source === "builtin"
                      ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                      : "bg-violet-500/15 text-violet-600 dark:text-violet-400",
                  )}
                >
                  {skill.source === "builtin" ? builtinBadge : workspaceBadge}
                </span>
              </span>
              {skill.description ? (
                <span className="line-clamp-2 text-[11.5px] leading-snug text-muted-foreground/80">
                  {skill.description}
                </span>
              ) : null}
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
