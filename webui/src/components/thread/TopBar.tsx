import { useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, Monitor, Moon, PanelLeft, Search, Sun } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { VersionBadge } from "@/components/VersionBadge";
import type { ThemeMode } from "@/hooks/useTheme";
import { providerDisplayLabel, resolveCustomBrand, type ProviderBrand } from "@/lib/provider-brand";
import { cn } from "@/lib/utils";

interface TopBarProvider {
  name: string;
  label: string;
  configured: boolean;
  is_custom_preset?: boolean;
  api_base?: string | null;
  presets?: Array<{ name: string; label: string; model: string; active: boolean }>;
}

interface TopBarProps {
  /** 点击 PanelLeft 按钮:切换侧边栏宽度。 */
  onToggleSidebar: () => void;
  /** 点击搜索按钮:打开会话搜索弹窗。 */
  onOpenSearch: () => void;
  /** 可选的中间标题(chat 视图传会话标题;其他视图可不传)。 */
  title?: string | null;
  /** 是否显示中间标题。 */
  showTitle?: boolean;
  theme: "light" | "dark";
  themeMode?: ThemeMode;
  onToggleTheme: () => void;
  onToggleLanguage: () => void;
  /** provider 切换器数据。 */
  providers?: TopBarProvider[];
  currentProvider?: string | null;
  onSelectProvider?: (provider: string) => void;
  /** 侧边栏展开宽度(px),用于让左侧按钮组右边缘与侧边栏右边缘对齐。 */
  sidebarWidth?: number;
  /** 后端版本号,显示在 logo 旁边。 */
  version?: string | null;
}

/**
 * 全局固定顶栏:跨整个窗口宽度,独立于 sidebar + main 的 flex 容器。
 * 左侧:PanelLeft 收起按钮 + 品牌文字。
 * 中间:可选标题(chat 视图传会话标题)。
 * 右侧:provider 下拉 + 语言切换 + 主题切换。
 *
 * 设计意图:点击 PanelLeft 按钮只切换侧边栏宽度,顶栏所有元素位置不变。
 */
export function TopBar({
  onToggleSidebar,
  onOpenSearch,
  title,
  showTitle = false,
  theme,
  themeMode,
  onToggleTheme,
  onToggleLanguage,
  providers = [],
  currentProvider = null,
  onSelectProvider,
  sidebarWidth = 272,
  version = null,
}: TopBarProps) {
  const { t } = useTranslation();
  const showProviderSwitcher = !!onSelectProvider && providers.length > 0;

  return (
    <header className="relative z-20 flex h-11 shrink-0 items-center justify-between border-b border-border/60 bg-background">
      {/* 左侧:宽度=侧边栏宽度,logo 左对齐,按钮组右对齐(与侧边栏右边缘对齐)。
       * PanelLeft + 搜索按钮用 -space-x-1 包裹,间距与右侧语言/主题按钮一致。 */}
      <div
        className="flex shrink-0 items-center justify-between pl-3"
        style={{ width: sidebarWidth }}
      >
        <div className="flex min-w-0 items-baseline gap-1.5">
          <span className="text-sm font-semibold text-foreground truncate">
            {t("app.brand")}
          </span>
          <VersionBadge version={version} />
        </div>
        <div className="flex items-center -space-x-1">
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("sidebar.collapse")}
            onClick={onToggleSidebar}
            className="h-8 w-8 rounded-full text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          >
            <PanelLeft className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("sidebar.search")}
            onClick={onOpenSearch}
            className="h-8 w-8 rounded-full text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          >
            <Search className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* 中间:可选标题 */}
      {showTitle && title ? (
        <div className="flex min-w-0 flex-1 items-center justify-center">
          <span className="max-w-[min(50vw,32rem)] truncate text-[12px] font-medium text-muted-foreground">
            {title}
          </span>
        </div>
      ) : null}

      {/* 右侧:provider 下拉 + 语言 + 主题 */}
      <div className="flex shrink-0 items-center gap-2 pr-3">
        {showProviderSwitcher ? (
          <HeaderProviderSwitcher
            providers={providers}
            currentProvider={currentProvider}
            onSelect={onSelectProvider!}
          />
        ) : null}
        <div className="flex items-center -space-x-1">
          <LocaleButton
            onToggleLanguage={onToggleLanguage}
            label={t("thread.header.toggleLanguage")}
          />
          <ThemeButton
            theme={theme}
            themeMode={themeMode}
            onToggleTheme={onToggleTheme}
            label={t("thread.header.toggleTheme")}
          />
        </div>
      </div>
    </header>
  );
}

/** 顶栏左侧的 provider 切换按钮:显示当前 provider 图标 + 标签,
 * 点击展开下拉菜单,可切换到其他已配置的 provider。 */
