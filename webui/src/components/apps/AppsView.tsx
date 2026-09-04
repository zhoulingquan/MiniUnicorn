import { LayoutGrid } from "lucide-react";
import { useTranslation } from "react-i18next";

import { AppsSettings } from "@/components/settings/sections/AppsSettings";
import { ViewShell } from "@/components/ui/view-shell";

interface AppsViewProps {
  onBack: () => void;
}

/** Apps 资源页：MCP Presets 管理入口。
 * 复用 AppsSettings 渲染主体，外加 ViewShell 提供返回按钮与标题。
 * 传 hideTitle 避免与 ViewShell 的 h1 重复渲染 "Apps" heading。 */
export function AppsView({ onBack }: AppsViewProps) {
  const { t } = useTranslation();
  return (
    <ViewShell
      onBack={onBack}
      icon={<LayoutGrid className="h-4 w-4 text-foreground/80" />}
      title={t("sidebar.apps")}
    >
      <AppsSettings hideTitle />
    </ViewShell>
  );
}