function HeaderProviderSwitcher({
  providers,
  currentProvider,
  onSelect,
}: {
  providers: TopBarProvider[];
  currentProvider: string | null;
  onSelect: (provider: string) => void;
}) {
  const { t } = useTranslation();
  // 当前 provider 行(用于取 api_base 给 ProviderIcon 动态生成 custom 图标)
  const currentRow = providers.find((p) => p.name === currentProvider) ?? null;
  const currentLabel = currentProvider
    ? providerDisplayLabel(providers, currentProvider)
    : t("thread.header.provider.empty");

  // 与设置页"已配置区域"保持一致:configured 为真,或 custom 有命名 preset 时显示。
  const availableProviders = providers.filter(
    (p) => p.configured || (p.name === "custom" && (p.presets?.length ?? 0) > 0),
  );

  // 解析 provider brand:custom(单例或虚拟 row custom__xxx)用 api_base 动态生成,
  // 其余用内置 providerBrand
  const resolveBrand = (providerName: string, apiBase: string | null | undefined): ProviderBrand | null =>
    resolveCustomBrand(providerName, apiBase);

  // trigger 图标:跟随 logoIndex 回退
  const [triggerLogoIndex, setTriggerLogoIndex] = useState(0);
  const triggerBrand = useMemo(
    () => resolveBrand(currentProvider ?? "", currentRow?.api_base),
    [currentProvider, currentRow?.api_base],
  );
  useEffect(() => setTriggerLogoIndex(0), [currentProvider, currentRow?.api_base]);
  const triggerLogoUrl = triggerBrand?.logoUrls[triggerLogoIndex];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label={t("thread.header.provider.ariaLabel")}
          title={t("thread.header.provider.ariaLabel")}
          className={cn(
            "h-8 gap-1.5 rounded-full border border-border/45 bg-card/80 px-2.5",
            "text-[11.5px] font-medium text-foreground/75 shadow-[0_1px_3px_rgba(15,23,42,0.04)]",
            "hover:bg-accent/40 hover:text-foreground",
          )}
        >
          {triggerBrand ? (
            <span
              className="grid h-4 w-4 shrink-0 place-items-center overflow-hidden rounded-full border bg-background"
              style={{ borderColor: `${triggerBrand.color}28` }}
              aria-hidden
            >
              {triggerLogoUrl ? (
                <img
                  src={triggerLogoUrl}
                  alt=""
                  className="h-2.5 w-2.5 object-contain"
                  onError={() => setTriggerLogoIndex((index) => index + 1)}
                />
              ) : (
                <span
                  className="grid h-full w-full place-items-center rounded-full text-white text-[6px]"
                  style={{ backgroundColor: triggerBrand.color }}
                >
                  {triggerBrand.initials.slice(0, 2)}
                </span>
              )}
            </span>
          ) : null}
          <span className="max-w-[7rem] truncate">{currentLabel}</span>
          <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[18rem] w-[220px] overflow-y-auto"
      >
        <DropdownMenuLabel className="text-[11px] uppercase tracking-wide text-muted-foreground">
          {t("thread.header.provider.label")}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {availableProviders.length === 0 ? (
          <div className="px-2.5 py-2 text-[12px] text-muted-foreground">
            {t("thread.header.provider.empty")}
          </div>
        ) : (
          availableProviders.map((provider) => {
            const selected = provider.name === currentProvider;
            const itemBrand = resolveBrand(provider.name, provider.api_base);
            return (
              <DropdownMenuItem
                key={provider.name}
                onSelect={() => onSelect(provider.name)}
                className={cn(
                  "flex items-center gap-2 rounded-[10px] px-2.5 py-2 text-[12.5px]",
                  "focus:bg-muted/85 focus:text-foreground",
                  selected && "bg-muted/80 text-foreground focus:bg-muted",
                )}
              >
                <ProviderBrandIcon brand={itemBrand} />
                <span className="min-w-0 flex-1 truncate">{provider.label}</span>
                {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
              </DropdownMenuItem>
            );
          })
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** 下拉项内的小图标:h-4 w-4,支持 favicon 回退链(失败自动切下一个 logoUrl,
 *  全部失败后显示首字母)。custom provider 的 brand 由调用方通过 resolveBrand 算好传入。 */
function ProviderBrandIcon({ brand }: { brand: ProviderBrand | null }) {
  const [logoIndex, setLogoIndex] = useState(0);
  useEffect(() => setLogoIndex(0), [brand]);
  const logoUrl = brand?.logoUrls[logoIndex];
  if (!brand) return null;
  return (
    <span
      className="grid h-4 w-4 shrink-0 place-items-center overflow-hidden rounded-full border bg-background"
      style={{ borderColor: `${brand.color}28` }}
      aria-hidden
    >
      {logoUrl ? (
        <img
          src={logoUrl}
          alt=""
          className="h-2.5 w-2.5 object-contain"
          onError={() => setLogoIndex((index) => index + 1)}
        />
      ) : (
        <span
          className="grid h-full w-full place-items-center rounded-full text-white text-[6px]"
          style={{ backgroundColor: brand.color }}
        >
          {brand.initials.slice(0, 2)}
        </span>
      )}
    </span>
  );
}

function ThemeButton({
  theme,
  themeMode,
  onToggleTheme,
  label,
  className,
}: {
  theme: "light" | "dark";
  themeMode?: ThemeMode;
  onToggleTheme: () => void;
  label: string;
  className?: string;
}) {
  const mode = themeMode ?? theme;
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={label}
      onClick={onToggleTheme}
      className={cn(
        "h-8 w-8 rounded-full text-muted-foreground hover:bg-accent/40 hover:text-foreground",
        className,
      )}
    >
      {mode === "light" ? (
        <Sun className="h-4 w-4" />
      ) : mode === "dark" ? (
        <Moon className="h-4 w-4" />
      ) : (
        <Monitor className="h-4 w-4" />
      )}
    </Button>
  );
}

function LocaleButton({
  onToggleLanguage,
  label,
  className,
}: {
  onToggleLanguage: () => void;
  label: string;
  className?: string;
}) {
  const { i18n } = useTranslation();
  const isEn = (i18n.resolvedLanguage ?? i18n.language) === "en";

  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={label}
      onClick={onToggleLanguage}
      className={cn(
        "h-8 w-8 rounded-full hover:bg-accent/40 hover:text-foreground",
        className,
      )}
    >
      <span className="flex items-baseline gap-[1px] text-[10px] leading-none tracking-tight">
        <span className={cn(
          "font-semibold text-foreground",
          !isEn && "font-normal text-muted-foreground/45",
        )}>A</span>
        <span className={cn(
          "font-semibold text-foreground",
          isEn && "font-normal text-muted-foreground/45",
        )}>文</span>
      </span>
    </Button>
  );
}
